"""Limites globais de processos pesados usando advisory locks do PostgreSQL."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Callable

from sqlalchemy import text

from backend.api.config import get_settings
from backend.database.session import engine

logger = logging.getLogger(__name__)
_LOCK_BASES = {"download": 8_706_430_000, "ffmpeg": 8_706_440_000}


@contextmanager
def global_job_slot(
    kind: str,
    identity: object,
    *,
    cancelled: Callable[[], bool] | None = None,
    cancel_exception: type[Exception] = RuntimeError,
):
    """Limita yt-dlp/FFmpeg entre processos e réplicas sem usar Redis/RAM."""
    settings = get_settings()
    slots = (
        settings.max_download_jobs if kind == "download"
        else settings.max_ffmpeg_jobs if kind == "ffmpeg"
        else 1
    )
    if engine.dialect.name != "postgresql":
        yield
        return

    base = _LOCK_BASES[kind]
    start = int(getattr(identity, "int", 0)) % slots
    deadline = time.monotonic() + settings.task_timeout
    connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    acquired: int | None = None
    waiting_logged = False
    job_id = str(identity)[:8]
    try:
        while acquired is None:
            if cancelled is not None and cancelled():
                raise cancel_exception()
            for offset in range(slots):
                key = base + ((start + offset) % slots)
                if connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": key},
                ).scalar_one():
                    acquired = key
                    break
            if acquired is not None:
                break
            if not waiting_logged:
                logger.info("resource_wait kind=%s job_id=%s", kind, job_id)
                waiting_logged = True
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Limite global de {kind} ocupado.")
            time.sleep(0.25)
        logger.info("resource_started kind=%s job_id=%s", kind, job_id)
        yield
    finally:
        if acquired is not None:
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": acquired},
                )
            except Exception as exc:  # noqa: BLE001 - fechar também libera o lock
                logger.warning("resource_lock_release_failed kind=%s type=%s", kind, type(exc).__name__)
        connection.close()
        if acquired is not None:
            logger.info("resource_finished kind=%s job_id=%s", kind, job_id)
