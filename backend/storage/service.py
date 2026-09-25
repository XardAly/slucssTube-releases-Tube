"""Orquestra banco, arquivo local e backend remoto sem estados intermediários ambíguos."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.api.config import get_settings
from backend.database.models import Download, StorageState
from backend.storage.base import (
    StorageAuthorizationError,
    StorageMetadata,
    StoragePermanentError,
    StorageTemporaryError,
)
from backend.storage.manager import create_storage_backend
from backend.storage.google_drive import GoogleDriveStorageBackend

logger = logging.getLogger(__name__)
_UPLOAD_LOCK_BASE = 8_706_410_000
_FALLBACK_QUOTA_LOCK = 8_706_419_999
_REMOTE_AUDIT_LOCK = 8_706_420_001


def output_kind(download: Download) -> str:
    if download.mode == "gif":
        return "gif"
    if download.mode == "a":
        return "audio"
    if download.mode == "p":
        return "photo"
    return "video"


def download_has_file(download: Download) -> bool:
    if download.expires_at:
        expires_at = download.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            return False
    if download.file_path:
        return True
    return bool(
        download.storage_backend == "google_drive"
        and download.storage_key
        and download.storage_state in {
            StorageState.remote_ready.value,
            StorageState.completed.value,
            StorageState.local_delete_pending.value,
        }
    )


async def _upload_once(
    path: Path,
    *,
    kind: str,
    idempotency_key: str,
    mime_type: str,
    reserved_key: str | None,
    cancel_requested,
) -> StorageMetadata:
    backend = create_storage_backend()
    try:
        return await backend.upload_file(
            path,
            kind=kind,
            idempotency_key=idempotency_key,
            mime_type=mime_type,
            reserved_key=reserved_key,
            cancel_requested=cancel_requested,
        )
    finally:
        await backend.close()


async def _reserve_once() -> str:
    backend = create_storage_backend(backend_name="google_drive")
    try:
        reserve = getattr(backend, "reserve_file_id", None)
        if not callable(reserve):
            raise StoragePermanentError("Backend remoto indisponível.")
        return await reserve()
    finally:
        await backend.close()


async def _remote_metadata_once(key: str) -> StorageMetadata:
    backend = create_storage_backend(backend_name="google_drive")
    try:
        return await backend.get_metadata(key)
    finally:
        await backend.close()


def get_remote_metadata(key: str) -> StorageMetadata:
    """Valida uma referência privada sem baixar nem bufferizar a mídia."""
    if not key:
        raise StoragePermanentError("Chave remota ausente.")
    timeout = get_settings().media_cache_validation_timeout_seconds
    try:
        return asyncio.run(asyncio.wait_for(_remote_metadata_once(key), timeout=timeout))
    except TimeoutError as exc:
        raise StorageTemporaryError("Validação remota excedeu o tempo limite.") from exc


def _fallback_bytes(db: Session, *, exclude_id: object | None = None) -> int:
    statement = select(func.coalesce(func.sum(Download.file_size), 0)).where(
            Download.storage_state.in_({
                StorageState.local_fallback.value,
                StorageState.remote_uploading.value,
            }),
            Download.file_path.is_not(None),
        )
    if exclude_id is not None:
        statement = statement.where(Download.id != exclude_id)
    return int(db.execute(statement).scalar_one())


def _uses_postgresql(db: Session) -> bool:
    try:
        return db.get_bind().dialect.name == "postgresql"
    except (AttributeError, RuntimeError):
        return False


@contextmanager
def _upload_slot(db: Session, identity: object, slots: int):
    """Limite global entre processos sem manter transação SQL ociosa."""
    if not _uses_postgresql(db):
        yield
        return
    slots = max(1, slots)
    start = int(getattr(identity, "int", 0)) % slots
    deadline = time.monotonic() + 30
    connection = db.get_bind().connect().execution_options(isolation_level="AUTOCOMMIT")
    acquired_key: int | None = None
    try:
        while time.monotonic() < deadline and acquired_key is None:
            for offset in range(slots):
                lock_key = _UPLOAD_LOCK_BASE + ((start + offset) % slots)
                acquired = connection.execute(
                    text("SELECT pg_try_advisory_lock(:lock_key)"),
                    {"lock_key": lock_key},
                ).scalar_one()
                if acquired:
                    acquired_key = lock_key
                    break
            if acquired_key is None:
                time.sleep(0.25)
        if acquired_key is None:
            raise StorageTemporaryError("Limite global de uploads remotos atingido.")
        yield
    finally:
        if acquired_key is not None:
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": acquired_key},
                )
            except Exception as exc:  # noqa: BLE001 - fechar conexão também libera lock de sessão
                logger.warning(
                    "storage_upload_lock_release_failed type=%s", type(exc).__name__,
                )
        connection.close()


def _lock_fallback_quota(db: Session) -> None:
    if _uses_postgresql(db):
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _FALLBACK_QUOTA_LOCK},
        )


def acquire_remote_audit_lock(db: Session):
    """Retorna um release idempotente ou None quando outra auditoria está ativa."""
    if not _uses_postgresql(db):
        return lambda: None
    connection = db.get_bind().connect().execution_options(isolation_level="AUTOCOMMIT")
    acquired = connection.execute(
        text("SELECT pg_try_advisory_lock(:lock_key)"),
        {"lock_key": _REMOTE_AUDIT_LOCK},
    ).scalar_one()
    if not acquired:
        connection.close()
        return None
    released = False

    def release() -> None:
        nonlocal released
        if released:
            return
        released = True
        try:
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": _REMOTE_AUDIT_LOCK},
            )
        finally:
            connection.close()

    return release


def finalize_confirmed_remote_output(
    db: Session,
    download: Download,
    local_path: Path | None,
) -> StorageMetadata:
    """Conclui um upload já confirmado no banco, mesmo sem a cópia local."""
    if not (
        download.storage_backend == "google_drive"
        and download.storage_key
        and download.remote_size is not None
        and download.storage_state in {
            StorageState.remote_ready.value,
            StorageState.local_delete_pending.value,
        }
    ):
        raise StoragePermanentError("Metadados remotos confirmados estão incompletos.")
    if local_path is not None:
        try:
            local_path.unlink(missing_ok=True)
        except OSError as exc:
            download.storage_state = StorageState.local_delete_pending.value
            download.storage_error = "LocalDeleteError"
            db.commit()
            raise StorageTemporaryError(
                "Arquivo remoto salvo, mas a cópia local aguarda limpeza."
            ) from exc

    now = datetime.now(timezone.utc)
    download.file_path = None
    download.local_delete_pending = False
    download.storage_state = StorageState.completed.value
    download.storage_error = None
    download.expires_at = download.expires_at or (
        now + timedelta(hours=get_settings().download_file_ttl_hours)
    )
    db.commit()
    return StorageMetadata(
        backend="google_drive",
        key=download.storage_key,
        size=download.remote_size,
        checksum=download.remote_checksum,
        mime_type=download.remote_mime_type or "application/octet-stream",
        parent_id=download.remote_parent_id,
        uploaded_at=download.remote_uploaded_at or now,
    )


def persist_completed_output(
    db: Session,
    download: Download,
    path: Path,
    *,
    cancel_requested=None,
) -> StorageMetadata:
    """Confirma o backend e somente então permite remover o arquivo local."""
    settings = get_settings()
    started = time.monotonic()
    previous_state = download.storage_state
    previous_expires_at = download.expires_at
    if (
        download.storage_backend == "google_drive"
        and download.storage_key
        and download.storage_state in {
            StorageState.remote_ready.value,
            StorageState.local_delete_pending.value,
        }
    ):
        return finalize_confirmed_remote_output(
            db, download, path if path.exists() else None,
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise StoragePermanentError("Arquivo final local não encontrado.") from exc

    kind = output_kind(download)
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    now = datetime.now(timezone.utc)
    download.file_path = str(path)
    download.file_size = size
    download.expires_at = now + timedelta(hours=settings.download_file_ttl_hours)
    download.storage_error = None

    target_backend = settings.storage_backend
    if kind == "gif" and not settings.storage_keep_generated_gifs:
        target_backend = "local"

    if target_backend == "local":
        download.storage_backend = "local"
        download.storage_state = StorageState.completed.value
        download.storage_key = str(path)
        download.remote_parent_id = None
        download.remote_size = None
        download.remote_checksum = None
        download.remote_mime_type = mime_type
        download.local_delete_pending = False
        db.commit()
        logger.info(
            "storage_upload backend=local kind=%s bytes=%s duration_seconds=%.3f state=completed",
            kind, size, time.monotonic() - started,
        )
        return StorageMetadata(
            backend="local",
            key=str(path),
            size=size,
            checksum=None,
            mime_type=mime_type,
            parent_id=None,
            uploaded_at=now,
        )

    try:
        if not download.storage_idempotency_key:
            download.storage_idempotency_key = f"{download.id}:{kind}:{max(1, download.revision)}"
        if not download.storage_reserved_key:
            download.storage_reserved_key = asyncio.run(_reserve_once())
        download.storage_backend = "google_drive"
        download.storage_state = StorageState.remote_uploading.value
        download.local_delete_pending = False
        db.commit()
        with _upload_slot(
            db, download.id, getattr(settings, "google_drive_max_concurrent_uploads", 2),
        ):
            metadata = asyncio.run(_upload_once(
                path,
                kind=kind,
                idempotency_key=download.storage_idempotency_key,
                mime_type=mime_type,
                reserved_key=download.storage_reserved_key,
                cancel_requested=cancel_requested,
            ))
    except StorageTemporaryError as exc:
        download.storage_error = type(exc).__name__
        if settings.storage_allow_local_fallback:
            _lock_fallback_quota(db)
            limit = settings.storage_local_fallback_max_size_mb * 1024 * 1024
            if _fallback_bytes(db, exclude_id=download.id) + size <= limit:
                download.storage_backend = "local"
                download.storage_state = StorageState.local_fallback.value
                download.storage_key = str(path)
                download.expires_at = (
                    previous_expires_at
                    if previous_state == StorageState.local_fallback.value and previous_expires_at
                    else now + timedelta(
                        seconds=(
                            getattr(settings, "storage_local_fallback_ttl_seconds", None)
                            if getattr(settings, "storage_local_fallback_ttl_seconds", None) is not None
                            else settings.storage_recovery_ttl_hours * 3600
                        )
                    )
                )
                db.commit()
                logger.warning(
                    "storage_upload backend=local kind=%s bytes=%s duration_seconds=%.3f state=fallback error=%s",
                    kind, size, time.monotonic() - started, type(exc).__name__,
                )
                return StorageMetadata(
                    backend="local",
                    key=str(path),
                    size=size,
                    checksum=None,
                    mime_type=mime_type,
                    parent_id=None,
                    uploaded_at=now,
                )
        db.commit()
        raise
    except StorageAuthorizationError as exc:
        download.storage_error = type(exc).__name__
        if settings.storage_allow_local_fallback:
            _lock_fallback_quota(db)
            limit = settings.storage_local_fallback_max_size_mb * 1024 * 1024
            if _fallback_bytes(db, exclude_id=download.id) + size <= limit:
                download.storage_backend = "local"
                download.storage_state = StorageState.local_fallback.value
                download.storage_key = str(path)
                fallback_seconds = getattr(settings, "storage_local_fallback_ttl_seconds", None)
                download.expires_at = now + timedelta(
                    seconds=(
                        fallback_seconds
                        if fallback_seconds is not None
                        else settings.storage_recovery_ttl_hours * 3600
                    )
                )
        db.commit()
        raise
    except StoragePermanentError as exc:
        download.storage_error = type(exc).__name__
        db.commit()
        raise

    if metadata.size != size:
        download.storage_error = "StorageSizeMismatch"
        db.commit()
        raise StoragePermanentError("Tamanho remoto divergente do arquivo local.")

    download.storage_key = metadata.key
    download.remote_parent_id = metadata.parent_id
    download.remote_size = metadata.size
    download.remote_checksum = metadata.checksum
    download.remote_mime_type = metadata.mime_type
    download.remote_uploaded_at = metadata.uploaded_at
    download.storage_state = StorageState.remote_ready.value
    download.local_delete_pending = True
    db.commit()

    finalize_confirmed_remote_output(db, download, path)
    logger.info(
        "storage_upload backend=google_drive kind=%s bytes=%s duration_seconds=%.3f state=completed",
        kind, size, time.monotonic() - started,
    )
    return metadata


def persist_source_video(
    db: Session,
    download: Download,
    path: Path,
    *,
    downloaded_source: bool,
    cancel_requested=None,
) -> StorageMetadata | None:
    """Preserva a fonte somente quando a política correspondente foi ativada."""
    settings = get_settings()
    keep = (
        (
            getattr(settings, "storage_keep_downloaded_source_videos", None)
            if getattr(settings, "storage_keep_downloaded_source_videos", None) is not None
            else settings.storage_keep_downloaded_videos
        )
        if downloaded_source
        else (
            getattr(settings, "storage_keep_source_uploads", None)
            if getattr(settings, "storage_keep_source_uploads", None) is not None
            else settings.storage_keep_source_videos
        )
    )
    if not keep or settings.storage_backend != "google_drive":
        return None
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise StoragePermanentError("Fonte local não disponível para retenção.") from exc
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    source_kind = "downloaded_source" if downloaded_source else "uploaded_source"
    started = time.monotonic()
    try:
        if not download.source_storage_idempotency_key:
            download.source_storage_idempotency_key = f"{download.id}:{source_kind}:1"
        if not download.source_storage_reserved_key:
            download.source_storage_reserved_key = asyncio.run(_reserve_once())
        db.commit()
        with _upload_slot(
            db, download.id, getattr(settings, "google_drive_max_concurrent_uploads", 2),
        ):
            metadata = asyncio.run(_upload_once(
                path,
                kind="video",
                idempotency_key=download.source_storage_idempotency_key,
                mime_type=mime_type,
                reserved_key=download.source_storage_reserved_key,
                cancel_requested=cancel_requested,
            ))
    except (StorageTemporaryError, StoragePermanentError) as exc:
        download.storage_error = type(exc).__name__
        db.commit()
        raise
    if metadata.size != size:
        download.storage_error = "SourceStorageSizeMismatch"
        db.commit()
        raise StoragePermanentError("Tamanho remoto da fonte divergente.")
    download.source_storage_backend = "google_drive"
    download.source_storage_key = metadata.key
    download.source_remote_parent_id = metadata.parent_id
    download.source_remote_size = metadata.size
    download.source_remote_checksum = metadata.checksum
    download.source_remote_mime_type = metadata.mime_type
    download.source_remote_uploaded_at = metadata.uploaded_at
    download.storage_error = None
    db.commit()
    logger.info(
        "storage_source_upload backend=google_drive kind=%s bytes=%s duration_seconds=%.3f",
        source_kind, size, time.monotonic() - started,
    )
    return metadata


def delete_download_file(db: Session, download: Download) -> bool:
    """Exclusão idempotente; nunca alcança objetos fora do registro/pasta gerenciada."""
    download.storage_state = StorageState.delete_pending.value
    db.commit()
    removed = False
    remote_keys = []
    # Uma saída ligada ao media_cache é apenas uma referência: o objeto remoto
    # pertence ao cache permanente e não ao histórico temporário do usuário.
    shared_cache_output = bool(
        download.media_cache_id
        and download.storage_backend == "google_drive"
        and download.storage_key
    )
    if (
        download.storage_backend == "google_drive"
        and download.storage_key
        and not shared_cache_output
    ):
        remote_keys.append(download.storage_key)
    if download.source_storage_backend == "google_drive" and download.source_storage_key:
        remote_keys.append(download.source_storage_key)
    if remote_keys:
        errors = asyncio.run(_operate_remote_keys(remote_keys, audit=False))
        if errors:
            error_types = set(errors.values())
            download.storage_error = sorted(error_types)[0]
            db.commit()
            if "StorageTemporaryError" in error_types:
                raise StorageTemporaryError("Exclusão remota temporariamente indisponível.")
            raise StoragePermanentError("Falha ao excluir arquivo remoto gerenciado.")
        removed = True
    if download.file_path:
        from backend.downloads.service import safe_storage_file

        local = safe_storage_file(download.file_path)
        if local is not None:
            try:
                local.unlink(missing_ok=True)
                removed = True
            except OSError as exc:
                download.storage_error = "LocalDeleteError"
                db.commit()
                raise StorageTemporaryError("Falha ao excluir arquivo local.") from exc
    if download.source_path:
        from backend.downloads.service import safe_storage_file

        source = safe_storage_file(download.source_path)
        if source is not None:
            try:
                source.unlink(missing_ok=True)
                removed = True
            except OSError as exc:
                download.storage_error = "LocalSourceDeleteError"
                db.commit()
                raise StorageTemporaryError("Falha ao excluir a fonte local.") from exc
    download.file_path = None
    download.source_path = None
    download.storage_key = None
    download.storage_reserved_key = None
    download.source_storage_key = None
    download.source_storage_reserved_key = None
    download.source_storage_backend = None
    download.source_remote_parent_id = None
    download.source_remote_size = None
    download.source_remote_checksum = None
    download.source_remote_mime_type = None
    download.source_remote_uploaded_at = None
    download.local_delete_pending = False
    download.storage_state = StorageState.deleted.value
    download.file_deleted_at = datetime.now(timezone.utc)
    download.storage_error = None
    db.commit()
    logger.info(
        "storage_delete backend=%s removed=%s state=deleted",
        download.storage_backend, removed,
    )
    return removed


async def _scan_managed(
    page_token: str | None,
    max_pages: int,
) -> tuple[list[dict], str | None]:
    backend = create_storage_backend(backend_name="google_drive")
    try:
        if not isinstance(backend, GoogleDriveStorageBackend):
            raise StoragePermanentError("Backend remoto indisponível.")
        files: list[dict] = []
        next_page = page_token
        for _ in range(max(1, min(max_pages, 10))):
            page, next_page = await backend.list_managed_files(
                page_token=next_page, page_size=100,
            )
            files.extend(page)
            if not next_page:
                break
        return files, next_page
    finally:
        await backend.close()


def scan_managed_remote_files(
    page_token: str | None,
    *,
    max_pages: int = 2,
) -> tuple[list[dict], str | None]:
    return asyncio.run(_scan_managed(page_token, max_pages))


async def _operate_remote_keys(keys: list[str], *, audit: bool) -> dict[str, str]:
    backend = create_storage_backend(backend_name="google_drive")
    errors: dict[str, str] = {}
    try:
        if not isinstance(backend, GoogleDriveStorageBackend):
            raise StoragePermanentError("Backend remoto indisponível.")
        for key in keys:
            try:
                if audit:
                    await backend.audit_private_file(key)
                else:
                    await backend.delete_file(key)
            except (StorageTemporaryError, StoragePermanentError) as exc:
                errors[key] = type(exc).__name__
        return errors
    finally:
        await backend.close()


def delete_managed_remote_keys(keys: list[str]) -> dict[str, str]:
    return asyncio.run(_operate_remote_keys(keys, audit=False))


def audit_managed_remote_keys(keys: list[str]) -> dict[str, str]:
    return asyncio.run(_operate_remote_keys(keys, audit=True))
