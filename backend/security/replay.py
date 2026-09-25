"""
Proteção anti-replay.

Duas camadas:
1. X-Timestamp deve estar dentro da janela de tolerância (skew).
2. X-Request-ID (UUID) só pode ser usado UMA vez — registrado no PostgreSQL
   com chave primária e expiração curta.
"""

from __future__ import annotations

import time

from backend.api.config import get_settings
from backend.database.models import SecurityNonce
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone

_last_nonce_cleanup = 0.0


def timestamp_valid(timestamp: str) -> bool:
    try:
        ts = int(float(timestamp))
    except (TypeError, ValueError):
        return False
    return abs(time.time() - ts) <= get_settings().signature_max_skew_seconds


def request_id_fresh(db: Session, request_id: str) -> bool:
    """True se o request_id nunca foi visto (e o registra atomicamente)."""
    global _last_nonce_cleanup
    if not request_id or len(request_id) > 64:
        return False
    now_monotonic = time.monotonic()
    if now_monotonic - _last_nonce_cleanup >= 60:
        db.execute(
            delete(SecurityNonce).where(
                SecurityNonce.expires_at <= datetime.now(timezone.utc)
            )
        )
        _last_nonce_cleanup = now_monotonic
    ttl = get_settings().signature_max_skew_seconds * 2
    try:
        # O SAVEPOINT preserva a sessão quando a chave única acusa replay.
        # A gravação é confirmada junto do registro de auditoria ao final de
        # get_auth_context, evitando um round-trip/commit extra por request.
        with db.begin_nested():
            db.add(SecurityNonce(
                id=request_id,
                kind="replay",
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl),
            ))
            db.flush()
        return True
    except IntegrityError:
        return False
