"""Durable release publication. Clients consume the signed manifest as a feed.

No outbound message is sent on boot. A unique version_code and release_id make
publication idempotent across restarts and multiple API workers.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column, Session

from backend.database.session import Base


class AndroidRelease(Base):
    __tablename__ = "android_releases"
    release_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version_code: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def publish_release(db: Session, *, release_id: str, version: str, version_code: int,
                    artifacts: dict) -> tuple[AndroidRelease, bool]:
    fingerprint = hashlib.sha256(json.dumps(
        {"version": version, "artifacts": artifacts}, sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()

    def existing():
        return db.scalar(select(AndroidRelease).where(
            (AndroidRelease.release_id == release_id) | (AndroidRelease.version_code == version_code)
        ))

    row = existing()
    created = False
    if row is None:
        row = AndroidRelease(release_id=release_id, version=version, version_code=version_code,
                             fingerprint=fingerprint, published_at=datetime.now(timezone.utc))
        db.add(row)
        try:
            db.commit()
            created = True
        except IntegrityError:
            db.rollback()
            row = existing()
    if row is None or (row.release_id, row.version_code, row.fingerprint) != (release_id, version_code, fingerprint):
        raise ValueError("Android release is immutable: increment version_code and release_id for different APKs")
    return row, created
