"""API organizada para consultar e controlar tarefas persistentes."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.api.deps import AuthContext, get_auth_context
from backend.database.models import Download, DownloadStatus
from backend.database.session import get_db
from backend.downloads import service
from backend.downloads.router import create_download
from backend.downloads.schemas import DownloadCreateRequest
from backend.realtime import publish_download_event
from backend.storage import service as private_storage
from backend.tasks.manager import notify_task_manager
from backend.workers.tasks import cleanup_task_files

router = APIRouter(prefix="/tasks", tags=["tasks"])
_TERMINAL = {
    DownloadStatus.completed,
    DownloadStatus.failed,
    DownloadStatus.cancelled,
}


def _owned_task(
    db: Session,
    task_id: uuid.UUID,
    device_id: uuid.UUID,
) -> Download:
    task = db.execute(
        select(Download).where(
            Download.id == task_id,
            Download.device_id == device_id,
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tarefa não encontrada.")
    return task


def task_payload(db: Session, task: Download) -> dict:
    payload = {
        "id": str(task.id),
        "user_id": str(task.device_id),
        "url": task.url,
        "operation_type": task.operation_type,
        "mode": task.mode,
        "format": task.output_format,
        "quality": task.format_spec,
        "status": task.status.value,
        "progress": task.progress,
        "progress_message": task.progress_message,
        "stage": task.stage,
        "queue_position": service.queue_position(db, task),
        "can_cancel": task.status in {DownloadStatus.queued, DownloadStatus.processing},
        "created_at": task.created_at,
        "queued_at": task.queued_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "updated_at": task.updated_at,
        "error": (
            "Não foi possível concluir a tarefa."
            if task.status == DownloadStatus.failed else None
        ),
        "title": task.title,
        "file_size": task.file_size,
        "revision": task.revision,
        "attempts": task.attempts,
    }
    if task.status == DownloadStatus.completed and private_storage.download_has_file(task):
        token = service.make_delivery_token(
            task.id, task.device_id, task.revision, task.expires_at,
        )
        payload["file_url"] = f"/download/public/{task.id}?token={token}"
        if task.mode == "gif" and task.conversion_options:
            try:
                payload["result"] = json.loads(task.conversion_options).get("result", {})
            except (TypeError, ValueError):
                pass
    return payload


@router.post("", status_code=202)
def create_task(
    data: DownloadCreateRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    """Alias organizado da criação de download, sem quebrar a rota legada."""
    return create_download(data=data, ctx=ctx, db=db)


@router.get("")
def list_tasks(
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
):
    rows = db.execute(
        select(Download)
        .where(Download.device_id == ctx.device.id)
        .order_by(Download.created_at.desc())
        .limit(min(max(limit, 1), 100))
        .offset(max(offset, 0))
    ).scalars().all()
    return [task_payload(db, task) for task in rows]


@router.get("/{task_id}")
def get_task(
    task_id: uuid.UUID,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    return task_payload(db, _owned_task(db, task_id, ctx.device.id))


@router.get("/{task_id}/progress")
def get_task_progress(
    task_id: uuid.UUID,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    task = _owned_task(db, task_id, ctx.device.id)
    return {
        "id": str(task.id),
        "status": task.status.value,
        "progress": task.progress,
        "progress_message": task.progress_message,
        "stage": task.stage,
        "queue_position": service.queue_position(db, task),
        "revision": task.revision,
        "updated_at": task.updated_at,
    }


@router.post("/{task_id}/cancel")
def cancel_task(
    task_id: uuid.UUID,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    task = _owned_task(db, task_id, ctx.device.id)
    if task.status in _TERMINAL:
        return {"status": task.status.value}
    now = datetime.now(timezone.utc)
    task.cancel_requested = True
    task.cancel_requested_at = now
    if task.status == DownloadStatus.queued:
        task.status = DownloadStatus.cancelled
        task.stage = "cancelled"
        task.progress_message = "Tarefa cancelada."
        task.error = None
        task.completed_at = now
        task.source_path = None
    db.commit()
    if task.status == DownloadStatus.cancelled:
        cleanup_task_files(str(task.id), str(task.device_id))
    notify_task_manager()
    publish_download_event(
        task,
        "download.cancelled"
        if task.status == DownloadStatus.cancelled
        else "download.cancel_requested",
    )
    return {
        "status": "cancelled"
        if task.status == DownloadStatus.cancelled else "cancelling",
    }


@router.post("/{task_id}/retry", status_code=202)
def retry_task(
    task_id: uuid.UUID,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    task = _owned_task(db, task_id, ctx.device.id)
    if task.status not in {DownloadStatus.failed, DownloadStatus.cancelled}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Somente tarefas encerradas com falha ou canceladas podem ser repetidas.",
        )
    if task.operation_type == "gif_upload" and not task.source_path:
        raise HTTPException(
            status.HTTP_410_GONE,
            "O arquivo enviado não está mais disponível; envie-o novamente.",
        )
    db.execute(select(func.pg_advisory_xact_lock(
        func.hashtext(f"download:{task.device_id}"),
    )))
    duplicate = service.active_duplicate(
        db, task.device_id, task.deduplication_key or "",
    )
    if duplicate is not None and duplicate.id != task.id:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Já existe uma tarefa igual em andamento.",
        )
    service.enforce_usage_limits(db, task.device_id)
    task.status = DownloadStatus.queued
    task.stage = "queued"
    task.progress = 0.0
    task.progress_message = "Aguardando na fila."
    task.error = None
    task.cancel_requested = False
    task.cancel_requested_at = None
    task.started_at = None
    task.completed_at = None
    task.heartbeat_at = None
    task.worker_id = None
    task.attempts = 0
    task.queued_at = datetime.now(timezone.utc)
    db.commit()
    notify_task_manager()
    publish_download_event(task, "download.queued")
    return {"id": str(task.id), "status": task.status.value}


@router.delete("/{task_id}", status_code=204)
def delete_task(
    task_id: uuid.UUID,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    task = _owned_task(db, task_id, ctx.device.id)
    if task.status not in _TERMINAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Cancele a tarefa antes de removê-la.",
        )
    private_storage.delete_download_file(db, task)
    cleanup_task_files(str(task.id), str(task.device_id))
    db.delete(task)
    db.commit()


@router.get("/{task_id}/download")
def download_task_result(
    task_id: uuid.UUID,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    task = _owned_task(db, task_id, ctx.device.id)
    if task.status != DownloadStatus.completed or not private_storage.download_has_file(task):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resultado não disponível.")
    token = service.make_delivery_token(
        task.id, task.device_id, task.revision, task.expires_at,
    )
    return RedirectResponse(
        url=f"/download/public/{task.id}?token={token}",
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )
