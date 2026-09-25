"""
Motor antifraude (sistema sem contas — identidade = dispositivo).

Detecta e reage a:
- Criação em massa de sessões/dispositivos por IP
- Comportamento de bot (velocidade anormal de requisições)
- Reutilização de identidade de dispositivo (chave divergente)

Reação: registra em security_logs e aplica ban automático quando
os thresholds são estourados. Admin pode revisar/remover bans.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.api.config import get_settings
from backend.database.models import Ban, SecurityLog


def log_event(
    db: Session,
    event: str,
    *,
    device_id: uuid.UUID | None = None,
    ip: str | None = None,
    details: str | None = None,
) -> None:
    db.add(SecurityLog(device_id=device_id, ip=ip, event=event, details=details))
    db.commit()
    severity = {
        "replay_detected": "high",
        "signature_invalid": "high",
        "device_key_mismatch": "high",
        "challenge_failed": "medium",
        "session_create_blocked": "medium",
        "device_check_failed": "medium",
        "bot_velocity": "medium",
        "auto_ban": "high",
    }.get(event)
    if severity:
        try:
            from backend.notifications.discord import report_security_event

            report_security_event(
                event_type=event,
                severity=severity,
                ip=ip,
                device_id=device_id,
                details=details,
            )
        except Exception:  # noqa: BLE001 - alertas nunca alteram a operação
            pass


def is_banned(db: Session, *, device_id: uuid.UUID | None = None, ip: str | None = None) -> bool:
    now = datetime.now(timezone.utc)
    conditions = []
    if device_id:
        conditions.append(Ban.device_id == device_id)
    if ip:
        conditions.append(Ban.ip == ip)
    if not conditions:
        return False
    stmt = select(Ban).where(or_(*conditions)).where((Ban.expires_at.is_(None)) | (Ban.expires_at > now))
    return db.execute(stmt.limit(1)).scalar_one_or_none() is not None


def is_banned_cached(
    db: Session,
    *,
    device_id: uuid.UUID | None = None,
    ip: str | None = None,
) -> bool:
    """Compatibilidade de chamada: a fonte de verdade agora é o PostgreSQL."""
    return is_banned(db, device_id=device_id, ip=ip)


def apply_ban(
    db: Session,
    reason: str,
    *,
    device_id: uuid.UUID | None = None,
    ip: str | None = None,
    hours: int | None = 24,
) -> None:
    expires = datetime.now(timezone.utc) + timedelta(hours=hours) if hours else None
    db.add(Ban(device_id=device_id, ip=ip, reason=reason, expires_at=expires))
    db.commit()
    log_event(db, "auto_ban", device_id=device_id, ip=ip, details=reason)


def check_session_abuse(db: Session, ip: str) -> bool:
    """True se o IP está criando sessões em volume anormal (janela de 1h)."""
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    count = db.execute(
        select(func.count(SecurityLog.id)).where(
            SecurityLog.ip == ip,
            SecurityLog.event == "session_created",
            SecurityLog.created_at >= since,
        )
    ).scalar_one()
    return int(count) >= get_settings().sessions_per_ip_per_hour


def flag_suspicious_velocity(db: Session, device_id: uuid.UUID) -> bool:
    """Bot heuristic: mais de 10 requisições no mesmo segundo pelo device."""
    from backend.database.models import ApiRequest

    since = datetime.now(timezone.utc) - timedelta(seconds=1)
    count = db.execute(
        select(func.count(ApiRequest.id)).where(
            ApiRequest.device_id == device_id,
            ApiRequest.timestamp >= since,
        )
    ).scalar_one()
    return int(count) > 10
