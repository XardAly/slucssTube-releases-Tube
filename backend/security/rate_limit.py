"""
Rate limiting no PostgreSQL por dispositivo e IP.

Camadas de defesa:
    Cloudflare (borda)  ->  Nginx (limit_req)  ->  aqui (por identidade)
"""

from __future__ import annotations

from fastapi import HTTPException, status
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.api.config import get_settings
from backend.database.models import ApiRequest


def enforce_rate_limit(db: Session, *, device_id, ip: str | None) -> None:
    limit = get_settings().rate_limit_per_minute
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    checks = []
    if device_id:
        checks.append((ApiRequest.device_id == device_id, limit))
    if ip:
        checks.append((ApiRequest.ip == ip, limit * 3))
    # Serializa a janela por identidade até o commit do registro de auditoria;
    # duas rajadas simultâneas não conseguem observar o mesmo contador antigo.
    lock_keys = sorted(
        key for key in (
            f"device:{device_id}" if device_id else None,
            f"ip:{ip}" if ip else None,
        ) if key
    )
    for key in lock_keys:
        db.execute(select(func.pg_advisory_xact_lock(func.hashtext(key))))
    for condition, threshold in checks:
        count = db.execute(
            select(func.count(ApiRequest.id)).where(
                ApiRequest.timestamp >= since, condition
            )
        ).scalar_one()
        if count >= threshold:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Limite de requisições excedido. Tente novamente em instantes.",
                headers={"Retry-After": "60"},
            )
