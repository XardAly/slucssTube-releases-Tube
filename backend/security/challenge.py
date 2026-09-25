"""
Challenge-Response para operações sensíveis (ex.: criar download).

Fluxo:
    App pede challenge  ->  servidor gera nonce único (PostgreSQL, TTL curto)
    App assina nonce    ->  servidor valida assinatura com a chave pública
                            do dispositivo e consome o nonce (uso único).
"""

from __future__ import annotations

import secrets

from backend.api.config import get_settings
from backend.database.models import SecurityNonce
from backend.security.signing import verify_raw_signature
from sqlalchemy import delete
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone


def create_challenge(db: Session, device_pk: str) -> str:
    nonce = secrets.token_urlsafe(32)
    db.add(SecurityNonce(
        id=nonce,
        kind="challenge",
        device_public_key=device_pk,
        expires_at=datetime.now(timezone.utc) + timedelta(
            seconds=get_settings().challenge_ttl_seconds
        ),
    ))
    db.commit()
    return nonce


def consume_challenge(
    db: Session,
    nonce: str,
    device_public_key: str,
    signature_b64: str,
    commit: bool = True,
) -> bool:
    """Valida e consome o challenge com DELETE RETURNING atômico."""
    stored = db.execute(
        delete(SecurityNonce)
        .where(
            SecurityNonce.id == nonce,
            SecurityNonce.kind == "challenge",
            SecurityNonce.expires_at > datetime.now(timezone.utc),
        )
        .returning(SecurityNonce.device_public_key)
    ).scalar_one_or_none()
    valid = (
        stored is not None
        and stored == device_public_key
        and verify_raw_signature(device_public_key, signature_b64, nonce.encode())
    )
    # Uma tentativa inválida continua consumindo o nonce imediatamente. No
    # caminho válido de /local/authorize, a exclusão é confirmada junto da
    # criação da operação, reduzindo outro commit sem abrir replay.
    if commit or not valid:
        db.commit()
    return valid
