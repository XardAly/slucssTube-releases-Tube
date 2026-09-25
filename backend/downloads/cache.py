"""Cache persistente de saídas finais, coordenado pelo PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from backend.api.config import get_settings
from backend.database.models import (
    Download,
    DownloadStatus,
    MediaCache,
    StorageState,
    SystemCounter,
)
from backend.storage import service as storage_service
from backend.storage.base import (
    StorageAuthorizationError,
    StorageNotFoundError,
    StoragePermanentError,
    StorageTemporaryError,
)

logger = logging.getLogger(__name__)

CACHE_SCHEMA_VERSION = "2"
CACHE_ROLE_OWNER = "owner"
CACHE_ROLE_WAITER = "waiter"
CACHE_ROLE_HIT = "hit"
CACHE_ROLE_REUSED = "reused"

COUNTER_REQUESTS = "media_cache_requests"
COUNTER_HITS = "media_cache_hits"
COUNTER_MISSES = "media_cache_misses"
COUNTER_EXTERNAL = "media_cache_external_downloads"
COUNTER_SAVED = "media_cache_external_downloads_saved"
COUNTER_BYTES_REUSED = "media_cache_bytes_reused"
COUNTER_BYTES_EXTERNAL = "media_cache_bytes_downloaded_external"


class CacheValidationUnavailable(RuntimeError):
    """O Drive não pôde ser validado sem risco de duplicar processamento."""


@dataclass(frozen=True, slots=True)
class MediaIdentity:
    platform: str
    media_id: str


@dataclass(frozen=True, slots=True)
class CacheDecision:
    kind: str
    cache_id: uuid.UUID | None = None


def _uses_postgresql(db: Session) -> bool:
    try:
        return db.get_bind().dialect.name == "postgresql"
    except (AttributeError, RuntimeError):
        return False


def _lock_cache_key(db: Session, cache_key: str) -> None:
    if _uses_postgresql(db):
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"media-cache:{cache_key}"},
        )


def _bump_counter(db: Session, key: str, delta: int = 1) -> None:
    if delta <= 0:
        return
    now = datetime.now(timezone.utc)
    if _uses_postgresql(db):
        statement = pg_insert(SystemCounter).values(key=key, value=delta, updated_at=now)
        db.execute(statement.on_conflict_do_update(
            index_elements=[SystemCounter.key],
            set_={"value": SystemCounter.value + delta, "updated_at": now},
        ))
        return
    counter = db.get(SystemCounter, key)
    if counter is None:
        db.add(SystemCounter(key=key, value=delta, updated_at=now))
    else:
        counter.value += delta
        counter.updated_at = now


def _safe_external_id(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw or len(raw) > 200:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", raw):
        return None
    return raw


def _canonical_url_digest(url: str) -> str:
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "unknown").lower()
    port = f":{parsed.port}" if parsed.port and parsed.port not in {80, 443} else ""
    ignored = {"fbclid", "gclid", "si", "feature", "utm_source", "utm_medium", "utm_campaign"}
    query = [
        (key, value)
        for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
        if key.lower() not in ignored
        for value in values
    ]
    canonical = urlunsplit(("https", f"{host}{port}", parsed.path.rstrip("/") or "/", urlencode(sorted(query)), ""))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def identify_media(url: str, *, extracted_id: object = None) -> MediaIdentity:
    """Extrai IDs estáveis das plataformas aceitas sem executar yt-dlp."""
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "unknown").lower().removeprefix("www.")
    path = parsed.path
    query = parse_qs(parsed.query)

    if host in {"youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}:
        platform = "youtube"
        candidate = path.strip("/").split("/")[0] if host == "youtu.be" else None
        if not candidate:
            candidate = (query.get("v") or [None])[0]
        if not candidate:
            match = re.search(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]+)", path)
            candidate = match.group(1) if match else None
    elif host.endswith("tiktok.com"):
        platform = "tiktok"
        match = re.search(r"/video/(\d+)", path)
        candidate = match.group(1) if match else None
    elif host.endswith("instagram.com"):
        platform = "instagram"
        match = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", path)
        candidate = match.group(1) if match else None
    elif host.endswith("pinterest.com") or host == "pin.it":
        platform = "pinterest"
        match = re.search(r"/pin/(\d+)", path)
        candidate = match.group(1) if match else None
    elif host in {"twitter.com", "x.com"} or host.endswith((".twitter.com", ".x.com")):
        platform = "twitter"
        match = re.search(r"/status/(\d+)", path)
        candidate = match.group(1) if match else None
    else:
        platform = re.sub(r"[^a-z0-9.-]", "", host)[:32] or "unknown"
        candidate = None

    trusted = _safe_external_id(extracted_id)
    media_id = trusted or _safe_external_id(candidate)
    if media_id is None:
        media_id = f"urlsha256:{_canonical_url_digest(url)}"
    return MediaIdentity(platform=platform, media_id=media_id)


def _canonical_conversion_options(raw: str | None) -> object:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if isinstance(payload, dict):
        payload.pop("result", None)
    return payload


def build_cache_identity(
    download: Download,
    *,
    extracted_id: object = None,
) -> tuple[MediaIdentity, str, str, str]:
    identity = identify_media(download.url, extracted_id=extracted_id)
    profile = (
        "gif-palette-v1" if download.mode == "gif"
        else "h264-aac-yuv420p-crf18-veryfast-v2"
        if (download.output_format or "").lower() == "mp4" and download.mode in {"va", "v"}
        else "native-v1"
    )
    parameters = {
        "schema": CACHE_SCHEMA_VERSION,
        "operation": download.operation_type,
        "mode": download.mode,
        "output_format": (download.output_format or "").lower(),
        "format_spec": download.format_spec,
        "conversion": _canonical_conversion_options(download.conversion_options),
        "profile": profile,
    }
    parameters_json = json.dumps(
        parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    fingerprint = hashlib.sha256(parameters_json.encode("utf-8")).hexdigest()
    cache_key = hashlib.sha256(
        f"{identity.platform}\0{identity.media_id}\0{fingerprint}".encode("utf-8"),
    ).hexdigest()
    return identity, fingerprint, cache_key, parameters_json


def is_cacheable(download: Download) -> bool:
    settings = get_settings()
    if not settings.media_cache_enabled or settings.storage_backend != "google_drive":
        return False
    if download.operation_type not in {"download", "gif_url"}:
        return False
    if download.mode == "gif" and not settings.storage_keep_generated_gifs:
        return False
    return True


def _copy_cached_output(cache: MediaCache, download: Download, *, role: str) -> None:
    now = datetime.now(timezone.utc)
    settings = get_settings()
    download.media_cache_id = cache.id
    download.cache_role = role
    download.status = DownloadStatus.completed
    download.progress = 100.0
    download.stage = "completed"
    download.progress_message = "Arquivo reutilizado do cache."
    download.title = cache.title
    download.file_size = cache.file_size
    download.file_path = None
    download.storage_backend = "google_drive"
    download.storage_state = StorageState.completed.value
    download.storage_key = cache.storage_key
    download.storage_reserved_key = None
    download.storage_idempotency_key = None
    download.remote_parent_id = cache.remote_parent_id
    download.remote_size = cache.file_size
    download.remote_checksum = cache.remote_checksum
    download.remote_mime_type = cache.remote_mime_type
    download.remote_uploaded_at = cache.remote_uploaded_at
    download.local_delete_pending = False
    download.storage_error = None
    download.conversion_options = cache.conversion_options or download.conversion_options
    download.processing_time = 0.0
    download.completed_at = now
    download.heartbeat_at = now
    download.expires_at = now + timedelta(hours=settings.download_file_ttl_hours)


def _cache_remote_is_valid(cache: MediaCache) -> bool:
    if cache.storage_backend != "google_drive" or not cache.storage_key:
        return False
    try:
        metadata = storage_service.get_remote_metadata(cache.storage_key)
    except StorageNotFoundError:
        return False
    except (StorageTemporaryError, StorageAuthorizationError, StoragePermanentError) as exc:
        raise CacheValidationUnavailable(
            "O cache remoto não pôde ser validado agora."
        ) from exc
    if cache.file_size is not None and metadata.size != cache.file_size:
        return False
    cache.validated_at = datetime.now(timezone.utc)
    cache.file_size = metadata.size
    cache.remote_checksum = metadata.checksum
    cache.remote_mime_type = metadata.mime_type
    cache.remote_parent_id = metadata.parent_id
    return True


def _invalidate(cache: MediaCache, reason: str) -> None:
    cache.state = "invalid"
    cache.last_error = reason[:120]
    cache.storage_backend = None
    cache.storage_key = None
    cache.storage_reserved_key = None
    cache.remote_parent_id = None
    cache.remote_checksum = None
    cache.remote_mime_type = None
    cache.remote_uploaded_at = None
    cache.file_size = None
    cache.ready_at = None
    cache.validated_at = None


def reserve_request(
    db: Session,
    download: Download,
    *,
    extracted_id: object = None,
) -> CacheDecision:
    """Reserva um único produtor ou conecta o pedido a uma saída existente."""
    if not is_cacheable(download):
        return CacheDecision("bypass")
    if download.id is None:
        download.id = uuid.uuid4()
    identity, fingerprint, cache_key, parameters = build_cache_identity(
        download, extracted_id=extracted_id,
    )
    _lock_cache_key(db, cache_key)
    cache = db.execute(
        select(MediaCache).where(MediaCache.cache_key == cache_key).with_for_update(),
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if cache is not None and cache.state == "ready":
        if _cache_remote_is_valid(cache):
            _copy_cached_output(cache, download, role=CACHE_ROLE_HIT)
            cache.request_count += 1
            cache.hit_count += 1
            cache.last_accessed_at = now
            cache.bytes_reused += int(cache.file_size or 0)
            _bump_counter(db, COUNTER_REQUESTS)
            _bump_counter(db, COUNTER_HITS)
            _bump_counter(db, COUNTER_SAVED)
            _bump_counter(db, COUNTER_BYTES_REUSED, int(cache.file_size or 0))
            logger.info(
                "media_cache_hit platform=%s media_id=%s bytes=%s",
                cache.platform, cache.media_id, cache.file_size or 0,
            )
            return CacheDecision("hit", cache.id)
        _invalidate(cache, "remote_missing_or_mismatched")
        logger.warning(
            "media_cache_invalid platform=%s media_id=%s",
            cache.platform, cache.media_id,
        )

    if cache is not None and cache.state == "processing":
        owner = db.get(Download, cache.owner_download_id) if cache.owner_download_id else None
        if owner is not None and owner.status in {DownloadStatus.queued, DownloadStatus.processing}:
            download.media_cache_id = cache.id
            download.cache_role = CACHE_ROLE_WAITER
            download.status = DownloadStatus.processing
            download.progress = cache.progress
            download.stage = cache.stage or "processing"
            download.progress_message = "Aguardando o processamento compartilhado."
            download.started_at = now
            download.heartbeat_at = now
            cache.request_count += 1
            cache.last_accessed_at = now
            _bump_counter(db, COUNTER_REQUESTS)
            logger.info(
                "media_cache_join platform=%s media_id=%s owner=%s",
                cache.platform, cache.media_id, str(cache.owner_download_id)[:8],
            )
            return CacheDecision("wait", cache.id)
        cache.state = "failed"
        cache.last_error = "owner_terminal_or_missing"
        cache.failure_count += 1

    if cache is None:
        cache = MediaCache(
            id=uuid.uuid4(),
            cache_key=cache_key,
            platform=identity.platform,
            media_id=identity.media_id,
            operation_type=download.operation_type,
            request_fingerprint=fingerprint,
            request_parameters=parameters,
            state="processing",
            mode=download.mode,
            output_format=download.output_format,
            request_count=1,
            processing_started_at=now,
            heartbeat_at=now,
            last_accessed_at=now,
        )
        db.add(cache)
        # A linha precisa existir antes do INSERT do download que a referencia.
        # Não há relationship() entre Download e MediaCache, então o unit of work
        # não conhece a dependência e ordena por tabela: `downloads` sai antes de
        # `media_cache` e o commit morre em ForeignKeyViolation.
        db.flush()
    else:
        cache.platform = identity.platform
        cache.media_id = identity.media_id
        cache.operation_type = download.operation_type
        cache.request_fingerprint = fingerprint
        cache.request_parameters = parameters
        cache.state = "processing"
        cache.mode = download.mode
        cache.output_format = download.output_format
        cache.request_count += 1
        cache.progress = 0.0
        cache.stage = "queued"
        cache.processing_started_at = now
        cache.heartbeat_at = now
        cache.last_accessed_at = now
        cache.last_error = None
        cache.storage_backend = None
        cache.storage_key = None
        cache.storage_reserved_key = None
        cache.remote_parent_id = None
        cache.remote_checksum = None
        cache.remote_mime_type = None
        cache.remote_uploaded_at = None
        cache.file_size = None
        cache.ready_at = None
        cache.validated_at = None

    cache.owner_download_id = download.id
    download.media_cache_id = cache.id
    download.cache_role = CACHE_ROLE_OWNER
    _bump_counter(db, COUNTER_REQUESTS)
    _bump_counter(db, COUNTER_MISSES)
    logger.info(
        "media_cache_miss platform=%s media_id=%s",
        cache.platform, cache.media_id,
    )
    return CacheDecision("owner", cache.id)


def propagate_progress(db: Session, owner: Download) -> None:
    if owner.cache_role != CACHE_ROLE_OWNER or not owner.media_cache_id:
        return
    now = datetime.now(timezone.utc)
    cache = db.get(MediaCache, owner.media_cache_id)
    if cache is None or cache.owner_download_id != owner.id or cache.state != "processing":
        return
    cache.progress = float(owner.progress or 0)
    cache.stage = owner.stage
    cache.heartbeat_at = owner.heartbeat_at or now
    cache.updated_at = now
    db.execute(
        update(Download)
        .where(
            Download.media_cache_id == cache.id,
            Download.cache_role == CACHE_ROLE_WAITER,
            Download.status == DownloadStatus.processing,
        )
        .values(
            progress=cache.progress,
            stage=cache.stage,
            progress_message="Processamento compartilhado em andamento.",
            heartbeat_at=now,
            updated_at=now,
            revision=Download.revision + 1,
        )
        .execution_options(synchronize_session=False)
    )


def complete_owner(db: Session, owner: Download) -> list[Download]:
    """Publica uma saída Drive no cache e conclui todos os seguidores."""
    if owner.cache_role != CACHE_ROLE_OWNER or not owner.media_cache_id:
        return []
    cache = db.execute(
        select(MediaCache).where(MediaCache.id == owner.media_cache_id).with_for_update(),
    ).scalar_one_or_none()
    if cache is None or cache.owner_download_id != owner.id:
        return []
    if cache.state == "ready" and cache.storage_key == owner.storage_key:
        return []
    if owner.storage_backend != "google_drive" or not owner.storage_key:
        cache.state = "failed"
        cache.failure_count += 1
        cache.last_error = "output_not_persisted_in_drive"
        return fail_waiters(db, cache, "O resultado compartilhado não pôde ser armazenado.")

    now = datetime.now(timezone.utc)
    cache.state = "ready"
    cache.progress = 100.0
    cache.stage = "completed"
    cache.title = owner.title
    cache.mode = owner.mode
    cache.output_format = owner.output_format
    cache.conversion_options = owner.conversion_options
    cache.storage_backend = "google_drive"
    cache.storage_key = owner.storage_key
    cache.storage_reserved_key = owner.storage_reserved_key
    cache.remote_parent_id = owner.remote_parent_id
    cache.remote_checksum = owner.remote_checksum
    cache.remote_mime_type = owner.remote_mime_type
    cache.remote_uploaded_at = owner.remote_uploaded_at
    cache.file_size = owner.file_size or owner.remote_size
    cache.external_downloads += 1
    cache.bytes_downloaded_external += int(cache.file_size or 0)
    cache.ready_at = now
    cache.last_accessed_at = now
    cache.validated_at = now
    cache.heartbeat_at = now
    cache.last_error = None

    followers = db.execute(
        select(Download).where(
            Download.media_cache_id == cache.id,
            Download.cache_role == CACHE_ROLE_WAITER,
            Download.status == DownloadStatus.processing,
        ).with_for_update(skip_locked=True)
    ).scalars().all()
    from backend.downloads.service import record_download_completion

    for follower in followers:
        _copy_cached_output(cache, follower, role=CACHE_ROLE_REUSED)
        record_download_completion(db, follower)
    reused = len(followers)
    reused_bytes = reused * int(cache.file_size or 0)
    cache.hit_count += reused
    cache.bytes_reused += reused_bytes
    _bump_counter(db, COUNTER_EXTERNAL)
    _bump_counter(db, COUNTER_BYTES_EXTERNAL, int(cache.file_size or 0))
    _bump_counter(db, COUNTER_HITS, reused)
    _bump_counter(db, COUNTER_SAVED, reused)
    _bump_counter(db, COUNTER_BYTES_REUSED, reused_bytes)
    logger.info(
        "media_cache_ready platform=%s media_id=%s bytes=%s followers=%s",
        cache.platform, cache.media_id, cache.file_size or 0, reused,
    )
    return followers


def fail_waiters(db: Session, cache: MediaCache, message: str) -> list[Download]:
    now = datetime.now(timezone.utc)
    followers = db.execute(
        select(Download).where(
            Download.media_cache_id == cache.id,
            Download.cache_role == CACHE_ROLE_WAITER,
            Download.status == DownloadStatus.processing,
        ).with_for_update(skip_locked=True)
    ).scalars().all()
    for follower in followers:
        follower.status = DownloadStatus.failed
        follower.stage = "failed"
        follower.progress_message = "Não foi possível concluir a tarefa compartilhada."
        follower.error = message[:500]
        follower.completed_at = now
        follower.heartbeat_at = now
    return followers


def fail_owner(db: Session, owner: Download, error: object) -> list[Download]:
    if owner.cache_role != CACHE_ROLE_OWNER or not owner.media_cache_id:
        return []
    cache = db.execute(
        select(MediaCache).where(MediaCache.id == owner.media_cache_id).with_for_update(),
    ).scalar_one_or_none()
    if cache is None or cache.owner_download_id != owner.id or cache.state != "processing":
        return []
    cache.state = "failed"
    cache.failure_count += 1
    cache.last_error = type(error).__name__[:120] if isinstance(error, BaseException) else str(error)[:120]
    cache.heartbeat_at = datetime.now(timezone.utc)
    logger.warning(
        "media_cache_failed platform=%s media_id=%s type=%s",
        cache.platform, cache.media_id, cache.last_error,
    )
    return fail_waiters(db, cache, "Não foi possível processar este vídeo.")


def invalidate_remote_entry(db: Session, cache: MediaCache, reason: str) -> None:
    _invalidate(cache, reason)
    db.execute(
        update(Download)
        .where(
            Download.media_cache_id == cache.id,
            Download.status == DownloadStatus.completed,
        )
        .values(
            storage_state=StorageState.remote_missing.value,
            storage_error=reason[:120],
            updated_at=datetime.now(timezone.utc),
            revision=Download.revision + 1,
        )
        .execution_options(synchronize_session=False)
    )
    fail_waiters(db, cache, "O arquivo compartilhado não está mais disponível.")


def recover_stale_entries() -> int:
    """Reconcilia produtores interrompidos sem iniciar trabalho duplicado."""
    from backend.database.session import SessionLocal

    db = SessionLocal()
    affected = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=get_settings().media_cache_processing_stale_seconds,
        )
        rows = db.execute(
            select(MediaCache).where(
                MediaCache.state == "processing",
                func.coalesce(MediaCache.heartbeat_at, MediaCache.updated_at) <= cutoff,
            ).with_for_update(skip_locked=True).limit(100)
        ).scalars().all()
        for cache in rows:
            owner = db.get(Download, cache.owner_download_id) if cache.owner_download_id else None
            owner_heartbeat = owner.heartbeat_at if owner is not None else None
            if owner_heartbeat is not None and owner_heartbeat.tzinfo is None:
                owner_heartbeat = owner_heartbeat.replace(tzinfo=timezone.utc)
            if (
                owner is not None
                and owner.status in {DownloadStatus.queued, DownloadStatus.processing}
                and owner_heartbeat is not None
                and owner_heartbeat > cutoff
            ):
                cache.heartbeat_at = owner_heartbeat
                continue
            if owner is not None and owner.status == DownloadStatus.completed:
                complete_owner(db, owner)
                affected += 1
                continue
            if owner is not None and owner.status == DownloadStatus.queued:
                cache.heartbeat_at = datetime.now(timezone.utc)
                continue
            if owner is not None and owner.status == DownloadStatus.processing:
                if owner.attempts > get_settings().task_max_retries:
                    owner.status = DownloadStatus.failed
                    owner.stage = "failed"
                    owner.error = "A tarefa foi interrompida repetidamente."
                    owner.completed_at = datetime.now(timezone.utc)
                    cache.state = "failed"
                    cache.failure_count += 1
                    cache.last_error = "stale_retry_limit"
                    fail_waiters(db, cache, "O processamento compartilhado foi interrompido.")
                    affected += 1
                    continue
                owner.status = DownloadStatus.queued
                owner.stage = "retrying"
                owner.progress_message = "Tarefa recuperada após interrupção."
                owner.worker_id = None
                owner.heartbeat_at = None
                owner.queued_at = datetime.now(timezone.utc)
                cache.heartbeat_at = datetime.now(timezone.utc)
                affected += 1
                continue
            cache.state = "failed"
            cache.failure_count += 1
            cache.last_error = "stale_owner_missing_or_terminal"
            fail_waiters(db, cache, "O processamento compartilhado foi interrompido.")
            affected += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    if affected:
        from backend.tasks.manager import notify_task_manager
        notify_task_manager()
    return affected


def metrics(db: Session) -> dict[str, int | float]:
    keys = (
        COUNTER_REQUESTS, COUNTER_HITS, COUNTER_MISSES,
        COUNTER_EXTERNAL, COUNTER_SAVED, COUNTER_BYTES_REUSED,
        COUNTER_BYTES_EXTERNAL,
    )
    result = db.execute(select(SystemCounter).where(SystemCounter.key.in_(keys)))
    # Mantém compatibilidade com sessões mínimas usadas por consumidores/testes
    # antigos; numa Session SQLAlchemy real o caminho abaixo sempre existe.
    rows = result.scalars().all() if hasattr(result, "scalars") else []
    values = {row.key: int(row.value) for row in rows}
    requests = values.get(COUNTER_REQUESTS, 0)
    hits = values.get(COUNTER_HITS, 0)
    return {
        "total_requests": requests,
        "cache_requests": requests,
        "cache_hits": hits,
        "cache_misses": values.get(COUNTER_MISSES, 0),
        "external_downloads": values.get(COUNTER_EXTERNAL, 0),
        "external_downloads_saved": values.get(COUNTER_SAVED, 0),
        "cache_bytes_reused": values.get(COUNTER_BYTES_REUSED, 0),
        "bytes_downloaded_external": values.get(COUNTER_BYTES_EXTERNAL, 0),
        "cache_hit_ratio": round(hits / requests, 4) if requests else 0.0,
    }
