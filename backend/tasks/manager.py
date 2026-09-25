"""Fila persistente e processos isolados controlados pela API."""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from multiprocessing.process import BaseProcess
from typing import Callable

import psutil
from sqlalchemy import func, or_, select, text

from backend.api.config import get_settings
from backend.database.models import Download, DownloadStatus
from backend.database.session import SessionLocal, engine
from backend.observability import container_memory_snapshot, memory_headroom_mb
from backend.tasks.runner import run_task_process

logger = logging.getLogger(__name__)
settings = get_settings()

_LEADER_LOCK_NAME = "xard:api-task-manager:v1"


@dataclass
class _RunningTask:
    process: BaseProcess
    operation_type: str
    started_monotonic: float
    cancel_seen_monotonic: float | None = None


def _operation_for(download: Download) -> str:
    if download.operation_type:
        return download.operation_type
    if download.mode == "gif" and download.source_path:
        return "gif_upload"
    if download.mode == "gif":
        return "gif_url"
    return "download"


def _terminate_process_tree(process: BaseProcess, grace_seconds: float = 5.0) -> None:
    """Encerra o processo e descendentes como FFmpeg/Deno, sem usar shell."""
    if process.pid is None:
        return
    try:
        parent = psutil.Process(process.pid)
        descendants = parent.children(recursive=True)
        for child in descendants:
            child.terminate()
        parent.terminate()
        _, alive = psutil.wait_procs(
            [*descendants, parent], timeout=max(0.1, grace_seconds),
        )
        for item in alive:
            item.kill()
        psutil.wait_procs(alive, timeout=2)
    except psutil.NoSuchProcess:
        pass
    except (psutil.AccessDenied, OSError) as exc:
        logger.warning("task_process_tree_termination_failed type=%s", type(exc).__name__)
        if process.is_alive():
            process.terminate()
    finally:
        process.join(timeout=2)


class TaskManager:
    """Coordena a fila no banco e mantém somente processos ativos em RAM."""

    def __init__(self) -> None:
        self._context = multiprocessing.get_context("spawn")
        self._running: dict[uuid.UUID, _RunningTask] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._stop: asyncio.Event | None = None
        self._scheduler: asyncio.Task | None = None
        self._leader_connection = None
        self._leader = False
        self._queued_count = 0
        self._state_lock = threading.Lock()
        self._maintenance_last_run: dict[str, float] = {}
        self._memory_admission_blocked = False

    async def start(self) -> None:
        if self._scheduler is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._maintenance_last_run.clear()
        self._scheduler = asyncio.create_task(
            self._supervise(), name="api-task-manager",
        )

    async def _supervise(self) -> None:
        """Mantém liderança verificável e volta a standby se a conexão cair."""
        assert self._stop is not None
        standby_logged = False
        while not self._stop.is_set():
            claimed = await asyncio.to_thread(self._claim_leadership)
            with self._state_lock:
                self._leader = claimed
            if claimed:
                try:
                    recovered = await asyncio.to_thread(self._recover_after_restart)
                except Exception as exc:  # noqa: BLE001 - liderança segue e tenta no ciclo
                    recovered = 0
                    logger.exception(
                        "task_manager_recovery_failed type=%s", type(exc).__name__,
                    )
                logger.info(
                    "task_manager_active role=leader concurrency=%s recovered=%s",
                    settings.resolved_task_concurrency,
                    recovered,
                )
                standby_logged = False
                await self._run()
                continue
            if not standby_logged:
                logger.info("task_manager_active role=standby")
                standby_logged = True
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        self.notify()
        if self._scheduler is not None:
            try:
                await asyncio.wait_for(
                    self._scheduler,
                    timeout=settings.api_graceful_shutdown_seconds,
                )
            except asyncio.TimeoutError:
                self._scheduler.cancel()
                await asyncio.gather(self._scheduler, return_exceptions=True)
        self._scheduler = None

        with self._state_lock:
            running = list(self._running.items())
        for task_id, state in running:
            await asyncio.to_thread(_terminate_process_tree, state.process)
            await asyncio.to_thread(
                self._finalize_terminated, task_id, "shutdown",
            )
            with self._state_lock:
                self._running.pop(task_id, None)

        if self._leader_connection is not None:
            try:
                await asyncio.to_thread(
                    self._leader_connection.execute,
                    text("SELECT pg_advisory_unlock(hashtext(:name))"),
                    {"name": _LEADER_LOCK_NAME},
                )
            except Exception as exc:  # noqa: BLE001 - conexão pode já ter caído
                logger.warning("task_manager_unlock_failed type=%s", type(exc).__name__)
            finally:
                await asyncio.to_thread(self._leader_connection.close)
                self._leader_connection = None
        with self._state_lock:
            self._leader = False
        self._loop = None
        self._wake = None
        self._stop = None
        logger.info("task_manager_stopped")

    def notify(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is None or wake is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            return

    def metrics(self) -> dict[str, object]:
        with self._state_lock:
            return {
                "role": "leader" if self._leader else "standby",
                "active": len(self._running),
                "queued": self._queued_count,
                "capacity": settings.resolved_task_concurrency,
                "max_queue_size": settings.max_queue_size,
                "process_ids": [state.process.pid for state in self._running.values()],
            }

    def _claim_leadership(self) -> bool:
        connection = None
        try:
            connection = engine.connect()
            claimed = bool(connection.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:name))"),
                {"name": _LEADER_LOCK_NAME},
            ).scalar_one())
            if not claimed:
                connection.close()
                return False
            self._leader_connection = connection
            return True
        except Exception as exc:  # noqa: BLE001 - API pode servir consultas em standby
            if connection is not None:
                connection.close()
            logger.exception("task_manager_leadership_failed type=%s", type(exc).__name__)
            return False

    def _leadership_is_alive(self) -> bool:
        connection = self._leader_connection
        if connection is None:
            return False
        try:
            connection.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # noqa: BLE001 - a promoção será tentada novamente
            logger.warning(
                "task_manager_leadership_lost type=%s", type(exc).__name__,
            )
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass
            self._leader_connection = None
            return False

    async def _demote_after_leadership_loss(self) -> None:
        with self._state_lock:
            running = list(self._running.items())
            self._leader = False
        for task_id, state in running:
            await asyncio.to_thread(_terminate_process_tree, state.process)
            await asyncio.to_thread(self._finalize_terminated, task_id, "shutdown")
            with self._state_lock:
                self._running.pop(task_id, None)
        logger.warning("task_manager_demoted tasks_requeued=%s", len(running))

    def _recover_after_restart(self) -> int:
        db = SessionLocal()
        recovered = 0
        try:
            now = datetime.now(timezone.utc)
            rows = db.execute(
                select(Download)
                .where(
                    Download.status == DownloadStatus.processing,
                    or_(Download.cache_role.is_(None), Download.cache_role != "waiter"),
                )
                .with_for_update(skip_locked=True)
            ).scalars().all()
            for download in rows:
                download.worker_id = None
                download.interrupted_at = now
                download.heartbeat_at = None
                if download.cancel_requested:
                    download.status = DownloadStatus.cancelled
                    download.stage = "cancelled"
                    download.progress_message = "Tarefa cancelada."
                    download.error = None
                    download.completed_at = now
                elif download.attempts > settings.task_max_retries:
                    download.status = DownloadStatus.failed
                    download.stage = "failed"
                    download.progress_message = "Tarefa interrompida repetidamente."
                    download.error = "A tarefa foi interrompida repetidamente."
                    download.completed_at = now
                else:
                    download.status = DownloadStatus.queued
                    download.stage = "retrying"
                    download.progress_message = "Tarefa recuperada após reinicialização."
                    download.error = None
                    download.queued_at = now
                    recovered += 1
            db.commit()
            return recovered
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    async def _run(self) -> None:
        assert self._stop is not None and self._wake is not None
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                last_ping = self._maintenance_last_run.get("leader_ping", 0.0)
                if now - last_ping >= 5.0:
                    self._maintenance_last_run["leader_ping"] = now
                    alive = await asyncio.to_thread(self._leadership_is_alive)
                    if not alive:
                        await self._demote_after_leadership_loss()
                        return
                await self._reap_processes()
                await self._recover_orphaned_processing()
                await self._start_available_tasks()
                await self._run_maintenance()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - falha transitória não mata a fila
                logger.exception(
                    "task_manager_cycle_failed type=%s", type(exc).__name__,
                )
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=settings.task_poll_interval,
                )
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def _memory_admits_new_task(self) -> bool:
        """Só recusa iniciar tarefas quando HÁ medição real do cgroup: sem
        ela, resolved_task_concurrency (contagem de processos) já é o limite
        de segurança em vigor, e travar a fila às cegas seria pior que não
        proteger nada."""
        if container_memory_snapshot() is None:
            return True
        headroom = memory_headroom_mb(settings.worker_memory_limit_mb)
        if headroom is None:
            return True
        _, available_mb = headroom
        return available_mb >= settings.task_admission_min_available_mb

    async def _start_available_tasks(self) -> None:
        with self._state_lock:
            capacity = settings.resolved_task_concurrency - len(self._running)
            excluded = tuple(self._running)
        if capacity <= 0:
            return
        # Só tarefas já em andamento podem pressionar a memória; com a fila
        # vazia de processos, sempre deixa a primeira entrar.
        if self._running and not await asyncio.to_thread(self._memory_admits_new_task):
            if not self._memory_admission_blocked:
                self._memory_admission_blocked = True
                logger.warning(
                    "task_admission_deferred reason=low_memory min_available_mb=%s",
                    settings.task_admission_min_available_mb,
                )
            return
        if self._memory_admission_blocked:
            self._memory_admission_blocked = False
            logger.info("task_admission_resumed")
        candidates, queued_count = await asyncio.to_thread(
            self._queued_candidates, capacity, excluded,
        )
        with self._state_lock:
            self._queued_count = queued_count
        for task_id, operation_type in candidates:
            with self._state_lock:
                if task_id in self._running:
                    continue
            process = self._context.Process(
                target=run_task_process,
                args=(str(task_id), operation_type),
                name=f"xard-task-{str(task_id)[:8]}",
                daemon=False,
            )
            try:
                process.start()
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "task_process_start_failed task_id=%s type=%s",
                    task_id, type(exc).__name__,
                )
                await asyncio.to_thread(
                    self._mark_start_failed, task_id, type(exc).__name__,
                )
                continue
            with self._state_lock:
                self._running[task_id] = _RunningTask(
                    process=process,
                    operation_type=operation_type,
                    started_monotonic=time.monotonic(),
                )
            logger.info(
                "task_process_started task_id=%s operation=%s pid=%s",
                task_id, operation_type, process.pid,
            )

    def _queued_candidates(
        self,
        capacity: int,
        excluded: tuple[uuid.UUID, ...],
    ) -> tuple[list[tuple[uuid.UUID, str]], int]:
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            base = select(Download).where(
                Download.status == DownloadStatus.queued,
                Download.cancel_requested.is_(False),
                or_(Download.queued_at.is_(None), Download.queued_at <= now),
            )
            queued_count = int(db.execute(
                select(func.count()).select_from(base.subquery())
            ).scalar_one())
            if excluded:
                base = base.where(Download.id.not_in(excluded))
            rows = db.execute(
                base.order_by(Download.queued_at.asc().nulls_first(), Download.created_at)
                .limit(capacity)
            ).scalars().all()
            return [(row.id, _operation_for(row)) for row in rows], queued_count
        finally:
            db.close()

    async def _reap_processes(self) -> None:
        with self._state_lock:
            if not self._running:
                return
            running_ids = tuple(self._running)
            running_items = list(self._running.items())
        snapshots = await asyncio.to_thread(
            self._task_snapshots, running_ids,
        )
        now = time.monotonic()
        for task_id, state in running_items:
            snapshot = snapshots.get(task_id)
            if not state.process.is_alive():
                state.process.join(timeout=1)
                exit_code = state.process.exitcode
                with self._state_lock:
                    self._running.pop(task_id, None)
                if snapshot and snapshot[0] == DownloadStatus.processing:
                    await asyncio.to_thread(
                        self._finalize_terminated, task_id, "crash",
                    )
                logger.info(
                    "task_process_finished task_id=%s exit_code=%s",
                    task_id, exit_code,
                )
                continue

            if snapshot and snapshot[1]:
                state.cancel_seen_monotonic = state.cancel_seen_monotonic or now
                if now - state.cancel_seen_monotonic >= settings.task_cancel_grace_seconds:
                    await asyncio.to_thread(_terminate_process_tree, state.process)
                    await asyncio.to_thread(
                        self._finalize_terminated, task_id, "cancelled",
                    )
                    with self._state_lock:
                        self._running.pop(task_id, None)
                    logger.info("task_cancelled_hard task_id=%s", task_id)
                    continue

            if now - state.started_monotonic >= settings.task_timeout:
                await asyncio.to_thread(_terminate_process_tree, state.process)
                await asyncio.to_thread(
                    self._finalize_terminated, task_id, "timeout",
                )
                with self._state_lock:
                    self._running.pop(task_id, None)
                logger.warning("task_timeout task_id=%s", task_id)

    def _task_snapshots(
        self, task_ids: tuple[uuid.UUID, ...],
    ) -> dict[uuid.UUID, tuple[DownloadStatus, bool]]:
        db = SessionLocal()
        try:
            rows = db.execute(
                select(Download.id, Download.status, Download.cancel_requested)
                .where(Download.id.in_(task_ids))
            ).all()
            return {row.id: (row.status, bool(row.cancel_requested)) for row in rows}
        finally:
            db.close()

    def _mark_start_failed(self, task_id: uuid.UUID, error_type: str) -> None:
        db = SessionLocal()
        try:
            download = db.get(Download, task_id)
            if download and download.status == DownloadStatus.queued:
                download.status = DownloadStatus.failed
                download.stage = "failed"
                download.progress_message = "Não foi possível iniciar o processo isolado."
                download.error = f"Falha ao iniciar o executor ({error_type})."
                download.completed_at = datetime.now(timezone.utc)
                db.commit()
        finally:
            db.close()

    def _finalize_terminated(self, task_id: uuid.UUID, reason: str) -> None:
        db = SessionLocal()
        device_id: uuid.UUID | None = None
        preserve_source = False
        should_cleanup = False
        delete_persisted_files = False
        try:
            download = db.execute(
                select(Download).where(Download.id == task_id).with_for_update()
            ).scalar_one_or_none()
            if download is None or download.status in {
                DownloadStatus.completed,
                DownloadStatus.failed,
                DownloadStatus.cancelled,
            }:
                return
            now = datetime.now(timezone.utc)
            device_id = download.device_id
            download.worker_id = None
            download.heartbeat_at = None
            download.interrupted_at = now
            if reason == "shutdown":
                download.status = DownloadStatus.queued
                download.stage = "queued"
                download.progress_message = "Tarefa devolvida à fila durante o encerramento."
                download.queued_at = now
                preserve_source = download.operation_type == "gif_upload"
            elif reason == "cancelled" or download.cancel_requested:
                download.status = DownloadStatus.cancelled
                download.stage = "cancelled"
                download.progress_message = "Tarefa cancelada."
                download.error = None
                download.completed_at = now
                should_cleanup = True
                delete_persisted_files = True
            elif reason == "timeout":
                download.status = DownloadStatus.failed
                download.stage = "timeout"
                download.progress_message = "A tarefa excedeu o tempo limite."
                download.error = "A tarefa excedeu o tempo máximo de processamento."
                download.completed_at = now
                should_cleanup = not bool(download.file_path or download.storage_key)
            elif download.attempts <= settings.task_max_retries:
                delay = min(30 * (2 ** max(download.attempts - 1, 0)), 300)
                download.status = DownloadStatus.queued
                download.stage = "retrying"
                download.progress_message = "Falha inesperada; nova tentativa agendada."
                download.error = None
                download.queued_at = now + timedelta(seconds=delay)
                preserve_source = download.operation_type == "gif_upload"
                should_cleanup = not bool(download.file_path or download.storage_key)
            else:
                download.status = DownloadStatus.failed
                download.stage = "failed"
                download.progress_message = "O processo da tarefa foi interrompido."
                download.error = "O processamento foi interrompido inesperadamente."
                download.completed_at = now
                should_cleanup = not bool(download.file_path or download.storage_key)
            if should_cleanup and not preserve_source:
                download.source_path = None
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("task_termination_persistence_failed task_id=%s", task_id)
        finally:
            db.close()

        if delete_persisted_files:
            cleanup_db = SessionLocal()
            try:
                from backend.storage import service as storage_service

                persisted = cleanup_db.get(Download, task_id)
                if persisted is not None:
                    storage_service.delete_download_file(cleanup_db, persisted)
            except Exception as exc:  # noqa: BLE001 - manutenção repetirá a exclusão
                cleanup_db.rollback()
                logger.warning(
                    "task_storage_cleanup_after_cancel_failed type=%s",
                    type(exc).__name__,
                )
            finally:
                cleanup_db.close()

        if device_id is not None and should_cleanup:
            try:
                from backend.workers.tasks import cleanup_task_files

                cleanup_task_files(
                    str(task_id), str(device_id), preserve_source=preserve_source,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("task_cleanup_after_termination_failed type=%s", type(exc).__name__)

    async def _recover_orphaned_processing(self) -> None:
        last = self._maintenance_last_run.get("recover", 0.0)
        now = time.monotonic()
        if now - last < min(60.0, settings.task_stale_seconds / 2):
            return
        self._maintenance_last_run["recover"] = now
        with self._state_lock:
            active_ids = tuple(self._running)
        await asyncio.to_thread(self._recover_stale_rows, active_ids)

    def _recover_stale_rows(self, active_ids: tuple[uuid.UUID, ...]) -> None:
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(seconds=settings.task_stale_seconds)
            stmt = select(Download).where(
                Download.status == DownloadStatus.processing,
                or_(Download.cache_role.is_(None), Download.cache_role != "waiter"),
                or_(Download.heartbeat_at.is_(None), Download.heartbeat_at < cutoff),
            )
            if active_ids:
                stmt = stmt.where(Download.id.not_in(active_ids))
            rows = db.execute(
                stmt.with_for_update(skip_locked=True).limit(100)
            ).scalars().all()
            for download in rows:
                download.worker_id = None
                download.heartbeat_at = None
                download.interrupted_at = now
                if download.cancel_requested:
                    download.status = DownloadStatus.cancelled
                    download.stage = "cancelled"
                    download.progress_message = "Tarefa cancelada."
                    download.error = None
                    download.completed_at = now
                elif download.attempts > settings.task_max_retries:
                    download.status = DownloadStatus.failed
                    download.stage = "failed"
                    download.progress_message = "Tarefa interrompida repetidamente."
                    download.error = "A tarefa foi interrompida repetidamente."
                    download.completed_at = now
                else:
                    download.status = DownloadStatus.queued
                    download.stage = "retrying"
                    download.progress_message = "Tarefa recuperada após interrupção."
                    download.error = None
                    download.queued_at = now
            db.commit()
            if rows:
                recovered = sum(row.status == DownloadStatus.queued for row in rows)
                logger.warning(
                    "task_stale_reconciled count=%s requeued=%s",
                    len(rows), recovered,
                )
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    async def _run_maintenance(self) -> None:
        from backend.workers import tasks as maintenance

        specs: tuple[tuple[str, float, Callable[[], object]], ...] = (
            ("expired", settings.task_cleanup_interval, maintenance.cleanup_expired_files),
            ("retention", settings.task_cleanup_interval, maintenance.cleanup_retained_tasks),
            ("orphans", settings.task_orphan_cleanup_interval, maintenance.cleanup_orphaned_files),
            ("storage", settings.task_storage_recovery_interval, maintenance.migrate_fallback_storage),
            ("audit", settings.task_storage_audit_interval, maintenance.audit_remote_storage),
            ("media_cache", settings.media_cache_recovery_interval, maintenance.recover_media_cache),
        )
        now = time.monotonic()
        for name, interval, callback in specs:
            last = self._maintenance_last_run.get(name, 0.0)
            if now - last < interval:
                continue
            self._maintenance_last_run[name] = now
            try:
                result = await asyncio.to_thread(callback)
                log = logger.info if result else logger.debug
                log("task_maintenance name=%s affected=%s", name, result)
            except Exception as exc:  # noqa: BLE001 - próxima janela tenta novamente
                logger.exception(
                    "task_maintenance_failed name=%s type=%s",
                    name, type(exc).__name__,
                )
            break


_manager = TaskManager()


async def start_task_manager() -> None:
    await _manager.start()


async def stop_task_manager() -> None:
    await _manager.stop()


def notify_task_manager() -> None:
    _manager.notify()


def task_manager_metrics() -> dict[str, object]:
    return _manager.metrics()
