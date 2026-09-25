"""Regras de negócio dos downloads: limites por dispositivo e entrega segura."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from backend.api.config import get_settings
from backend.database.models import Device, Download, DownloadStatus, SystemCounter


SITE_DOWNLOADS_COUNTER_KEY = "site_downloads_completed"


def _native_mp4_video_choices(
    height: int | None,
    format_id: str | None,
) -> list[str]:
    exact_height = f"[height={height}]" if height else ""
    choices = [
        f"bv[vcodec^=avc1]{exact_height}",
    ]
    if format_id:
        choices.append(format_id)
    else:
        choices.append(f"bv{exact_height}")
    return choices


def build_format_spec(
    mode: str,
    height: int | None,
    format_id: str | None,
    output_format: str | None = None,
) -> str:
    """Monta o format_spec do yt-dlp no servidor, a partir da escolha do cliente."""
    if mode == "p":
        return ""  # foto: baixada direto por HTTP, não passa pelo seletor de formato do yt-dlp
    h = f"[height<={height}]" if height else ""
    if mode == "a":
        return f"{format_id}/bestaudio/best" if format_id else "bestaudio/best"
    if mode == "v":
        if (output_format or "mp4").lower() == "mp4":
            choices = _native_mp4_video_choices(height, format_id)
            return f"{'/'.join(choices)}/b{h}"
        if format_id:
            return f"{format_id}/bestvideo{h}/best{h}"
        return f"bestvideo{h}/best{h}"
    if (output_format or "mp4").lower() == "mp4":
        # Para MP4, tenta primeiro AVC/H.264 + M4A/AAC já prontos na mesma
        # resolução. Isso evita o transcode pesado sem reduzir a qualidade
        # escolhida; VP9/AV1 continuam como fallback quando não há AVC nativo.
        video_choices = _native_mp4_video_choices(height, format_id)
        video_group = "/".join(video_choices)
        return f"({video_group})+(ba[ext=m4a]/ba)/b{h}"
    if format_id:
        return f"{format_id}+bestaudio/bestvideo{h}+bestaudio/best{h}"
    return f"bestvideo{h}+bestaudio/best{h}"


def safe_filename(title: str | None, suffix: str) -> str:
    """Nome de arquivo amigável a partir do título, sem caracteres inválidos."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title or "").strip() or "video"
    return f"{name[:120]}{suffix}"


def safe_storage_file(file_path: str | None) -> Path | None:
    """Retorna somente arquivo regular dentro do storage configurado."""
    if not file_path:
        return None
    raw = Path(file_path)
    try:
        root = Path(get_settings().downloads_dir).resolve(strict=True)
        if raw.is_symlink():
            return None
        resolved = raw.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return None
        return resolved
    except (OSError, RuntimeError):
        return None


def _downloads_since(db: Session, device_id: uuid.UUID, since: datetime) -> int:
    return db.execute(
        select(func.count(Download.id)).where(Download.device_id == device_id, Download.created_at >= since)
    ).scalar_one()


def downloads_today(db: Session, device_id: uuid.UUID) -> int:
    since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return _downloads_since(db, device_id, since)


def active_downloads(db: Session, device_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count(Download.id)).where(
            Download.device_id == device_id,
            Download.status.in_([DownloadStatus.queued, DownloadStatus.processing]),
        )
    ).scalar_one()


def active_tasks(db: Session) -> int:
    return db.execute(
        select(func.count(Download.id)).where(
            Download.status.in_([DownloadStatus.queued, DownloadStatus.processing]),
            (Download.cache_role.is_(None)) | (Download.cache_role != "waiter"),
        )
    ).scalar_one()


def public_download_stats(db: Session) -> dict[str, int | datetime]:
    """Retorna somente totais agregados seguros para exibição pública."""
    downloads_in_queue, users_in_queue = db.execute(
        select(
            func.count(Download.id),
            func.count(func.distinct(Download.device_id)),
        ).where(Download.status == DownloadStatus.queued)
    ).one()
    counter = db.get(SystemCounter, SITE_DOWNLOADS_COUNTER_KEY)
    result = {
        "users_in_queue": int(users_in_queue or 0),
        "downloads_in_queue": int(downloads_in_queue or 0),
        "site_downloads_completed": int(counter.value if counter else 0),
        "updated_at": datetime.now(timezone.utc),
    }
    from backend.downloads.cache import metrics as cache_metrics
    result.update(cache_metrics(db))
    return result


def record_download_completion(db: Session, download: Download) -> bool:
    """Registra uma conclusão web uma única vez, na transação da tarefa."""
    if download.statistics_counted_at is not None:
        return False

    now = datetime.now(timezone.utc)
    claim = db.execute(
        update(Download)
        .where(
            Download.id == download.id,
            Download.statistics_counted_at.is_(None),
        )
        .values(statistics_counted_at=now)
        .execution_options(synchronize_session=False)
    )
    if claim.rowcount != 1:
        return False

    download.statistics_counted_at = now
    platform = db.execute(
        select(Device.platform).where(Device.id == download.device_id)
    ).scalar_one_or_none()
    if platform != "web":
        return False

    statement = pg_insert(SystemCounter).values(
        key=SITE_DOWNLOADS_COUNTER_KEY,
        value=1,
        updated_at=now,
    )
    db.execute(statement.on_conflict_do_update(
        index_elements=[SystemCounter.key],
        set_={
            "value": SystemCounter.value + 1,
            "updated_at": now,
        },
    ))
    return True


def build_deduplication_key(
    *,
    device_id: uuid.UUID,
    url: str,
    operation_type: str,
    mode: str,
    format_spec: str,
    output_format: str | None,
    conversion_options: str | None = None,
    content_digest: str | None = None,
) -> str:
    """Fingerprint estável de uma solicitação ativa, sem guardar payload em RAM."""
    canonical = "\n".join((
        str(device_id),
        url.strip(),
        operation_type,
        mode,
        format_spec,
        output_format or "",
        conversion_options or "",
        content_digest or "",
    ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def active_duplicate(
    db: Session,
    device_id: uuid.UUID,
    deduplication_key: str,
) -> Download | None:
    return db.execute(
        select(Download).where(
            Download.device_id == device_id,
            Download.deduplication_key == deduplication_key,
            Download.status.in_([DownloadStatus.queued, DownloadStatus.processing]),
        )
        .order_by(Download.created_at)
        .limit(1)
    ).scalar_one_or_none()


def queue_position(db: Session, download: Download) -> int | None:
    if download.status != DownloadStatus.queued:
        return None
    anchor = download.queued_at or download.created_at
    order_time = func.coalesce(Download.queued_at, Download.created_at)
    before = db.execute(
        select(func.count(Download.id)).where(
            Download.status == DownloadStatus.queued,
            (order_time < anchor)
            | (
                (order_time == anchor)
                & (Download.created_at < download.created_at)
            ),
        )
    ).scalar_one()
    return int(before) + 1


def enforce_usage_limits(
    db: Session,
    device_id: uuid.UUID,
    *,
    serialize: bool = False,
) -> None:
    settings = get_settings()
    if serialize:
        db.execute(select(func.pg_advisory_xact_lock(func.hashtext(
            f"download:{device_id}",
        ))))
    if downloads_today(db, device_id) >= settings.daily_downloads_per_device:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Limite diário de {settings.daily_downloads_per_device} downloads atingido.",
        )
    hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    if _downloads_since(db, device_id, hour_ago) >= settings.hourly_downloads_per_device:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Limite de downloads por hora atingido. Aguarde um pouco.",
        )
    max_active = min(
        settings.concurrent_downloads_per_device,
        settings.max_tasks_per_user,
    )
    if active_downloads(db, device_id) >= max_active:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Aguarde os downloads em andamento terminarem.",
        )
    if active_tasks(db) >= settings.max_queue_size:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "A fila de processamento está cheia. Tente novamente em instantes.",
            headers={"Retry-After": "30"},
        )


# ------------------------------------------------------------------
# URL de entrega assinada (HMAC): o cliente nunca vê o caminho real do
# arquivo nem acessa o storage diretamente. O link expira junto com o
# arquivo temporário.
# ------------------------------------------------------------------

def make_delivery_token(
    download_id: uuid.UUID,
    device_id: uuid.UUID,
    revision: int,
    expires_at: datetime | None = None,
) -> str:
    settings = get_settings()
    expires = int(time.time()) + settings.download_token_ttl_seconds
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        expires = min(expires, int(expires_at.timestamp()))
    payload = f"delivery:v2:{download_id}:{device_id}:{revision}:{expires}"
    sig = hmac.new(settings.jwt_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"v2.{expires}.{revision}.{sig}"


def verify_delivery_token(
    download_id: uuid.UUID,
    device_id: uuid.UUID,
    revision: int,
    token: str,
) -> bool:
    settings = get_settings()
    try:
        version, expires_str, token_revision, sig = token.split(".", 3)
        expires = int(expires_str)
        claimed_revision = int(token_revision)
    except (TypeError, ValueError):
        return False
    if version != "v2" or claimed_revision != revision or time.time() > expires:
        return False
    payload = f"delivery:v2:{download_id}:{device_id}:{revision}:{expires}"
    expected = hmac.new(settings.jwt_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)
