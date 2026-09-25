"""
Verificação de integridade do cliente oficial.

Windows:
    O app envia sha256 do próprio executável assinado (app_hash) +
    metadados do certificado Authenticode. O servidor compara com a
    allowlist de hashes publicados a cada release oficial.

Android:
    O app envia um token do Play Integrity API. O servidor decodifica
    via Google (server-to-server) e valida: pacote oficial, assinatura
    de certificado esperada, veredito MEETS_DEVICE_INTEGRITY.

IMPORTANTE: integridade de cliente é defesa em profundidade, não a
única barreira. Mesmo que seja burlada, o atacante ainda precisa de
conta + dispositivo autorizado + assinatura Ed25519 + limites de uso.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx

from backend.api.config import get_settings

_http_client: httpx.AsyncClient | None = None
_token_lock: asyncio.Lock | None = None
_access_token_cache: tuple[str, float] | None = None


def _integrity_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=10,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _http_client


async def shutdown_integrity_http() -> None:
    global _http_client, _token_lock, _access_token_cache
    if _http_client is not None:
        await _http_client.aclose()
    _http_client = None
    _token_lock = None
    _access_token_cache = None


def verify_windows_integrity(app_hash: str | None) -> bool:
    settings = get_settings()
    if not settings.integrity_enforced:
        return True
    allowlist = settings.windows_hash_allowlist
    if not allowlist:
        # Sem allowlist configurada = não dá para validar; falha fechado em prod.
        return settings.environment != "production"
    return bool(app_hash) and app_hash.strip().lower() in allowlist


async def verify_play_integrity(integrity_token: str | None) -> bool:
    """
    Decodifica o token via playintegrity.googleapis.com usando a conta
    de serviço, e valida appRecognitionVerdict / deviceRecognitionVerdict.
    """
    settings = get_settings()
    if not settings.integrity_enforced:
        return True
    if not integrity_token:
        return False

    sa_path = Path(settings.google_service_account_json)
    if not sa_path.exists():
        return settings.environment != "production"

    client = _integrity_http_client()
    access_token = await _service_account_access_token(sa_path, client)
    url = (
        f"{settings.play_integrity_decode_url}/packages/"
        f"{settings.play_integrity_package_name}:decodeIntegrityToken"
    )
    resp = await client.post(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        json={"integrityToken": integrity_token},
    )
    if resp.status_code != 200:
        return False

    payload = resp.json().get("tokenPayloadExternal", {})
    app_integrity = payload.get("appIntegrity", {})
    device_integrity = payload.get("deviceIntegrity", {})
    return (
        app_integrity.get("appRecognitionVerdict") == "PLAY_RECOGNIZED"
        and app_integrity.get("packageName") == settings.play_integrity_package_name
        and "MEETS_DEVICE_INTEGRITY" in device_integrity.get("deviceRecognitionVerdict", [])
    )


async def _service_account_access_token(
    sa_path: Path,
    client: httpx.AsyncClient,
) -> str:
    """OAuth2 JWT-bearer assíncrono, com reaproveitamento até a expiração."""
    import jwt as pyjwt

    global _token_lock, _access_token_cache
    now_monotonic = time.monotonic()
    if _access_token_cache and now_monotonic < _access_token_cache[1]:
        return _access_token_cache[0]
    if _token_lock is None:
        _token_lock = asyncio.Lock()
    async with _token_lock:
        now_monotonic = time.monotonic()
        if _access_token_cache and now_monotonic < _access_token_cache[1]:
            return _access_token_cache[0]

        sa = json.loads(sa_path.read_text())
        now = int(time.time())
        assertion = pyjwt.encode(
            {
                "iss": sa["client_email"],
                "scope": "https://www.googleapis.com/auth/playintegrity",
                "aud": sa["token_uri"],
                "iat": now,
                "exp": now + 3600,
            },
            sa["private_key"],
            algorithm="RS256",
        )
        resp = await client.post(
            sa["token_uri"],
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        token = str(payload["access_token"])
        expires_in = max(1, int(payload.get("expires_in") or 3600))
        _access_token_cache = (
            token,
            time.monotonic() + max(1, expires_in - 60),
        )
        return token
