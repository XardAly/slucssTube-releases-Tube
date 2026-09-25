"""
Painel administrativo (API).

Autenticação própria via X-Admin-Key (chave do .env, nunca embutida em
cliente). Fica fora do fluxo de dispositivos — admin não é um "device".
Em produção, restrinja também o acesso por IP no Nginx/Cloudflare.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.api.deps import require_admin
from backend.api.config import get_settings
from backend.database.models import (
    Announcement,
    ApiRequest,
    Ban,
    Device,
    DeviceStatus,
    Download,
    DownloadStatus,
    MediaCache,
    SecurityLog,
    SessionToken,
)
from backend.database.session import get_db
from backend.announcements.router import announcement_payload
from backend.downloads import service as download_service
from backend.downloads import cache as media_cache
from backend.security import fraud
from backend.tasks.manager import notify_task_manager, task_manager_metrics
from backend.storage.base import StoragePermanentError, StorageTemporaryError
from backend.storage.google_drive import GoogleDriveStorageBackend
from backend.storage.manager import get_api_storage_backend

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/storage")
async def storage_status():
    """Estado operacional sem IDs remotos, conta Google ou credenciais."""
    settings = get_settings()
    if settings.storage_backend != "google_drive":
        return {"backend": "local", "connected": True, "needs_reconnect": False}
    backend = get_api_storage_backend("google_drive")
    if not isinstance(backend, GoogleDriveStorageBackend):
        return {"backend": "google_drive", "connected": False, "needs_reconnect": False}
    try:
        quota = await backend.storage_status(force_refresh=True)
    except StorageTemporaryError:
        return {
            "backend": "google_drive",
            "connected": False,
            "temporarily_unavailable": True,
            "needs_reconnect": False,
        }
    except StoragePermanentError as exc:
        return {
            "backend": "google_drive",
            "connected": False,
            "temporarily_unavailable": False,
            "needs_reconnect": "reconecte" in str(exc).lower(),
        }
    usage_percent = quota.get("usage_percent")
    return {
        "backend": "google_drive",
        "connected": True,
        "needs_reconnect": False,
        "usage_bytes": quota.get("usage_bytes"),
        "free_bytes": quota.get("free_bytes"),
        "trash_bytes": quota.get("trash_bytes"),
        "usage_percent": usage_percent,
        "storage_alert": (
            isinstance(usage_percent, (int, float))
            and usage_percent >= settings.google_drive_storage_alert_percent
        ),
    }


class BanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: uuid.UUID | None = None
    ip: str | None = Field(default=None, max_length=45)
    reason: str = Field(max_length=500)
    hours: int | None = Field(default=24, description="None = permanente")

    @model_validator(mode="after")
    def has_target(self) -> "BanRequest":
        if not self.device_id and not self.ip:
            raise ValueError("Informe device_id ou IP.")
        if self.hours is not None and not 1 <= self.hours <= 24 * 365:
            raise ValueError("Duração de banimento inválida.")
        return self


class AnnouncementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=120)
    message: str = Field(max_length=1000)
    enabled: bool = True
    target: str = Field(default="all", pattern="^(all|web|desktop)$")
    display_mode: str = Field(default="popup", pattern="^(popup|banner)$")
    cta_label: str | None = Field(default=None, max_length=40)
    cta_url: HttpUrl | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @field_validator("cta_url")
    @classmethod
    def cta_must_use_https(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and value.scheme != "https":
            raise ValueError("O link do anúncio deve usar HTTPS.")
        return value


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    return {
        **download_service.public_download_stats(db),
        "devices_total": db.execute(select(func.count(Device.id))).scalar_one(),
        "devices_blocked": db.execute(
            select(func.count(Device.id)).where(Device.status == DeviceStatus.blocked)
        ).scalar_one(),
        "sessions_active": db.execute(
            select(func.count(SessionToken.id)).where(
                SessionToken.revoked.is_(False),
                SessionToken.expires_at > datetime.now(timezone.utc),
            )
        ).scalar_one(),
        "downloads_24h": db.execute(
            select(func.count(Download.id)).where(Download.created_at >= day_ago)
        ).scalar_one(),
        "downloads_failed_24h": db.execute(
            select(func.count(Download.id)).where(
                Download.created_at >= day_ago, Download.status == DownloadStatus.failed
            )
        ).scalar_one(),
        "requests_24h": db.execute(
            select(func.count(ApiRequest.id)).where(ApiRequest.timestamp >= day_ago)
        ).scalar_one(),
        "security_events_24h": db.execute(
            select(func.count(SecurityLog.id)).where(SecurityLog.created_at >= day_ago)
        ).scalar_one(),
    }


@router.get("/cache")
def cache_status(db: Session = Depends(get_db)):
    """Métricas e estados agregados, sem expor IDs do Drive."""
    states = {
        state: int(total)
        for state, total in db.execute(
            select(MediaCache.state, func.count(MediaCache.id)).group_by(MediaCache.state)
        ).all()
    }
    ready_bytes = db.execute(
        select(func.coalesce(func.sum(MediaCache.file_size), 0)).where(
            MediaCache.state == "ready",
        )
    ).scalar_one()
    return {
        **media_cache.metrics(db),
        "states": states,
        "ready_bytes": int(ready_bytes or 0),
    }


@router.post("/cache/maintenance")
def run_cache_maintenance():
    """Executa recuperação, auditoria do Drive e limpeza local já existentes."""
    from backend.workers.tasks import (
        audit_remote_storage,
        cleanup_orphaned_files,
        recover_media_cache,
    )

    return {
        "recovered": recover_media_cache(),
        "remote_removed": audit_remote_storage(),
        "local_removed": cleanup_orphaned_files(),
    }


@router.post("/cache/{cache_id}/invalidate", status_code=204)
def invalidate_cache_entry(cache_id: uuid.UUID, db: Session = Depends(get_db)):
    entry = db.execute(
        select(MediaCache).where(MediaCache.id == cache_id).with_for_update(),
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entrada de cache não encontrada.")
    media_cache.invalidate_remote_entry(db, entry, "admin_invalidated")
    db.commit()


@router.get("/announcement")
def get_admin_announcement(db: Session = Depends(get_db)):
    row = db.execute(
        select(Announcement).order_by(Announcement.updated_at.desc()).limit(1)
    ).scalars().first()
    return announcement_payload(row)


@router.put("/announcement")
def upsert_announcement(data: AnnouncementRequest, db: Session = Depends(get_db)):
    if data.ends_at and data.starts_at and data.ends_at <= data.starts_at:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "ends_at deve ser maior que starts_at.")

    row = db.execute(
        select(Announcement).order_by(Announcement.updated_at.desc()).limit(1)
    ).scalars().first()
    if row is None:
        row = Announcement(title=data.title, message=data.message)
        db.add(row)

    row.title = data.title
    row.message = data.message
    row.enabled = data.enabled
    row.target = data.target
    row.display_mode = data.display_mode
    row.cta_label = data.cta_label
    row.cta_url = str(data.cta_url) if data.cta_url else None
    row.starts_at = data.starts_at
    row.ends_at = data.ends_at
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return announcement_payload(row)


@router.delete("/announcement", status_code=204)
def disable_announcement(db: Session = Depends(get_db)):
    row = db.execute(
        select(Announcement).order_by(Announcement.updated_at.desc()).limit(1)
    ).scalars().first()
    if row:
        row.enabled = False
        row.updated_at = datetime.now(timezone.utc)
        db.commit()


@router.get("/devices")
def list_devices(
    db: Session = Depends(get_db),
    status_filter: DeviceStatus | None = None,
    limit: int = 50,
    offset: int = 0,
):
    stmt = select(Device).order_by(Device.first_seen.desc())
    if status_filter:
        stmt = stmt.where(Device.status == status_filter)
    rows = db.execute(stmt.limit(min(limit, 200)).offset(offset)).scalars().all()
    return [
        {
            "id": str(d.id),
            "device_id": d.device_id,
            "platform": d.platform,
            "app_version": d.app_version,
            "status": d.status.value,
            "first_seen": d.first_seen,
            "last_seen": d.last_seen,
        }
        for d in rows
    ]


def _set_device_status(db: Session, device_id: uuid.UUID, new_status: DeviceStatus, event: str) -> None:
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dispositivo não encontrado.")
    device.status = new_status
    if new_status == DeviceStatus.blocked:
        # Revoga todas as sessões: bloqueio tem efeito imediato.
        for s in db.execute(select(SessionToken).where(SessionToken.device_id == device_id)).scalars():
            s.revoked = True
    db.commit()
    fraud.log_event(db, event, device_id=device_id)


@router.post("/devices/{device_id}/block", status_code=204)
def block_device(device_id: uuid.UUID, db: Session = Depends(get_db)):
    _set_device_status(db, device_id, DeviceStatus.blocked, "admin_block_device")


@router.post("/devices/{device_id}/unblock", status_code=204)
def unblock_device(device_id: uuid.UUID, db: Session = Depends(get_db)):
    _set_device_status(db, device_id, DeviceStatus.authorized, "admin_unblock_device")


@router.get("/sessions")
def list_sessions(db: Session = Depends(get_db), limit: int = 100, offset: int = 0):
    rows = (
        db.execute(
            select(SessionToken).order_by(SessionToken.created_at.desc()).limit(min(limit, 500)).offset(offset)
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(s.id),
            "device_id": str(s.device_id),
            "created_at": s.created_at,
            "expires_at": s.expires_at,
            "revoked": s.revoked,
        }
        for s in rows
    ]


@router.post("/sessions/{session_id}/revoke", status_code=204)
def revoke_session(session_id: uuid.UUID, db: Session = Depends(get_db)):
    session = db.get(SessionToken, session_id)
    if not session:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sessão não encontrada.")
    session.revoked = True
    db.commit()


@router.get("/downloads")
def list_downloads(
    db: Session = Depends(get_db),
    status_filter: DownloadStatus | None = None,
    limit: int = 100,
    offset: int = 0,
):
    stmt = select(Download).order_by(Download.created_at.desc())
    if status_filter:
        stmt = stmt.where(Download.status == status_filter)
    rows = db.execute(stmt.limit(min(limit, 500)).offset(offset)).scalars().all()
    return [
        {
            "id": str(d.id),
            "device_id": str(d.device_id),
            "url": d.url,
            "status": d.status.value,
            "storage_backend": d.storage_backend,
            "storage_state": d.storage_state,
            "file_size": d.file_size,
            "processing_time": d.processing_time,
            "created_at": d.created_at,
            "completed_at": d.completed_at,
        }
        for d in rows
    ]


@router.post("/downloads/{download_id}/retry-storage", status_code=202)
def retry_download_storage(download_id: uuid.UUID, db: Session = Depends(get_db)):
    download = db.get(Download, download_id)
    if not download:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Arquivo de recuperação não encontrado.")
    heartbeat = download.heartbeat_at
    if heartbeat and heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
    if (
        download.status == DownloadStatus.processing
        and heartbeat
        and heartbeat > datetime.now(timezone.utc) - timedelta(minutes=15)
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "A tarefa de armazenamento ainda está ativa.")
    remotely_confirmed = bool(
        download.storage_key
        and download.storage_state in {"remote_ready", "local_delete_pending", "completed"}
    )
    if not download.file_path and not remotely_confirmed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Arquivo de recuperação não encontrado.")
    if download.storage_state not in {
        "local_ready", "local_fallback", "remote_uploading",
        "remote_ready", "local_delete_pending", "completed",
    }:
        raise HTTPException(status.HTTP_409_CONFLICT, "O armazenamento não permite nova tentativa.")
    download.status = DownloadStatus.queued
    download.operation_type = "storage_retry"
    download.stage = "queued"
    download.progress_message = "Recuperação do armazenamento aguardando na fila."
    download.error = None
    download.cancel_requested = False
    download.cancel_requested_at = None
    download.worker_id = None
    download.heartbeat_at = None
    download.queued_at = datetime.now(timezone.utc)
    db.commit()
    notify_task_manager()
    return {"queued": True}


@router.post("/bans", status_code=201)
def create_ban(data: BanRequest, db: Session = Depends(get_db)):
    if not any([data.device_id, data.ip]):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Informe device_id ou ip.")
    fraud.apply_ban(db, data.reason, device_id=data.device_id, ip=data.ip, hours=data.hours)
    return {"created": True}


@router.get("/bans")
def list_bans(db: Session = Depends(get_db), limit: int = 100):
    rows = db.execute(select(Ban).order_by(Ban.created_at.desc()).limit(min(limit, 500))).scalars().all()
    return [
        {
            "id": str(b.id),
            "device_id": str(b.device_id) if b.device_id else None,
            "ip": b.ip,
            "reason": b.reason,
            "created_at": b.created_at,
            "expires_at": b.expires_at,
        }
        for b in rows
    ]


@router.delete("/bans/{ban_id}", status_code=204)
def remove_ban(ban_id: uuid.UUID, db: Session = Depends(get_db)):
    ban = db.get(Ban, ban_id)
    if not ban:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ban não encontrado.")
    db.delete(ban)
    db.commit()


@router.get("/security-logs")
def security_logs(
    db: Session = Depends(get_db),
    event: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    stmt = select(SecurityLog).order_by(SecurityLog.created_at.desc())
    if event:
        stmt = stmt.where(SecurityLog.event == event)
    rows = db.execute(stmt.limit(min(limit, 500)).offset(offset)).scalars().all()
    return [
        {
            "id": log.id,
            "event": log.event,
            "device_id": str(log.device_id) if log.device_id else None,
            "ip": log.ip,
            "details": log.details,
            "created_at": log.created_at,
        }
        for log in rows
    ]


@router.get("/workers")
def workers_status():
    """Estado do gerenciador de processos embutido na API."""
    return task_manager_metrics()
