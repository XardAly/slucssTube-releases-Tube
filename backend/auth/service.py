"""
Regras de negócio das sessões temporárias.

Não há login nem contas: o dispositivo pede autorização, o servidor
valida (integridade + antifraude) e emite tokens de curta duração.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.api.config import get_settings
from backend.api.deps import _version_tuple
from backend.auth.schemas import DeviceInfo, SessionCreateRequest, TokenResponse
from backend.auth.tokens import create_access_token, hash_refresh_token, new_refresh_token
from backend.database.models import Device, DeviceStatus, SessionToken
from backend.notifications.discord import notify_app_updated
from backend.security import fraud
from backend.security.integrity import verify_play_integrity, verify_windows_integrity


log = logging.getLogger(__name__)


async def _verify_client_integrity(device: DeviceInfo) -> None:
    if device.platform == "windows":
        ok = verify_windows_integrity(device.app_hash)
    elif device.platform == "android":
        settings = get_settings()
        # Distribuição direta pelo site não produz um veredito
        # PLAY_RECOGNIZED. Se um token for enviado (futura versão Play), ele
        # continua sendo validado normalmente; sem token, o modo sideload deve
        # estar explicitamente habilitado na configuração do servidor.
        ok = (
            await verify_play_integrity(device.play_integrity_token)
            if device.play_integrity_token
            else settings.android_sideload_enabled
        )
    else:
        # Web não possui attestation: a defesa fica no CORS restritivo,
        # rate limit por IP e heurísticas de comportamento.
        ok = True
    if not ok:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cliente não reconhecido como oficial.")


def _get_or_register_device(db: Session, info: DeviceInfo, ip: str) -> Device:
    device = db.execute(
        select(Device).where(Device.device_id == info.device_id)
    ).scalar_one_or_none()

    if device:
        if device.status == DeviceStatus.blocked:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Dispositivo bloqueado.")
        if device.platform != info.platform:
            fraud.log_event(db, "device_platform_mismatch", device_id=device.id, ip=ip)
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Identidade do dispositivo inválida.")
        # Chave pública é fixada no primeiro registro (TOFU). Troca de chave
        # exige nova instalação — impede clonagem de device_id.
        if device.public_key.strip() != info.public_key.strip():
            fraud.log_event(db, "device_key_mismatch", device_id=device.id, ip=ip)
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Identidade do dispositivo inválida.")
        previous_version = device.app_version
        app_updated = (
            device.platform in {"windows", "android"}
            and _version_tuple(info.app_version) > _version_tuple(previous_version)
        )
        device.app_version = info.app_version
        device.last_seen = datetime.now(timezone.utc)
        db.commit()
        if app_updated:
            try:
                notify_app_updated(device, previous_version)
            except Exception:  # noqa: BLE001 - o webhook nunca bloqueia a sessao
                log.warning("app_update_webhook_enqueue_failed")
        return device

    device = Device(
        device_id=info.device_id,
        installation_id=info.installation_id,
        hardware_hash=info.hardware_hash,
        public_key=info.public_key,
        platform=info.platform,
        app_version=info.app_version,
        last_seen=datetime.now(timezone.utc),
    )
    db.add(device)
    db.commit()
    fraud.log_event(db, "device_registered", device_id=device.id, ip=ip)
    return device


def _issue_tokens(db: Session, device: Device) -> TokenResponse:
    settings = get_settings()
    refresh_token, token_hash, expires = new_refresh_token()
    db.add(SessionToken(device_id=device.id, refresh_token_hash=token_hash, expires_at=expires))
    db.commit()
    return TokenResponse(
        access_token=create_access_token(device.id),
        refresh_token=refresh_token,
        expires_in=settings.access_token_ttl_minutes * 60,
    )


async def create_session(db: Session, data: SessionCreateRequest, ip: str) -> TokenResponse:
    if fraud.check_session_abuse(db, ip):
        fraud.log_event(db, "session_create_blocked", ip=ip)
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Muitas sessões criadas. Aguarde.")
    if fraud.is_banned(db, ip=ip):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Acesso suspenso.")

    # Versão mínima antes da integridade: apps antigos têm hash fora da
    # allowlist e receberiam "cliente não reconhecido" — a mensagem de
    # atualização com o link é a orientação correta para esses usuários.
    settings = get_settings()
    minimum_version = (
        settings.android_minimum_version
        if data.device.platform == "android"
        else settings.app_minimum_version
    )
    download_url = (
        settings.android_download_url
        if data.device.platform == "android"
        else settings.app_download_url
    )
    if _version_tuple(data.device.app_version) < _version_tuple(minimum_version):
        raise HTTPException(
            status.HTTP_426_UPGRADE_REQUIRED,
            "Versão do aplicativo desatualizada. Baixe a nova versão em: "
            f"{download_url}",
        )

    await _verify_client_integrity(data.device)
    device = _get_or_register_device(db, data.device, ip)

    if fraud.is_banned(db, device_id=device.id, ip=ip):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Acesso suspenso.")

    fraud.log_event(db, "session_created", device_id=device.id, ip=ip)
    return _issue_tokens(db, device)


def refresh_session(db: Session, refresh_token: str) -> TokenResponse:
    token_hash = hash_refresh_token(refresh_token)
    session = db.execute(
        select(SessionToken)
        .where(SessionToken.refresh_token_hash == token_hash)
        .with_for_update()
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if not session or session.revoked or session.expires_at < now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sessão expirada. Solicite uma nova.")

    device = db.get(Device, session.device_id)
    if not device or device.status == DeviceStatus.blocked:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Acesso revogado.")
    if fraud.is_banned(db, device_id=device.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Acesso suspenso.")

    # Rotação: o refresh antigo é revogado e um novo é emitido.
    session.revoked = True
    return _issue_tokens(db, device)


def revoke_session(db: Session, refresh_token: str) -> None:
    session = db.execute(
        select(SessionToken).where(SessionToken.refresh_token_hash == hash_refresh_token(refresh_token))
    ).scalar_one_or_none()
    if session:
        session.revoked = True
        db.commit()
