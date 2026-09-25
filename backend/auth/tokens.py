"""
Tokens de sessão temporária (sem contas de usuário).

Access Token : JWT (HS256), expira em poucos minutos, carrega apenas o
               id interno do dispositivo. Nenhum dado sensível.
Refresh Token: aleatório opaco de 64 bytes, expira em dias.
               O banco guarda apenas sha256(refresh_token) — vazamento do
               banco não permite forjar sessões.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt

from backend.api.config import get_settings


def create_access_token(device_id: uuid.UUID) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "dev": str(device_id),
        "sub": str(device_id),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_access_audience,
        "purpose": "access",
        "nbf": now,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict | None:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_access_audience,
            issuer=settings.jwt_issuer,
            leeway=settings.jwt_clock_skew_seconds,
            options={"require": ["sub", "iss", "aud", "purpose", "iat", "nbf", "exp", "jti"]},
        )
        return payload if payload.get("purpose") == "access" and payload.get("sub") == payload.get("dev") else None
    except jwt.InvalidTokenError:
        return None


def create_websocket_token(device_id: uuid.UUID) -> tuple[str, int]:
    """Token restrito a eventos; não é aceito nas rotas HTTP protegidas."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    ttl = settings.websocket_ticket_ttl_seconds
    payload = {
        "dev": str(device_id),
        "sub": str(device_id),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_websocket_audience,
        "purpose": "websocket",
        "nbf": now,
        "iat": now,
        "exp": now + timedelta(seconds=ttl),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm), ttl


def decode_websocket_token(token: str) -> dict | None:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_websocket_audience,
            issuer=settings.jwt_issuer,
            leeway=settings.jwt_clock_skew_seconds,
            options={"require": ["sub", "iss", "aud", "purpose", "iat", "nbf", "exp", "jti"]},
        )
        return payload if payload.get("purpose") == "websocket" and payload.get("sub") == payload.get("dev") else None
    except jwt.InvalidTokenError:
        return None


def new_refresh_token() -> tuple[str, str, datetime]:
    """Retorna (token_puro_para_o_cliente, hash_para_o_banco, expiração)."""
    token = secrets.token_urlsafe(64)
    token_hash = hash_refresh_token(token)
    expires = datetime.now(timezone.utc) + timedelta(days=get_settings().refresh_token_ttl_days)
    return token, token_hash, expires


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_upload_token(device_id: uuid.UUID) -> tuple[str, int]:
    """
    Token de curta duração para o navegador enviar o vídeo DIRETO à API,
    sem passar o corpo pelo proxy do site (transferências grandes saindo do
    proxy disparam o anti-abuso da hospedagem). Aceito somente em
    /conversion/create-upload; assinatura, challenge e anti-replay
    continuam obrigatórios.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    ttl = 120
    payload = {
        "dev": str(device_id),
        "sub": str(device_id),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_access_audience,
        "purpose": "upload",
        "nbf": now,
        "iat": now,
        "exp": now + timedelta(seconds=ttl),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm), ttl


def decode_upload_token(token: str) -> dict | None:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_access_audience,
            issuer=settings.jwt_issuer,
            leeway=settings.jwt_clock_skew_seconds,
            options={"require": ["sub", "iss", "aud", "purpose", "iat", "nbf", "exp", "jti"]},
        )
        return payload if payload.get("purpose") == "upload" and payload.get("sub") == payload.get("dev") else None
    except jwt.InvalidTokenError:
        return None
