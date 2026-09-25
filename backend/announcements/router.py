"""Servico de anuncio/mensagem do servidor."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.database.models import Announcement

VALID_TARGETS = {"all", "web", "desktop"}


def announcement_payload(row: Announcement | None) -> dict:
    if row is None:
        return {"enabled": False}
    return {
        "id": str(row.id),
        "enabled": row.enabled,
        "title": row.title,
        "message": row.message,
        "target": row.target,
        "display_mode": row.display_mode,
        "cta_label": row.cta_label,
        "cta_url": row.cta_url,
        "starts_at": row.starts_at,
        "ends_at": row.ends_at,
        "updated_at": row.updated_at,
    }


def active_announcement(db: Session, platform: str = "all") -> Announcement | None:
    platform = platform if platform in VALID_TARGETS else "all"
    now = datetime.now(timezone.utc)
    stmt = (
        select(Announcement)
        .where(Announcement.enabled.is_(True))
        .where(Announcement.target.in_(["all", platform]))
        .where((Announcement.starts_at.is_(None)) | (Announcement.starts_at <= now))
        .where((Announcement.ends_at.is_(None)) | (Announcement.ends_at > now))
        .order_by(Announcement.updated_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()
