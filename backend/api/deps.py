"""
Cadeia de autorização aplicada a TODO endpoint protegido.

Ordem de validação (falha em qualquer etapa = requisição rejeitada):

    1. Access token JWT válido (curta duração)
    2. Dispositivo existe, autorizado e bate com o header
    3. Dispositivo/IP não banidos
    4. Versão do app >= mínima permitida
    5. Timestamp dentro da janela (anti-replay camada 1)
    6. X-Request-ID inédito (anti-replay camada 2)
    7. Assinatura Ed25519 válida sobre a requisição
    8. Rate limit por dispositivo / IP
    9. Heurística de velocidade (bots)
   10. Registro em api_requests (auditoria)

O cliente nunca é confiável: tudo é re-validado aqui, a cada chamada.
"""

from __future__ import annotations

import hmac
import ipaddress
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from backend.api.config import get_settings
from backend.auth.tokens import decode_access_token, decode_upload_token
from backend.database.models import ApiRequest, Device, DeviceStatus
from backend.database.session import get_db
from backend.security import fraud
from backend.security.rate_limit import enforce_rate_limit
from backend.security.replay import request_id_fresh, timestamp_valid
from backend.security.signing import verify_device_signature, verify_device_signature_hash


@dataclass
class AuthContext:
    device: Device
    ip: str


def _version_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in v.split("."))
    except ValueError:
        return (0,)


def _normalized_ip(value: str | None) -> str | None:
    try:
        return str(ipaddress.ip_address((value or "").strip()))
    except ValueError:
        return None


def _peer_is_trusted(peer: str | None, configured_cidrs: str) -> bool:
    normalized = _normalized_ip(peer)
    if not normalized:
        return False
    address = ipaddress.ip_address(normalized)
    for raw in configured_cidrs.split(","):
        try:
            if raw.strip() and address in ipaddress.ip_network(raw.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip(request: Request) -> str:
    """
    IP real do usuário final — nunca o IP de um proxy interno nosso.

    Prioridade: CF-Connecting-IP / X-Real-IP (setados por Cloudflare/Nginx
    quando presentes, confiáveis) -> primeiro IP de X-Forwarded-For (caso
    do proxy do site em web/, que repassa o IP do visitante nesse header
    ao chamar a API) -> IP da conexão TCP como último recurso.
    """
    settings = get_settings()
    peer = request.client.host if request.client else None
    supplied_secret = request.headers.get("x-internal-proxy-key", "")
    secret_ok = bool(
        settings.internal_proxy_secret
        and hmac.compare_digest(supplied_secret, settings.internal_proxy_secret)
    )
    if not secret_ok and not _peer_is_trusted(peer, settings.trusted_proxy_cidrs):
        return _normalized_ip(peer) or "unknown"

    for header in ("cf-connecting-ip", "x-real-ip"):
        value = request.headers.get(header)
        normalized = _normalized_ip(value)
        if normalized:
            return normalized

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        normalized = _normalized_ip(forwarded.split(",")[0])
        if normalized:
            return normalized

    return _normalized_ip(peer) or "unknown"


async def _signed_request_body(request: Request) -> bytes:
    """Lê o corpo fora do pool de threads sem carregar uploads grandes na RAM."""
    if request.url.path == "/conversion/create-upload":
        return b""
    return await request.body()


def get_auth_context(
    request: Request,
    authorization: str = Header(default=""),
    x_device_id: str = Header(default=""),
    x_app_version: str = Header(default=""),
    x_timestamp: str = Header(default=""),
    x_request_id: str = Header(default=""),
    x_signature: str = Header(default=""),
    x_content_sha256: str = Header(default=""),
    request_body: bytes = Depends(_signed_request_body),
    db: Session = Depends(get_db),
) -> AuthContext:
    settings = get_settings()
    ip = client_ip(request)
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Não autorizado.")

    # 1. Access token (ou token de upload, restrito à rota de upload direto)
    if not authorization.startswith("Bearer "):
        raise unauthorized
    bearer = authorization.removeprefix("Bearer ").strip()
    payload = decode_access_token(bearer)
    if not payload and request.url.path == "/conversion/create-upload":
        payload = decode_upload_token(bearer)
    if not payload:
        raise unauthorized

    # 2. Dispositivo — o do token deve bater com o header
    device = db.get(Device, uuid.UUID(payload["dev"]))
    if not device or device.device_id != x_device_id or device.status == DeviceStatus.blocked:
        fraud.log_event(db, "device_check_failed", ip=ip, details=x_device_id[:64])
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Dispositivo não autorizado.")

    # 3. Bans (consulta persistente com cache local limitado)
    if fraud.is_banned_cached(db, device_id=device.id, ip=ip):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Acesso suspenso.")

    # 4. Versão mínima do app
    minimum_version = (
        settings.android_minimum_version
        if device.platform == "android"
        else settings.app_minimum_version
    )
    if _version_tuple(x_app_version) < _version_tuple(minimum_version):
        raise HTTPException(
            status.HTTP_426_UPGRADE_REQUIRED,
            "Versão do aplicativo desatualizada. Atualize para continuar.",
        )

    # 5-6. Anti-replay
    if not timestamp_valid(x_timestamp):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Timestamp inválido.")
    if not request_id_fresh(db, x_request_id):
        fraud.log_event(db, "replay_detected", device_id=device.id, ip=ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Requisição repetida.")

    # 7. Assinatura Ed25519 do dispositivo
    # A rota de upload nunca pode cair em request.body(): um header ausente ou
    # inválido deve falhar fechado, não carregar dezenas de MB na RAM.
    is_streaming_upload = request.url.path == "/conversion/create-upload"
    signature_ok = (
        verify_device_signature_hash(
            device.public_key, x_signature, request.method, request.url.path,
            x_timestamp, x_request_id, x_content_sha256,
        )
        if is_streaming_upload
        else verify_device_signature(
            device.public_key, x_signature, request.method, request.url.path,
            x_timestamp, x_request_id, request_body,
        )
    )
    if not signature_ok:
        fraud.log_event(db, "signature_invalid", device_id=device.id, ip=ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Assinatura inválida.")

    # 8. Rate limit
    enforce_rate_limit(db, device_id=device.id, ip=ip)

    # 9. Comportamento de bot
    if fraud.flag_suspicious_velocity(db, device.id):
        device.status = DeviceStatus.suspicious
        fraud.log_event(db, "bot_velocity", device_id=device.id, ip=ip)
        db.commit()

    # 10. Auditoria (país/ASN vêm do Cloudflare, repassados pelo Nginx)
    device.last_seen = datetime.now(timezone.utc)
    db.add(
        ApiRequest(
            device_id=device.id,
            endpoint=request.url.path,
            ip=ip,
            country=(request.headers.get("cf-ipcountry") or "")[:2] or None,
            asn=(request.headers.get("x-client-asn") or "")[:16] or None,
            user_agent=(request.headers.get("user-agent") or "")[:255],
            request_id=x_request_id[:64] or None,
        )
    )
    db.commit()

    return AuthContext(device=device, ip=ip)


def require_admin(x_admin_key: str = Header(default="")) -> None:
    """
    Painel admin: chave dedicada do .env, fora do fluxo de dispositivos.
    Chave vazia no servidor = admin desabilitado (fail closed).
    """
    key = get_settings().admin_api_key
    if not key or not hmac.compare_digest(x_admin_key, key):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Requer privilégios de administrador.")
