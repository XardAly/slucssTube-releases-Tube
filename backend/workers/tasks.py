"""Processamento isolado de downloads e conversões iniciado pela API."""

from __future__ import annotations

import base64
import gc
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request

from sqlalchemy import and_, delete, or_, select, update

from backend.api.config import get_settings
from backend.database.models import (
    Download,
    DownloadStatus,
    LocalOperation,
    MediaCache,
    SecurityNonce,
    StorageCursor,
    StorageState,
)
from backend.database.session import SessionLocal
from backend.notifications.discord import notify_download_completed
from backend.workers.deno_utils import ensure_deno
from backend.workers.ffmpeg_utils import ensure_ffmpeg
from backend.workers.resource_limits import global_job_slot
from backend.workers.gif_converter import (
    GifConversionCancelled,
    _stop_process as _stop_ffmpeg_process,
    convert_within_limit,
    probe_video,
)
from backend.observability import memory_headroom_mb, release_freed_memory, rss_mb
from backend.security.url_validation import UnsafeUrl, open_public_url, validate_public_http_url
from backend.downloads.schemas import GifOptions
from backend.downloads.service import record_download_completion, safe_storage_file
from backend.downloads import cache as media_cache
from backend.storage import service as storage_service
from backend.storage.base import StorageTemporaryError
from backend.realtime import (
    publish_download_event,
)

settings = get_settings()
logger = logging.getLogger(__name__)


_QUALITY_LABELS = {
    4320: "4320p (8K)", 2160: "2160p (4K)", 1440: "1440p (2K)",
    1080: "1080p (Full HD)", 720: "720p (HD)",
}

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

_YOUTUBE_COOKIE_REJECTION_MARKERS = (
    "the page needs to be reloaded",
    "sign in to confirm you're not a bot",
    "cookies are no longer valid",
)

# ------------------------------------------------------------------
# Progresso persistido: o PostgreSQL é a fonte oficial e a API distribui as
# revisões aos clientes WebSocket conectados.
# ------------------------------------------------------------------
_HEARTBEAT_INTERVAL = 15.0

# Extensões que a limpeza de resíduos nunca remove: DOWNLOADS_DIR só deveria
# conter mídia, mas um caminho mal configurado já custou o pacote downloads/.
_SOURCE_SUFFIXES = {".py", ".pyc", ".pyi", ".pyd", ".so", ".pth"}

_progress_last_write: dict[str, dict] = {}

_PROGRESS_MESSAGES = {
    "queued": "Aguardando na fila.",
    "processing": "Preparando o processamento.",
    "downloading": "Baixando o conteúdo.",
    "converting": "Convertendo o arquivo.",
    "optimizing": "Otimizando o resultado.",
    "optimizing_fps": "Reduzindo quadros para atingir o limite.",
    "storage_uploading": "Enviando ao armazenamento privado.",
    "completed": "Processamento concluído.",
    "retrying": "Nova tentativa agendada.",
    "cancelled": "Tarefa cancelada.",
    "failed": "Não foi possível concluir a tarefa.",
}


class TaskProcessingCancelled(Exception):
    """Cancelamento cooperativo solicitado pelo proprietário da tarefa."""


class CompatibilityMemoryLimit(RuntimeError):
    """O limite do contêiner não comporta um transcode de vídeo completo."""

    def __init__(self, limit_mb: int, available_mb: int) -> None:
        super().__init__("Memória insuficiente para recodificação completa.")
        self.limit_mb = limit_mb
        self.available_mb = available_mb


_COMPATIBILITY_STDERR_TAIL_BYTES = 4096
_COMPATIBILITY_POLL_SECONDS = 0.2
_COMPATIBILITY_PROBE_TIMEOUT_SECONDS = 8.0
_COMPATIBILITY_PROBE_OUTPUT_BYTES = 16 * 1024
_COMPATIBILITY_PLANS = frozenset({"copy", "copy_video", "transcode"})


def _compatibility_transcode_memory_status() -> tuple[bool, int, int]:
    headroom = memory_headroom_mb(settings.worker_memory_limit_mb)
    if headroom is None:
        return True, 0, 0
    limit_mb, available_mb = headroom
    allowed = bool(
        limit_mb >= settings.compatibility_transcode_min_memory_mb
        and available_mb >= settings.compatibility_transcode_min_available_mb
    )
    return allowed, limit_mb, available_mb


def _ensure_compatibility_transcode_memory() -> None:
    allowed, limit_mb, available_mb = _compatibility_transcode_memory_status()
    if not allowed:
        raise CompatibilityMemoryLimit(limit_mb, available_mb)


def _build_compatible_mp4_args(
    ffmpeg: str,
    source: Path,
    output: Path,
    plan: str = "transcode",
) -> list[str]:
    """Monta uma conversão fechada para MP4 H.264/AAC compatível."""
    if plan not in _COMPATIBILITY_PLANS:
        raise ValueError("Plano de compatibilidade inválido.")

    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-nostdin",
        "-i", str(source),
        "-map", "0:v:0",
        "-map", "0:a:0?",
    ]
    if plan == "copy":
        args.extend(("-c:v", "copy", "-c:a", "copy"))
    elif plan == "copy_video":
        args.extend((
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
        ))
    else:
        # Sem -threads o FFmpeg usa todos os núcleos visíveis: cada thread de
        # encode mantém seus próprios buffers de frame, e em vídeos de alta
        # resolução isso soma centenas de MB. Limita ao mesmo teto por job
        # usado no GIF, coerente com resolved_task_concurrency.
        args.extend((
            "-threads", str(settings.resolved_compatibility_ffmpeg_threads),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
        ))
    args.extend((
        "-tag:v", "avc1",
        "-movflags", "+faststart",
        str(output),
    ))
    return args


def _read_limited_probe_output(output_log) -> bytes:
    """Lê o JSON pequeno do ffprobe sem aceitar saída ilimitada."""
    output_log.flush()
    size = output_log.seek(0, os.SEEK_END)
    if size > _COMPATIBILITY_PROBE_OUTPUT_BYTES:
        raise RuntimeError("O FFprobe retornou metadados acima do limite.")
    output_log.seek(0)
    return output_log.read(_COMPATIBILITY_PROBE_OUTPUT_BYTES)


def _probe_first_compatibility_stream(
    ffprobe: str,
    source: Path,
    selector: str,
    cancelled: Callable[[], bool],
) -> dict | None:
    """Consulta somente o primeiro stream pedido, com prazo e memória limitados."""
    if selector not in {"v:0", "a:0"}:
        raise ValueError("Seletor de stream inválido.")
    if cancelled():
        raise TaskProcessingCancelled()

    process: subprocess.Popen | None = None
    with (
        tempfile.TemporaryFile(prefix="xard-ffprobe-", suffix=".json") as output_log,
        tempfile.TemporaryFile(prefix="xard-ffprobe-", suffix=".log") as error_log,
    ):
        try:
            process = subprocess.Popen(
                [
                    ffprobe,
                    "-v", "error",
                    "-select_streams", selector,
                    "-show_entries", "stream=codec_type,codec_name,pix_fmt",
                    "-of", "json",
                    str(source),
                ],
                stdin=subprocess.DEVNULL,
                stdout=output_log,
                stderr=error_log,
                shell=False,
                start_new_session=True,
            )
            deadline = time.monotonic() + _COMPATIBILITY_PROBE_TIMEOUT_SECONDS
            while True:
                if cancelled():
                    _stop_ffmpeg_process(process)
                    raise TaskProcessingCancelled()
                returncode = process.poll()
                if returncode is not None:
                    break
                if time.monotonic() >= deadline:
                    _stop_ffmpeg_process(process)
                    raise TimeoutError("O FFprobe excedeu o tempo limite.")
                time.sleep(_COMPATIBILITY_POLL_SECONDS)

            if returncode != 0:
                raise RuntimeError("O FFprobe não conseguiu ler o vídeo.")
            if cancelled():
                raise TaskProcessingCancelled()

            try:
                data = json.loads(
                    _read_limited_probe_output(output_log).decode(
                        "utf-8", errors="strict",
                    )
                )
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
                raise RuntimeError("O FFprobe retornou metadados inválidos.") from exc
            streams = data.get("streams") if isinstance(data, dict) else None
            if not isinstance(streams, list):
                raise RuntimeError("O FFprobe retornou metadados inválidos.")
            if not streams:
                return None
            return streams[0] if isinstance(streams[0], dict) else None
        finally:
            if process is not None and process.poll() is None:
                _stop_ffmpeg_process(process)


def _compatibility_plan(
    ffprobe: str,
    source: Path,
    cancelled: Callable[[], bool],
) -> str:
    """Escolhe cópia segura; qualquer dúvida recai no transcode completo."""
    try:
        video = _probe_first_compatibility_stream(
            ffprobe, source, "v:0", cancelled,
        )
    except TaskProcessingCancelled:
        raise
    except (OSError, RuntimeError, TimeoutError) as exc:
        logger.warning(
            "compatibility_probe_fallback type=%s",
            type(exc).__name__,
        )
        return "transcode"

    video_codec = str((video or {}).get("codec_name") or "").lower()
    pixel_format = str((video or {}).get("pix_fmt") or "").lower()
    if video_codec != "h264" or pixel_format != "yuv420p":
        return "transcode"

    try:
        audio = _probe_first_compatibility_stream(
            ffprobe, source, "a:0", cancelled,
        )
    except TaskProcessingCancelled:
        raise
    except (OSError, RuntimeError, TimeoutError) as exc:
        logger.warning(
            "compatibility_probe_fallback type=%s",
            type(exc).__name__,
        )
        return "transcode"

    if audio is None:
        return "copy"
    audio_codec = str(audio.get("codec_name") or "").lower()
    return "copy" if audio_codec == "aac" else "copy_video"


def _compatibility_ffprobe_executable(ffmpeg: str) -> str:
    executable = Path(ffmpeg)
    name = "ffprobe.exe" if executable.suffix.lower() == ".exe" else "ffprobe"
    return str(executable.with_name(name))


def _read_compatibility_stderr(error_log) -> str:
    """Lê somente o fim do log temporário, mantendo o erro limitado em RAM."""
    try:
        error_log.flush()
        size = error_log.seek(0, os.SEEK_END)
        error_log.seek(max(0, size - _COMPATIBILITY_STDERR_TAIL_BYTES))
        return error_log.read(_COMPATIBILITY_STDERR_TAIL_BYTES).decode(
            "utf-8", errors="replace",
        ).strip()
    except OSError:
        return ""


def _requires_compatible_mp4_transcode(
    mode: str,
    output_format: str | None,
) -> bool:
    return mode in {"va", "v"} and (output_format or "mp4").lower() == "mp4"


def _compatibility_ffmpeg_executable(ffmpeg_dir: str | None) -> str:
    if not ffmpeg_dir:
        raise RuntimeError("FFmpeg indisponível para converter o vídeo para MP4.")
    return str(
        Path(ffmpeg_dir) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    )


def _run_compatibility_attempt(
    ffmpeg: str,
    source: Path,
    temporary: Path,
    plan: str,
    cancelled: Callable[[], bool],
) -> None:
    """Executa uma tentativa isolada, sempre encerrando o processo iniciado."""
    if cancelled():
        raise TaskProcessingCancelled()

    process: subprocess.Popen | None = None
    try:
        with tempfile.TemporaryFile(
            prefix="xard-ffmpeg-compat-", suffix=".log",
        ) as error_log:
            process = subprocess.Popen(
                _build_compatible_mp4_args(
                    ffmpeg, source, temporary, plan=plan,
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=error_log,
                shell=False,
                start_new_session=True,
            )
            while True:
                if cancelled():
                    _stop_ffmpeg_process(process)
                    raise TaskProcessingCancelled()
                returncode = process.poll()
                if returncode is not None:
                    break
                time.sleep(_COMPATIBILITY_POLL_SECONDS)

            if returncode != 0:
                detail = _read_compatibility_stderr(error_log)
                message = "Não foi possível converter o vídeo para MP4 compatível."
                if detail:
                    message = f"{message} {detail}"
                else:
                    message = f"{message} FFmpeg encerrou com código {returncode}."
                raise RuntimeError(message)

        try:
            output_size = temporary.stat().st_size
        except OSError:
            output_size = 0
        if output_size <= 0:
            raise RuntimeError("O FFmpeg gerou um arquivo MP4 vazio.")
        if cancelled():
            raise TaskProcessingCancelled()
    finally:
        if process is not None and process.poll() is None:
            _stop_ffmpeg_process(process)


def _transcode_compatible_mp4(
    ffmpeg: str,
    source: Path,
    cancelled: Callable[[], bool],
) -> Path:
    """Compatibiliza em arquivo separado e publica o MP4 somente após sucesso."""
    if cancelled():
        raise TaskProcessingCancelled()
    if not source.is_file():
        raise RuntimeError("Arquivo de vídeo baixado não foi encontrado.")

    destination = (
        source
        if source.suffix.lower() == ".mp4"
        else source.parent / f"{uuid.uuid4().hex}.mp4"
    )
    temporary = source.parent / (
        f".{source.stem}.{uuid.uuid4().hex}.transcoding.mp4"
    )

    try:
        initial_plan = _compatibility_plan(
            _compatibility_ffprobe_executable(ffmpeg), source, cancelled,
        )
        plans = (
            (initial_plan, "transcode")
            if initial_plan in {"copy", "copy_video"}
            else ("transcode",)
        )
        for plan in plans:
            temporary.unlink(missing_ok=True)
            try:
                if plan == "transcode":
                    _ensure_compatibility_transcode_memory()
                _run_compatibility_attempt(
                    ffmpeg, source, temporary, plan, cancelled,
                )
                break
            except TaskProcessingCancelled:
                raise
            except (OSError, RuntimeError) as exc:
                temporary.unlink(missing_ok=True)
                if plan == "transcode":
                    raise
                if cancelled():
                    raise TaskProcessingCancelled()
                logger.warning(
                    "compatibility_copy_retry plan=%s type=%s",
                    plan, type(exc).__name__,
                )
        if cancelled():
            raise TaskProcessingCancelled()

        os.replace(temporary, destination)
        if source != destination:
            try:
                source.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "compatibility_source_cleanup_failed type=%s",
                    type(exc).__name__,
                )
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def _compatibilize_mp4_or_keep_original(
    download_id: str,
    ffmpeg: str,
    source: Path,
    cancelled: Callable[[], bool],
) -> Path:
    """Mantém o download utilizável se o transcode exceder os recursos do host."""
    try:
        return _transcode_compatible_mp4(ffmpeg, source, cancelled)
    except TaskProcessingCancelled:
        raise
    except CompatibilityMemoryLimit as exc:
        if not source.is_file():
            raise
        logger.warning(
            "Download %s: conversão ignorada para proteger a RAM "
            "(%s MB disponíveis de %s MB).",
            download_id[:8], exc.available_mb, exc.limit_mb,
        )
        return source
    except (OSError, RuntimeError) as exc:
        if not source.is_file():
            raise
        logger.warning(
            "Download %s: compatibilidade não aplicada; arquivo original mantido.",
            download_id[:8],
        )
        logger.debug(
            "compatibility_conversion_failed type=%s detail=%s",
            type(exc).__name__, str(exc)[:_COMPATIBILITY_STDERR_TAIL_BYTES],
        )
        return source


def _download_progress_percent(
    done_files: int,
    current_percent: float,
    expected_files: int,
    *,
    ceiling: float = 100.0,
    previous: float = 0.0,
) -> float:
    """Calcula progresso monotônico, reservando uma faixa para pós-processar."""
    if expected_files <= 0:
        raise ValueError("A quantidade esperada de arquivos deve ser positiva.")
    completed = max(0, min(done_files, expected_files))
    current = max(0.0, min(float(current_percent), 100.0))
    limit = max(0.0, min(float(ceiling), 100.0))
    overall = min(100.0, (completed * 100.0 + current) / expected_files)
    scaled = overall * limit / 100.0
    return round(max(min(float(previous), limit), scaled), 1)


def _weighted_stream_progress(
    bytes_done: int,
    current_downloaded: int,
    current_total: int,
    combined_total_hint: int = 0,
) -> float | None:
    """Progresso (0-100) pela fração real de bytes já baixados entre streams.

    Evita dividir a barra 50/50 por ARQUIVO quando vídeo e áudio têm tamanhos
    muito diferentes (vídeo domina os bytes) — isso fazia a % "saltar" rápido
    assim que o segundo arquivo, bem menor, começava a baixar. Retorna None
    quando nenhum tamanho é conhecido ainda, para o chamador usar outra base.
    """
    combined_total = combined_total_hint or (bytes_done + current_total)
    if not combined_total:
        return None
    return min(100.0, (bytes_done + current_downloaded) * 100 / combined_total)


def _progress_message(stage: str) -> str:
    return _PROGRESS_MESSAGES.get(stage, "Processando tarefa.")


def _task_directory(download_id: str, device_id: str) -> Path:
    return Path(settings.downloads_dir) / device_id / download_id


def cleanup_task_files(
    download_id: str,
    device_id: str,
    *,
    preserve_source: bool = False,
) -> None:
    """Remove resíduos de uma tarefa sem atravessar a raiz configurada."""
    task_dir = _task_directory(download_id, device_id)
    try:
        root = Path(settings.downloads_dir).resolve()
        resolved = task_dir.resolve()
        if not resolved.is_relative_to(root) or not resolved.exists():
            return
        if preserve_source:
            for path in resolved.rglob("*"):
                if path.is_file() and path.name != "source-upload" + path.suffix:
                    path.unlink(missing_ok=True)
            return
        shutil.rmtree(resolved)
        parent = resolved.parent
        if parent != root:
            try:
                parent.rmdir()
            except OSError:
                pass
        logger.info("task_files_cleaned task_id=%s", download_id)
    except OSError as exc:
        logger.warning("task_files_cleanup_failed type=%s", type(exc).__name__)


def _cleanup_completed_remote_task(download: Download) -> None:
    if (
        download.storage_backend == "google_drive"
        and download.storage_state == StorageState.completed.value
        and not download.file_path
        and not download.source_path
    ):
        cleanup_task_files(str(download.id), str(download.device_id))


def _start_task_heartbeat(
    download_id: str,
    interval_seconds: float | None = None,
) -> Callable[[], None]:
    """Mantém a posse da tarefa viva mesmo em estágios sem progresso visível."""
    stop_event = threading.Event()
    task_id = uuid.UUID(download_id)
    interval = interval_seconds or max(
        5.0,
        min(30.0, settings.task_stale_seconds / 3),
    )

    def heartbeat_loop() -> None:
        while not stop_event.wait(interval):
            heartbeat_db = SessionLocal()
            try:
                result = heartbeat_db.execute(
                    update(Download)
                    .where(
                        Download.id == task_id,
                        Download.status == DownloadStatus.processing,
                    )
                    .values(heartbeat_at=datetime.now(timezone.utc))
                )
                heartbeat_db.commit()
                if result.rowcount != 1:
                    return
            except Exception as exc:  # noqa: BLE001 - próxima rodada tenta novamente
                heartbeat_db.rollback()
                logger.warning("task_heartbeat_failed type=%s", type(exc).__name__)
            finally:
                heartbeat_db.close()

    thread = threading.Thread(
        target=heartbeat_loop,
        name=f"task-heartbeat-{download_id[:8]}",
        daemon=True,
    )
    thread.start()

    def stop() -> None:
        stop_event.set()
        thread.join(timeout=2)

    return stop


_MEMORY_SAMPLE_INTERVAL_SECONDS = 7.0


def _start_memory_sampler(download_id: str) -> Callable[[], None]:
    """Amostra o RSS deste processo periodicamente, marcado pelo estágio atual.

    Não segura nenhum objeto grande: só lê psutil e o estágio já publicado
    por _store_progress. Serve para descobrir, pelos logs, qual etapa
    (download/converting/storage_uploading...) realmente pesa na RAM.
    """
    stop_event = threading.Event()
    peak_rss_mb = 0.0

    def sampler_loop() -> None:
        nonlocal peak_rss_mb
        while not stop_event.wait(_MEMORY_SAMPLE_INTERVAL_SECONDS):
            current = rss_mb()
            if current < 0:
                continue
            peak_rss_mb = max(peak_rss_mb, current)
            stage = _progress_last_write.get(download_id, {}).get("stage", "processing")
            logger.info(
                "memory_sample task_id=%s stage=%s rss_mb=%.1f peak_rss_mb=%.1f",
                download_id[:8], stage, current, peak_rss_mb,
            )

    thread = threading.Thread(
        target=sampler_loop,
        name=f"task-memory-{download_id[:8]}",
        daemon=True,
    )
    thread.start()

    def stop() -> None:
        stop_event.set()
        thread.join(timeout=2)

    return stop


def _log_download_progress(download_id: str, percent: float, state: dict) -> None:
    whole_percent = int(max(0.0, min(percent, 100.0)))
    last_percent = int(state.get("logged_percent", -1))
    for current_percent in range(last_percent + 1, whole_percent + 1):
        if current_percent == 0 or current_percent % 5 == 0:
            logger.info("Download %s: %s%%", download_id[:8], current_percent)
    state["logged_percent"] = max(last_percent, whole_percent)


def _release_memory_before_conversion() -> None:
    """Descarta ciclos do yt-dlp e devolve páginas livres ao cgroup no Linux."""
    gc.collect()
    release_freed_memory()


def _temporary_storage_bytes(root: Path) -> int:
    total = 0
    if not root.exists():
        return 0
    for directory, _subdirs, files in os.walk(root):
        for filename in files:
            try:
                total += (Path(directory) / filename).stat().st_size
            except OSError:
                continue
    return total


def _ensure_temporary_storage_capacity() -> None:
    """Falha antes do trabalho pesado quando o disco temporário está no limite."""
    root = Path(settings.downloads_dir)
    root.mkdir(parents=True, exist_ok=True)
    usage = _temporary_storage_bytes(root)
    maximum = settings.temp_storage_max_mb * 1024 * 1024
    free = shutil.disk_usage(root).free
    minimum_free = settings.temp_storage_min_free_mb * 1024 * 1024
    if usage >= maximum or free <= minimum_free:
        logger.warning(
            "temporary_storage_busy used_mb=%s free_mb=%s",
            usage // (1024 * 1024), free // (1024 * 1024),
        )
        raise StorageTemporaryError("Armazenamento temporário ocupado. Tente novamente.")


def _store_progress(download_id: str, percent: float, stage: str) -> None:
    now = time.monotonic()
    value = round(max(0.0, min(percent, 100.0)), 1)
    state = _progress_last_write.setdefault(download_id, {
        "db_at": 0.0, "stage": "", "db_percent": -1.0,
        "heartbeat": 0.0, "db_writes": 0, "logged_percent": -1,
    })
    if stage in {"downloading", "converting"}:
        _log_download_progress(download_id, value, state)
    stage_changed = stage != state["stage"]
    heartbeat_due = now - state["heartbeat"] >= _HEARTBEAT_INTERVAL
    terminal = value == 100.0 or (value == 0.0 and state["db_percent"] < 0)
    db_due = (
        stage_changed or terminal or heartbeat_due
        or value >= state["db_percent"] + settings.progress_update_step
        or now - state["db_at"] >= settings.progress_update_interval
    )
    if not db_due:
        return
    progress_db = SessionLocal()
    try:
        dl = progress_db.get(Download, uuid.UUID(download_id))
        if not dl or dl.status != DownloadStatus.processing:
            return
        dl.progress = value
        dl.stage = stage
        dl.progress_message = _progress_message(stage)
        if heartbeat_due:
            dl.heartbeat_at = datetime.now(timezone.utc)
        media_cache.propagate_progress(progress_db, dl)
        progress_db.commit()
        publish_download_event(
            dl,
            "download.stage_changed" if stage_changed else "download.progress",
        )
        state.update(
            db_at=now,
            stage=stage,
            db_percent=value,
            heartbeat=now if heartbeat_due else state["heartbeat"],
            db_writes=state["db_writes"] + 1,
        )
    except Exception as exc:
        progress_db.rollback()
        logger.warning("progress_persistence_failed type=%s", type(exc).__name__)
    finally:
        progress_db.close()


def _clear_progress(download_id: str) -> None:
    state = _progress_last_write.pop(download_id, None)
    if state:
        logger.debug(
            "task_progress_metrics download_id=%s db_writes=%s",
            download_id, state["db_writes"],
        )


def _cancel_requested(download_id: str) -> bool:
    check_db = SessionLocal()
    try:
        dl = check_db.get(Download, uuid.UUID(download_id))
        return bool(dl and dl.cancel_requested)
    finally:
        check_db.close()


def _make_cancel_checker(download_id: str):
    state = {"last_db": 0.0, "value": False}

    def cancelled() -> bool:
        if state["value"]:
            return True
        now = time.monotonic()
        if now - state["last_db"] >= settings.task_cancel_check_interval:
            state["last_db"] = now
            state["value"] = _cancel_requested(download_id)
        return bool(state["value"])

    return cancelled


def _gif_conversion_limits() -> dict:
    return {
        "threads": settings.resolved_gif_ffmpeg_threads,
        "filter_threads": settings.resolved_gif_ffmpeg_filter_threads,
        "timeout_seconds": settings.gif_ffmpeg_timeout_seconds,
        "max_encodings": settings.gif_max_encodings,
        "safety_margin": settings.gif_size_safety_margin,
        "min_fps": settings.gif_min_fps,
        "min_colors": settings.gif_min_colors,
        "min_width": settings.gif_min_width,
        "min_height": settings.gif_min_height,
    }


def _publish_generated_file(
    generated: Path,
    destination: Path,
    cancelled: Callable[[], bool],
) -> None:
    """Publica atomicamente e remove o arquivo se o cancelamento vencer a corrida."""
    if cancelled():
        raise GifConversionCancelled()
    os.replace(generated, destination)
    if cancelled():
        destination.unlink(missing_ok=True)
        raise GifConversionCancelled()


def _resume_pending_storage(
    db,
    download: Download,
    download_id: str,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """Retoma só a publicação remota sem repetir download ou FFmpeg."""
    if download.storage_state not in {
        StorageState.remote_uploading.value,
        StorageState.remote_ready.value,
        StorageState.local_delete_pending.value,
        StorageState.completed.value,
    }:
        return False
    path = safe_storage_file(download.file_path)
    if download.storage_state == StorageState.completed.value:
        if download.storage_backend == "google_drive" and not download.storage_key:
            raise RuntimeError("O arquivo remoto concluído não possui chave confirmada.")
        if download.storage_backend == "local" and path is None:
            raise RuntimeError("O arquivo local concluído não está disponível.")
    elif (
        download.storage_state in {
            StorageState.remote_ready.value,
            StorageState.local_delete_pending.value,
        }
        and download.storage_key
    ):
        raw_path = Path(download.file_path) if download.file_path else None
        if path is None and raw_path is not None and raw_path.exists():
            raise RuntimeError("A cópia local confirmada está fora da área segura.")
        storage_service.finalize_confirmed_remote_output(db, download, path)
    else:
        if path is None:
            raise RuntimeError("O arquivo local aguardando armazenamento não está disponível.")
        storage_service.persist_completed_output(
            db, download, path, cancel_requested=cancelled,
        )
    if download.source_path:
        source = safe_storage_file(download.source_path)
        if source is not None:
            source.unlink(missing_ok=True)
        download.source_path = None
    download.status = DownloadStatus.completed
    download.progress = 100.0
    download.stage = "completed"
    download.error = None
    download.completed_at = download.completed_at or datetime.now(timezone.utc)
    download.heartbeat_at = datetime.now(timezone.utc)
    followers = media_cache.complete_owner(db, download)
    record_download_completion(db, download)
    db.commit()
    publish_download_event(download, "download.completed")
    for follower in followers:
        publish_download_event(follower, "download.completed")
    _cleanup_completed_remote_task(download)
    _clear_progress(download_id)
    return True


def _resolve_cookiefile() -> str | None:
    configured = (settings.ytdlp_cookies_file or "").strip()
    candidates: list[Path] = []
    if configured:
        configured_path = Path(configured)
        candidates.append(configured_path)
        if not configured_path.is_absolute():
            candidates.append(Path.cwd() / configured_path)
            candidates.append(Path(__file__).resolve().parents[2] / configured_path)

    app_dir = Path(__file__).resolve().parents[2]
    candidates.extend((
        Path.cwd() / "cookies.txt",
        app_dir / "cookies.txt",
        app_dir / "www.youtube.com_cookies.txt",
    ))

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _ytdlp_base_opts() -> dict:
    """Opções comuns do yt-dlp; inclui cookies se configurados (anti-bot)."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "http_headers": _HTTP_HEADERS,
        # O app opera sempre com UM único vídeo. Sem estas duas opções, uma URL
        # de vídeo com &list=... (o mix/rádio que o YouTube anexa sozinho aos
        # links compartilhados), de playlist ou de canal faz o yt-dlp extrair
        # TODOS os itens numa só chamada — cada um com dezenas de formatos — e
        # estourar a RAM do servidor (derrubando a host). noplaylist resolve o
        # caso "watch?v=X&list=Y" direto para o vídeo X; playlist_items limita
        # qualquer enumeração restante (playlist/canal puros) ao primeiro item.
        "noplaylist": True,
        "playlist_items": "1",
        # Só "default": ele já resolve para android_vr sem cookies e para
        # tv_downgraded+web_safari com cookies. Pedir android_vr junto era peso
        # morto — com cookiefile o yt-dlp descarta esse cliente ("does not
        # support cookies") e sem cookies ele já entra pelo default.
        "extractor_args": {
            "youtube": {
                "player_client": ["default"],
            },
        },
    }
    cookies = _resolve_cookiefile()
    if cookies:
        opts["cookiefile"] = cookies
    return opts


def _is_tiktok_short_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"vm.tiktok.com", "vt.tiktok.com"}


def _is_youtube_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}


def _youtube_cookie_rejected(error: Exception) -> bool:
    detail = " ".join(str(error).replace("’", "'").lower().split())
    return any(marker in detail for marker in _YOUTUBE_COOKIE_REJECTION_MARKERS)


def _extract_with_cookie_fallback(
    yt_dlp,
    url: str,
    opts: dict,
    *,
    download: bool,
    result_builder=None,
    result_usable=None,
):
    """Repete uma extração do YouTube sem cookies quando a sessão falhou."""
    build_result = result_builder or (lambda _ydl, info: info)

    def attempt(current_opts: dict):
        with yt_dlp.YoutubeDL(current_opts) as ydl:
            info = ydl.extract_info(url, download=download)
            return build_result(ydl, info)

    can_retry = _is_youtube_url(url) and bool(opts.get("cookiefile"))
    try:
        result = attempt(opts)
    except yt_dlp.utils.DownloadError as exc:
        if not can_retry or not _youtube_cookie_rejected(exc):
            raise
        result = None
    else:
        if not can_retry or result_usable is None or result_usable(result):
            return result

    del result
    logger.warning("youtube_cookies_rejected_retrying_anonymous")
    anonymous_opts = dict(opts)
    anonymous_opts.pop("cookiefile", None)
    return attempt(anonymous_opts)


def _expand_tiktok_short_url(url: str) -> str:
    if not _is_tiktok_short_url(url):
        return url

    headers = {**_HTTP_HEADERS, "Referer": "https://www.tiktok.com/"}
    request = Request(url, headers=headers)
    try:
        with open_public_url(request, timeout=10) as response:
            final_url = response.geturl()
    except HTTPError as exc:
        final_url = exc.geturl()
    except (OSError, TimeoutError, URLError):
        return url

    final_host = (urlparse(final_url).hostname or "").lower()
    if final_host in {"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}:
        return final_url
    return url


# Caminhos de um post do TikTok: /@usuario/video/ID, /video/ID, /photo/ID...
_TIKTOK_POST_PATH = re.compile(r"^/(?:@[\w.\-]+/(?:video|photo)|video|photo|embed|v)/\d+/?$")


def _canonical_tiktok_url(url: str) -> str:
    """
    Remove o rastreio dos links vindos do botão "compartilhar" do TikTok.

    Esses links carregam dezenas de parâmetros (share_iid, sec_user_id,
    enable_checksum, timestamp...) emitidos para o aparelho e o IP de quem
    compartilhou. Repetidos a partir de outro cliente, o TikTok responde
    "Your IP address is blocked from accessing this post" e a extração falha —
    enquanto o mesmo vídeo abre normalmente pelo link canônico.
    """
    parsed = urlparse(url)
    if not (parsed.query or parsed.fragment):
        return url
    host = (parsed.hostname or "").lower()
    if host != "tiktok.com" and not host.endswith(".tiktok.com"):
        return url
    if not _TIKTOK_POST_PATH.match(parsed.path or ""):
        return url
    return urlunparse(parsed._replace(query="", fragment=""))


def _resolve_platform_url(url: str) -> str:
    """Link pronto para o yt-dlp: encurtador expandido e sem rastreio."""
    return _canonical_tiktok_url(_expand_tiktok_short_url(url))


def _configure_youtube_runtime(opts: dict, url: str) -> None:
    if not _is_youtube_url(url):
        return
    deno = ensure_deno()
    if deno:
        opts["js_runtimes"] = {"deno": {"path": deno}}
        # O yt-dlp repassa o ambiente ao subprocesso Deno; DENO_V8_FLAGS
        # limita a RAM do V8 (pico medido: ~260 MB → ~200 MB por consulta).
        # setdefault: a variável definida no painel da hospedagem prevalece.
        if settings.deno_v8_flags:
            os.environ.setdefault("DENO_V8_FLAGS", settings.deno_v8_flags)


_TWITTER_HOSTS = {
    "twitter.com", "www.twitter.com", "mobile.twitter.com",
    "x.com", "www.x.com", "mobile.x.com",
}


def _is_twitter_url(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in _TWITTER_HOSTS


_twitter_cookiefile_cache: str | None = None
_twitter_cookiefile_lock = threading.Lock()


def _resolve_twitter_cookiefile() -> str | None:
    """
    Reconstrói o cookie de sessão do X a partir de TWITTER_COOKIES_B64.

    Propositalmente SEPARADO do ytdlp_cookies_file: aquele arquivo é servido
    inteiro ao app em GET /video/cookies (COOKIES_SHARING_ENABLED) — qualquer
    device autenticado o recebe. Se a sessão do X estivesse lá, qualquer
    instalação do app conseguiria extrair o auth_token da conta. Este valor só
    é lido aqui, pelo worker, e nunca por uma rota de API.
    """
    global _twitter_cookiefile_cache
    encoded = (settings.twitter_cookies_b64 or "").strip()
    if not encoded:
        return None
    with _twitter_cookiefile_lock:
        if _twitter_cookiefile_cache and Path(_twitter_cookiefile_cache).is_file():
            return _twitter_cookiefile_cache
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            logger.warning("twitter_cookies_b64_invalid")
            return None
        path = Path(tempfile.gettempdir()) / f"xard-twitter-cookies-{os.getpid()}.txt"
        path.write_bytes(raw)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _twitter_cookiefile_cache = str(path)
        return _twitter_cookiefile_cache


def _configure_twitter_runtime(opts: dict, url: str) -> None:
    if not _is_twitter_url(url):
        return
    cookiefile = _resolve_twitter_cookiefile()
    if cookiefile:
        opts["cookiefile"] = cookiefile
    else:
        opts.pop("cookiefile", None)


def _video_qualities(formats: list[dict]) -> list[dict]:
    """Agrupa os formatos por resolução: melhor opção de cada altura."""
    best_audio_size = max(
        (f.get("filesize") or f.get("filesize_approx") or 0
         for f in formats
         if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")),
        default=0,
    )

    by_height: dict[int, dict] = {}
    for f in formats:
        if f.get("vcodec") in (None, "none") or not f.get("height"):
            continue
        current = by_height.get(f["height"])
        codec = str(f.get("vcodec") or "").lower()
        is_h264 = codec == "h264" or codec.startswith(("avc1", "avc3"))
        score = (is_h264, f.get("fps") or 0, f.get("tbr") or 0)
        current_codec = str((current or {}).get("vcodec") or "").lower()
        current_is_h264 = current_codec == "h264" or current_codec.startswith(
            ("avc1", "avc3"),
        )
        current_score = (
            current_is_h264,
            (current or {}).get("fps") or 0,
            (current or {}).get("tbr") or 0,
        )
        if current is None or score > current_score:
            by_height[f["height"]] = f

    result = []
    for height in sorted(by_height, reverse=True):
        f = by_height[height]
        size = f.get("filesize") or f.get("filesize_approx") or 0
        result.append({
            "height": height,
            "label": _QUALITY_LABELS.get(height, f"{height}p"),
            "fps": f.get("fps"),
            "vcodec": (f.get("vcodec") or "").split(".")[0],
            "size": size,
            "size_with_audio": (size + best_audio_size) if size else 0,
            "format_id": f.get("format_id"),
        })
    return result


def _audio_qualities(formats: list[dict]) -> list[dict]:
    """Qualidades de áudio disponíveis (maior bitrate primeiro)."""
    audio, seen_abr = [], set()
    for f in formats:
        if f.get("vcodec") not in (None, "none") or f.get("acodec") in (None, "none"):
            continue
        abr = int(f.get("abr") or f.get("tbr") or 0)
        if abr in seen_abr:
            continue
        seen_abr.add(abr)
        audio.append({
            "abr": abr,
            "label": f"{abr} kbps" if abr else "Qualidade padrão",
            "acodec": (f.get("acodec") or "").split(".")[0],
            "size": f.get("filesize") or f.get("filesize_approx") or 0,
            "format_id": f.get("format_id"),
        })
    audio.sort(key=lambda a: a["abr"], reverse=True)
    return audio


def _fallback_video_quality(info: dict, formats: list[dict]) -> list[dict]:
    best = next(
        (f for f in formats
         if f.get("vcodec") not in (None, "none") and f.get("format_id")),
        None,
    )
    if not best:
        return []
    height = best.get("height") or info.get("height")
    return [{
        "height": height,
        "label": f"{height}p" if height else "Melhor qualidade",
        "fps": best.get("fps"),
        "vcodec": (best.get("vcodec") or "").split(".")[0],
        "size": best.get("filesize") or best.get("filesize_approx") or 0,
        "size_with_audio": best.get("filesize") or best.get("filesize_approx") or 0,
        "format_id": best.get("format_id"),
    }]


def _fallback_audio_quality(formats: list[dict]) -> list[dict]:
    best = next(
        (f for f in formats
         if f.get("acodec") not in (None, "none") and f.get("format_id")),
        None,
    )
    if not best:
        return []
    abr = int(best.get("abr") or best.get("tbr") or 0)
    return [{
        "abr": abr,
        "label": f"{abr} kbps" if abr else "Melhor audio",
        "acodec": (best.get("acodec") or "").split(".")[0],
        "size": best.get("filesize") or best.get("filesize_approx") or 0,
        "format_id": best.get("format_id"),
    }]


def _info_has_playable_media(info: dict | None) -> bool:
    if not info:
        return False
    entries = info.get("entries")
    if entries is not None:
        return any(_info_has_playable_media(entry) for entry in entries if entry)
    return any(
        f.get("vcodec") not in (None, "none")
        or f.get("acodec") not in (None, "none")
        for f in info.get("formats", [])
    )


def fetch_info(url: str) -> dict:
    """
    Metadados do vídeo — sem download.

    As qualidades já saem agrupadas daqui: o cliente nunca recebe a
    lista bruta de formatos internos do yt-dlp.
    """
    import yt_dlp

    resolved_url = _resolve_platform_url(url)
    captured_messages: list[str] = []

    class _CapturingLogger:
        def debug(self, msg):  # noqa: ANN001
            pass

        def warning(self, msg):  # noqa: ANN001
            captured_messages.append(str(msg))

        def error(self, msg):  # noqa: ANN001
            captured_messages.append(str(msg))

    opts = {
        **_ytdlp_base_opts(),
        "skip_download": True,
        "socket_timeout": 15,
        "ignore_no_formats_error": True,
        "logger": _CapturingLogger(),
    }
    # Só na consulta de metadados: pular os manifestos HLS/DASH poupa uma
    # requisição a manifest.googlevideo.com (~0,5 s) e ainda melhora o que o
    # cliente vê — as variantes HLS venciam o desempate de _video_qualities sem
    # trazer filesize, deixando o tamanho em branco em quase toda resolução.
    # O caminho de download NÃO usa este skip: lá o HLS é a única fonte de live.
    opts["extractor_args"] = {
        **opts.get("extractor_args", {}),
        "youtube": {
            **opts.get("extractor_args", {}).get("youtube", {}),
            "skip": ["hls", "dash", "translated_subs"],
        },
    }
    _configure_youtube_runtime(opts, resolved_url)
    _configure_twitter_runtime(opts, resolved_url)
    memory_before = rss_mb()
    extract_started = time.monotonic()
    info = _extract_with_cookie_fallback(
        yt_dlp,
        resolved_url,
        opts,
        download=False,
        result_usable=_info_has_playable_media,
    )
    logger.debug(
        "video_info_extracted host=%s duration_ms=%.0f memory_before_mb=%.1f memory_after_mb=%.1f",
        urlparse(resolved_url).hostname,
        (time.monotonic() - extract_started) * 1000,
        memory_before, rss_mb(),
    )

    # URL de playlist/canal: com playlist_items="1" o yt-dlp extraiu apenas o
    # primeiro item — usa esse vídeo em vez do contêiner da playlist.
    if info and info.get("entries") is not None:
        entries = list(info.get("entries") or [])
        if not entries:
            raise RuntimeError("Não foi possível obter informações do vídeo.")
        info = entries[0]

    formats = [f for f in info.get("formats", []) if f.get("format_id")]
    # Pinterest (e outros) tem pins que sao só foto: yt-dlp não acha nenhum
    # formato de vídeo/áudio, mas devolve a imagem em "thumbnail". Sem isso
    # o cliente tentaria baixar vídeo de algo que não tem vídeo nenhum.
    # YouTube nunca é foto — formatos vazios ali são falha de extração (ex.:
    # bot-check), não um post de imagem. Tratar como foto faria o cliente
    # baixar a thumbnail do vídeo em vez do vídeo em si.
    video_qualities = _video_qualities(formats) or _fallback_video_quality(info, formats)
    audio_qualities = _audio_qualities(formats) or _fallback_audio_quality(formats)
    if not (video_qualities or audio_qualities) and _is_youtube_url(resolved_url):
        logger.warning(
            "youtube_formats_empty url=%s reasons=%s",
            resolved_url, " | ".join(captured_messages) or "sem mensagens do yt-dlp",
        )
        raise RuntimeError("Não foi possível obter as qualidades deste vídeo. Tente novamente em instantes.")
    is_photo = not (video_qualities or audio_qualities) and bool(info.get("thumbnail"))
    if is_photo:
        video_qualities = []
        audio_qualities = []
    def safe_external_url(value: object) -> str | None:
        try:
            return validate_public_http_url(str(value), https_only=True) if value else None
        except UnsafeUrl:
            return None

    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel"),
        "channel_url": safe_external_url(info.get("channel_url") or info.get("uploader_url")),
        "thumbnail": safe_external_url(info.get("thumbnail")),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "upload_date": info.get("upload_date"),
        "description": (info.get("description") or "")[:300],
        "is_photo": is_photo,
        "video_qualities": video_qualities,
        "audio_qualities": audio_qualities,
    }


def _guess_image_ext(url: str, content_type: str | None) -> str:
    path_ext = Path(urlparse(url).path).suffix.lower()
    if path_ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        return path_ext
    mapping = {
        "image/jpeg": ".jpg", "image/png": ".png",
        "image/webp": ".webp", "image/gif": ".gif",
    }
    return mapping.get((content_type or "").split(";")[0].strip().lower(), ".jpg")


def _download_photo(
    download_id: str,
    photo_url: str,
    out_dir: Path,
    max_bytes: int,
    cancelled: Callable[[], bool],
) -> Path:
    """Baixa a foto direto por HTTP: pins de imagem não tem stream de vídeo/áudio pro yt-dlp."""
    validate_public_http_url(photo_url, https_only=True)
    request = Request(photo_url, headers=_HTTP_HEADERS)
    last_push = 0.0
    with open_public_url(request, timeout=30) as response:
        ext = _guess_image_ext(photo_url, response.headers.get("Content-Type"))
        filepath = out_dir / f"{uuid.uuid4().hex}{ext}"
        total = int(response.headers.get("Content-Length") or 0)
        downloaded = 0
        with filepath.open("wb") as fh:
            while True:
                if cancelled():
                    filepath.unlink(missing_ok=True)
                    raise TaskProcessingCancelled()
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if max_bytes and downloaded > max_bytes:
                    filepath.unlink(missing_ok=True)
                    raise RuntimeError("Arquivo excede o tamanho máximo permitido.")
                fh.write(chunk)
                now = time.monotonic()
                if total and now - last_push >= 0.5:
                    last_push = now
                    _store_progress(download_id, round(downloaded * 100 / total, 1), "downloading")
    return filepath


def process_download(download_id: str) -> None:
    import yt_dlp

    db = SessionLocal()
    cancelled = _make_cancel_checker(download_id)
    stop_heartbeat: Callable[[], None] | None = None
    stop_memory_sampler: Callable[[], None] | None = None
    try:
        now = datetime.now(timezone.utc)
        claimed = db.execute(
            update(Download)
            .where(
                Download.id == uuid.UUID(download_id),
                Download.status == DownloadStatus.queued,
                Download.cancel_requested.is_(False),
            )
            .values(
                status=DownloadStatus.processing,
                attempts=Download.attempts + 1,
                worker_id=f"api-process:{os.getpid()}",
                started_at=now,
                heartbeat_at=now,
                progress=0.0,
                stage="processing",
                progress_message=_progress_message("processing"),
                revision=Download.revision + 1,
                updated_at=now,
            )
        )
        db.commit()
        if claimed.rowcount != 1:
            return
        stop_heartbeat = _start_task_heartbeat(download_id)
        stop_memory_sampler = _start_memory_sampler(download_id)
        dl = db.get(Download, uuid.UUID(download_id))
        publish_download_event(dl, "download.started")
        if cancelled():
            raise TaskProcessingCancelled()
        if _resume_pending_storage(db, dl, download_id, cancelled=cancelled):
            return
        started = time.monotonic()

        _ensure_temporary_storage_capacity()
        out_dir = _task_directory(download_id, str(dl.device_id))
        out_dir.mkdir(parents=True, exist_ok=True)

        max_bytes = settings.max_file_size_mb * 1024 * 1024
        download_url = _resolve_platform_url(str(dl.url))
        logger.info(
            "external_download_start task_id=%s host=%s",
            download_id[:8], urlparse(download_url).hostname or "unknown",
        )
        mode = dl.mode or "va"
        requires_compatibility = _requires_compatible_mp4_transcode(
            mode, dl.output_format,
        )

        if mode == "p":
            # Foto (ex.: pin de imagem do Pinterest): sem stream de vídeo/áudio,
            # então não passa pelo pipeline de formato/FFmpeg do yt-dlp.
            _store_progress(download_id, 0.0, "downloading")
            info_opts = {
                **_ytdlp_base_opts(),
                "skip_download": True,
                "socket_timeout": 15,
                # Sem isso o yt-dlp aborta com "No video formats found" antes
                # de entregar o info — e foto não tem formato de vídeo mesmo.
                "ignore_no_formats_error": True,
            }
            _configure_youtube_runtime(info_opts, download_url)
            _configure_twitter_runtime(info_opts, download_url)
            with global_job_slot(
                "download", dl.id, cancelled=cancelled,
                cancel_exception=TaskProcessingCancelled,
            ):
                with yt_dlp.YoutubeDL(info_opts) as ydl:
                    info = ydl.extract_info(download_url, download=False)
                photo_url = info.get("thumbnail")
                if not photo_url:
                    raise RuntimeError("Não foi possível encontrar a foto para baixar.")
                filepath = _download_photo(
                    download_id, photo_url, out_dir, max_bytes, cancelled,
                )
            _store_progress(download_id, 100.0, "converting")
        else:
            # Nome aleatório: impossível adivinhar o caminho de outro usuário.
            out_template = str(out_dir / f"{uuid.uuid4().hex}.%(ext)s")
            opts = {
                **_ytdlp_base_opts(),
                "outtmpl": out_template,
                "format": dl.format_spec,
                "max_filesize": max_bytes,
                "socket_timeout": 30,
                "retries": 3,
                "abort_on_error": True,
                # HLS/DASH: baixa fragmentos em paralelo (grande ganho de velocidade)
                "concurrent_fragment_downloads": 4,
            }

            # FFmpeg auto-detect / auto-download
            ffmpeg_dir = ensure_ffmpeg(
                require_compatibility_codecs=requires_compatibility,
            )
            if ffmpeg_dir:
                opts["ffmpeg_location"] = ffmpeg_dir
            _configure_youtube_runtime(opts, download_url)
            _configure_twitter_runtime(opts, download_url)

            # Progresso: "va" baixa 2 streams (vídeo + áudio) em sequência.
            # Vídeo costuma ser 90%+ dos bytes, então dividir a barra 50/50 por
            # ARQUIVO faz ela "saltar" rápido no áudio (pequeno) logo depois de
            # avançar devagar no vídeo (grande). Pondera por BYTES sempre que o
            # tamanho combinado dos dois formatos é conhecido de antemão
            # (info_dict.requested_formats, já resolvido antes do 1º byte);
            # sem isso, cai para a divisão por contagem de arquivos de sempre.
            expected_files = 2 if mode == "va" else 1
            progress_ceiling = 95.0 if requires_compatibility else 100.0
            progress_state = {
                "done_files": 0,
                "bytes_done": 0,
                "last_push": 0.0,
                "last_percent": 0.0,
            }

            def _progress_hook(d: dict) -> None:
                if cancelled():
                    raise TaskProcessingCancelled()
                if d.get("status") == "finished":
                    progress_state["done_files"] = min(
                        progress_state["done_files"] + 1, expected_files)
                    progress_state["bytes_done"] += (
                        d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    )
                    if progress_state["done_files"] >= expected_files:
                        progress_state["last_percent"] = progress_ceiling
                        _store_progress(
                            download_id, progress_ceiling, "converting",
                        )
                    return
                if d.get("status") != "downloading":
                    return
                if progress_state["done_files"] >= expected_files:
                    return
                now = time.monotonic()
                if now - progress_state["last_push"] < 0.5:
                    return
                progress_state["last_push"] = now
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                combined_hint = 0
                if expected_files > 1 and progress_state["done_files"] == 0:
                    requested = (d.get("info_dict") or {}).get("requested_formats") or []
                    sizes = [
                        f.get("filesize") or f.get("filesize_approx") or 0
                        for f in requested
                    ]
                    if len(sizes) == expected_files and all(sizes):
                        combined_hint = sum(sizes)
                overall_raw = _weighted_stream_progress(
                    progress_state["bytes_done"], downloaded, total, combined_hint,
                )
                if overall_raw is not None:
                    overall = _download_progress_percent(
                        0, overall_raw, 1,
                        ceiling=progress_ceiling,
                        previous=progress_state["last_percent"],
                    )
                else:
                    current = downloaded * 100 / total if total else 0.0
                    overall = _download_progress_percent(
                        progress_state["done_files"],
                        current,
                        expected_files,
                        ceiling=progress_ceiling,
                        previous=progress_state["last_percent"],
                    )
                progress_state["last_percent"] = overall
                _store_progress(download_id, overall, "downloading")

            opts["progress_hooks"] = [_progress_hook]
            _store_progress(download_id, 0.0, "downloading")
            if mode == "va":
                opts["merge_output_format"] = dl.output_format or "mp4"
            elif mode == "v":
                opts["postprocessors"] = [{
                    "key": "FFmpegVideoRemuxer",
                    "preferedformat": dl.output_format or "mp4",
                }]
            else:
                opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": dl.output_format or "mp3",
                    "preferredquality": "0",
                }]

            with global_job_slot(
                "download", dl.id, cancelled=cancelled,
                cancel_exception=TaskProcessingCancelled,
            ), global_job_slot(
                "ffmpeg", dl.id, cancelled=cancelled,
                cancel_exception=TaskProcessingCancelled,
            ):
                def build_download_result(ydl, extracted_info):
                    prepared = Path(ydl.prepare_filename(extracted_info))
                    # merge pode trocar a extensão
                    if not prepared.exists():
                        candidates = list(out_dir.glob(f"{prepared.stem}.*"))
                        if not candidates:
                            raise RuntimeError("Arquivo final não encontrado após o download.")
                        prepared = candidates[0]
                    return extracted_info, prepared

                try:
                    info, filepath = _extract_with_cookie_fallback(
                        yt_dlp,
                        download_url,
                        opts,
                        download=True,
                        result_builder=build_download_result,
                    )
                except Exception:
                    if cancelled():
                        raise TaskProcessingCancelled()
                    raise

        if cancelled():
            raise TaskProcessingCancelled()
        dl.title = info.get("title")
        # Só o título é usado daqui em diante: solta o dicionário completo do
        # yt-dlp (todos os formatos/URLs) antes do upload, que pode levar minutos.
        del info
        ydl = None
        _release_memory_before_conversion()
        if requires_compatibility:
            _store_progress(download_id, 95.0, "converting")
            ffmpeg = _compatibility_ffmpeg_executable(ffmpeg_dir)
            with global_job_slot(
                "ffmpeg", dl.id, cancelled=cancelled,
                cancel_exception=TaskProcessingCancelled,
            ):
                filepath = _compatibilize_mp4_or_keep_original(
                    download_id, ffmpeg, filepath, cancelled,
                )
        if cancelled():
            raise TaskProcessingCancelled()
        dl.heartbeat_at = datetime.now(timezone.utc)
        storage_service.persist_completed_output(
            db, dl, filepath, cancel_requested=cancelled,
        )
        dl.processing_time = round(time.monotonic() - started, 2)
        dl.completed_at = datetime.now(timezone.utc)
        dl.heartbeat_at = dl.completed_at
        dl.status = DownloadStatus.completed
        dl.progress = 100.0
        dl.stage = "completed"
        dl.progress_message = _progress_message("completed")
        dl.error = None
        followers = media_cache.complete_owner(db, dl)
        record_download_completion(db, dl)
        db.commit()
        publish_download_event(dl, "download.completed")
        for follower in followers:
            publish_download_event(follower, "download.completed")
        _cleanup_completed_remote_task(dl)
        state = _progress_last_write.get(download_id)
        if state:
            _log_download_progress(download_id, 100.0, state)
        _clear_progress(download_id)
        try:
            notify_download_completed(db, dl)
        except Exception as notify_exc:  # noqa: BLE001 - webhook nao pode falhar o download
            import logging
            logging.getLogger(__name__).warning("Webhook falhou: %s", notify_exc)

    except TaskProcessingCancelled:
        db.rollback()
        dl = db.get(Download, uuid.UUID(download_id))
        if dl:
            dl.status = DownloadStatus.cancelled
            dl.stage = "cancelled"
            dl.progress_message = _progress_message("cancelled")
            dl.error = None
            dl.completed_at = datetime.now(timezone.utc)
            followers = media_cache.fail_owner(db, dl, TaskProcessingCancelled())
            db.commit()
            publish_download_event(dl, "download.cancelled")
            for follower in followers:
                publish_download_event(follower, "download.failed")
            cleanup_task_files(download_id, str(dl.device_id))
        _clear_progress(download_id)
    except Exception as exc:  # noqa: BLE001 — status de falha precisa ser gravado
        db.rollback()
        _clear_progress(download_id)
        dl = db.get(Download, uuid.UUID(download_id))
        if dl:
            if _schedule_retry(dl, exc):
                db.commit()
                publish_download_event(dl, "download.queued")
                if not dl.file_path:
                    cleanup_task_files(download_id, str(dl.device_id))
                return
            dl.status = DownloadStatus.failed
            dl.stage = "failed"
            dl.progress_message = _progress_message("failed")
            dl.error = str(exc)[:1000]
            dl.completed_at = datetime.now(timezone.utc)
            followers = media_cache.fail_owner(db, dl, exc)
            db.commit()
            publish_download_event(dl, "download.failed")
            for follower in followers:
                publish_download_event(follower, "download.failed")
            if not dl.file_path and not dl.storage_key:
                cleanup_task_files(download_id, str(dl.device_id))
    finally:
        if stop_heartbeat is not None:
            stop_heartbeat()
        if stop_memory_sampler is not None:
            stop_memory_sampler()
        db.close()
        release_freed_memory()


def process_gif(download_id: str) -> None:
    """Baixa a fonte por link e converte somente o trecho validado para GIF."""
    import yt_dlp

    db = SessionLocal()
    cancelled = _make_cancel_checker(download_id)
    filepath: Path | None = None
    published = False
    stop_heartbeat: Callable[[], None] | None = None
    stop_memory_sampler: Callable[[], None] | None = None

    try:
        now = datetime.now(timezone.utc)
        claimed = db.execute(
            update(Download)
            .where(
                Download.id == uuid.UUID(download_id),
                Download.mode == "gif",
                Download.status == DownloadStatus.queued,
                Download.cancel_requested.is_(False),
            )
            .values(
                status=DownloadStatus.processing,
                attempts=Download.attempts + 1,
                worker_id=f"api-process:{os.getpid()}",
                started_at=now,
                heartbeat_at=now,
                progress=0.0,
                stage="processing",
                progress_message=_progress_message("processing"),
                revision=Download.revision + 1,
                updated_at=now,
            )
        )
        db.commit()
        if claimed.rowcount != 1:
            return
        stop_heartbeat = _start_task_heartbeat(download_id)
        stop_memory_sampler = _start_memory_sampler(download_id)
        dl = db.get(Download, uuid.UUID(download_id))
        if _resume_pending_storage(db, dl, download_id, cancelled=cancelled):
            published = True
            return
        queue_wait_seconds = max(
            0.0, (now - (dl.queued_at or dl.created_at)).total_seconds(),
        )
        publish_download_event(dl, "download.started")
        options = GifOptions.model_validate_json(dl.conversion_options or "{}")
        if cancelled():
            raise GifConversionCancelled()

        started = time.monotonic()
        _ensure_temporary_storage_capacity()
        out_dir = _task_directory(download_id, str(dl.device_id))
        out_dir.mkdir(parents=True, exist_ok=True)
        download_url = _resolve_platform_url(str(dl.url))
        logger.info(
            "external_download_start task_id=%s host=%s",
            download_id[:8], urlparse(download_url).hostname or "unknown",
        )
        ffmpeg_dir = ensure_ffmpeg()
        if not ffmpeg_dir:
            raise RuntimeError("FFmpeg indisponível no servidor.")
        ffmpeg = str(Path(ffmpeg_dir) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"))
        ffprobe = str(Path(ffmpeg_dir) / ("ffprobe.exe" if os.name == "nt" else "ffprobe"))

        with tempfile.TemporaryDirectory(prefix=f"gif-{download_id}-", dir=out_dir) as temp_name:
            temp_dir = Path(temp_name)
            opts = {
                **_ytdlp_base_opts(),
                "outtmpl": str(temp_dir / "source.%(ext)s"),
                "format": dl.format_spec,
                "merge_output_format": "mp4",
                "max_filesize": settings.max_file_size_mb * 1024 * 1024,
                "socket_timeout": 30,
                "retries": 3,
                "abort_on_error": True,
                "concurrent_fragment_downloads": 4,
                "ffmpeg_location": ffmpeg_dir,
            }
            _configure_youtube_runtime(opts, download_url)
            _configure_twitter_runtime(opts, download_url)

            last_push = {"at": 0.0}

            def download_progress(data: dict) -> None:
                if cancelled():
                    raise GifConversionCancelled()
                if data.get("status") != "downloading":
                    return
                now = time.monotonic()
                if now - last_push["at"] < 0.5:
                    return
                last_push["at"] = now
                total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                current = data.get("downloaded_bytes") or 0
                percent = current * 55 / total if total else 0
                _store_progress(download_id, round(percent, 1), "downloading")

            opts["progress_hooks"] = [download_progress]
            _store_progress(download_id, 0.0, "downloading")
            try:
                with global_job_slot(
                    "download", dl.id, cancelled=cancelled,
                    cancel_exception=GifConversionCancelled,
                ), global_job_slot(
                    "ffmpeg", dl.id, cancelled=cancelled,
                    cancel_exception=GifConversionCancelled,
                ):
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(download_url, download=True)
            except Exception:
                if cancelled():
                    raise GifConversionCancelled()
                raise
            # Libera o dicionário completo do yt-dlp antes da conversão/upload;
            # só o título é necessário no final.
            source_title = info.get("title") or "video"
            del info

            sources = [
                path for path in temp_dir.iterdir()
                if path.is_file() and path.suffix not in {".part", ".ytdl", ".json"}
            ]
            if not sources:
                raise RuntimeError("O vídeo de origem não foi encontrado.")
            source = max(sources, key=lambda path: path.stat().st_size)
            probe = probe_video(ffprobe, source)
            if probe["duration"] and options.end > probe["duration"] + 0.25:
                raise RuntimeError("O trecho escolhido ultrapassa a duração do vídeo.")
            if probe["width"] > 7680 or probe["height"] > 4320:
                raise RuntimeError("A resolução do vídeo excede o limite permitido.")

            def _attempt_progress(attempt: int, value: float, fps_reduced: bool) -> None:
                if attempt == 0:
                    stage = "processing"
                elif fps_reduced:
                    stage = "optimizing_fps"
                else:
                    stage = "optimizing"
                base = 55.0 if attempt == 0 else 75.0
                span = 40.0 if attempt == 0 else 20.0
                _store_progress(download_id, round(base + value * span / 100, 1), stage)

            with global_job_slot(
                "ffmpeg", dl.id, cancelled=cancelled,
                cancel_exception=GifConversionCancelled,
            ):
                generated, final_options, conversion_stats = convert_within_limit(
                    ffmpeg, source, temp_dir, options,
                    probe["width"], probe["height"],
                    _attempt_progress, cancelled,
                    **_gif_conversion_limits(),
                )

            filepath = out_dir / f"{uuid.uuid4().hex}.gif"
            _publish_generated_file(generated, filepath, cancelled)
            if final_options.resolution == "original":
                result_width, result_height = probe["width"], probe["height"]
            elif final_options.resolution == "custom":
                result_width = final_options.custom_width
                result_height = final_options.custom_height
            else:
                result_height = int(final_options.resolution.removesuffix("p"))
                ratio = probe["width"] / probe["height"] if probe["height"] else 16 / 9
                result_width = max(2, round((result_height * ratio) / 2) * 2)

            result = {
                "size": filepath.stat().st_size,
                "width": result_width,
                "height": result_height,
                "fps": final_options.fps,
                "duration": round((final_options.end - final_options.start) / final_options.speed, 2),
                "attempts": conversion_stats.encodings,
                "ffmpeg_processes": conversion_stats.ffmpeg_processes,
            }
            logger.info(
                "gif_conversion_metrics result=completed attempts=%s ffmpeg_processes=%s "
                "palette_seconds=%.3f gif_seconds=%.3f cleanup_seconds=%.3f "
                "retry_reasons=%s input_bytes=%s output_bytes=%s fps=%s "
                "resolution=%sx%s colors=%s queue_wait_seconds=%.3f",
                conversion_stats.encodings, conversion_stats.ffmpeg_processes,
                conversion_stats.palette_seconds, conversion_stats.gif_seconds,
                conversion_stats.cleanup_seconds, len(conversion_stats.retry_reasons),
                source.stat().st_size, result["size"], final_options.fps,
                result_width, result_height, final_options.colors, queue_wait_seconds,
            )
            payload = options.model_dump()
            payload["result"] = result
            dl.conversion_options = json.dumps(payload)
            storage_service.persist_source_video(
                db, dl, source,
                downloaded_source=True,
                cancel_requested=cancelled,
            )

        dl.title = f"{source_title} - GIF"
        dl.heartbeat_at = datetime.now(timezone.utc)
        published = True
        storage_service.persist_completed_output(
            db, dl, filepath, cancel_requested=cancelled,
        )
        dl.processing_time = round(time.monotonic() - started, 2)
        dl.completed_at = datetime.now(timezone.utc)
        dl.heartbeat_at = dl.completed_at
        dl.status = DownloadStatus.completed
        dl.progress = 100.0
        dl.stage = "completed"
        dl.progress_message = _progress_message("completed")
        dl.error = None
        followers = media_cache.complete_owner(db, dl)
        record_download_completion(db, dl)
        db.commit()
        publish_download_event(dl, "download.completed")
        for follower in followers:
            publish_download_event(follower, "download.completed")
        _cleanup_completed_remote_task(dl)
        _clear_progress(download_id)

    except GifConversionCancelled:
        db.rollback()
        dl = db.get(Download, uuid.UUID(download_id))
        if dl:
            dl.status = DownloadStatus.cancelled
            dl.stage = "cancelled"
            dl.progress_message = _progress_message("cancelled")
            dl.error = None
            dl.completed_at = datetime.now(timezone.utc)
            followers = media_cache.fail_owner(db, dl, GifConversionCancelled())
            db.commit()
            publish_download_event(dl, "download.cancelled")
            for follower in followers:
                publish_download_event(follower, "download.failed")
            cleanup_task_files(download_id, str(dl.device_id))
        _clear_progress(download_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("gif_conversion_failed type=%s", type(exc).__name__)
        db.rollback()
        _clear_progress(download_id)
        dl = db.get(Download, uuid.UUID(download_id))
        if dl:
            if _schedule_retry(dl, exc):
                db.commit()
                publish_download_event(dl, "download.queued")
                if not dl.file_path:
                    cleanup_task_files(download_id, str(dl.device_id))
                return
            dl.status = DownloadStatus.failed
            dl.stage = "failed"
            dl.progress_message = _progress_message("failed")
            safe_prefixes = (
                "FFmpeg indisponível", "O vídeo de origem", "O trecho escolhido",
                "A resolução", "Não foi possível gerar", "Não foi possível atingir",
                "A conversão excedeu",
            )
            message = str(exc)
            dl.error = (
                message[:500] if message.startswith(safe_prefixes)
                else "Não foi possível processar este vídeo. Tente outro link ou trecho."
            )
            followers = media_cache.fail_owner(db, dl, exc)
            db.commit()
            publish_download_event(dl, "download.failed")
            for follower in followers:
                publish_download_event(follower, "download.failed")
            if not dl.file_path and not dl.storage_key:
                cleanup_task_files(download_id, str(dl.device_id))
    finally:
        if stop_heartbeat is not None:
            stop_heartbeat()
        if stop_memory_sampler is not None:
            stop_memory_sampler()
        db.close()
        if filepath is not None and not published:
            filepath.unlink(missing_ok=True)
        release_freed_memory()


def _temporary_failure(exc: Exception) -> bool:
    if isinstance(exc, (StorageTemporaryError, TimeoutError, URLError, OSError)):
        return True
    message = str(exc).lower()
    return any(token in message for token in (
        "timed out", "timeout", "temporarily unavailable", "connection reset",
        "connection aborted", "try again", "storage unavailable",
    ))


def _schedule_retry(download: Download, exc: Exception) -> bool:
    """Persiste backoff no próprio registro, sem backend externo de resultados."""
    if not _temporary_failure(exc) or download.attempts > settings.task_max_retries:
        return False
    delay = min(30 * (2 ** max(download.attempts - 1, 0)), 300)
    download.status = DownloadStatus.queued
    download.stage = "retrying"
    download.progress_message = _progress_message("retrying")
    download.error = None
    download.worker_id = None
    download.heartbeat_at = None
    download.queued_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    return True


def process_gif_upload(download_id: str) -> None:
    """Converte o upload já persistido; o executor recebe somente o ID."""
    db = SessionLocal()
    source: Path | None = None
    filepath: Path | None = None
    keep_source = False
    published = False
    cancelled = _make_cancel_checker(download_id)
    stop_heartbeat: Callable[[], None] | None = None
    stop_memory_sampler: Callable[[], None] | None = None

    try:
        now = datetime.now(timezone.utc)
        claimed = db.execute(
            update(Download)
            .where(
                Download.id == uuid.UUID(download_id),
                Download.mode == "gif",
                Download.status == DownloadStatus.queued,
                Download.cancel_requested.is_(False),
            )
            .values(
                status=DownloadStatus.processing,
                attempts=Download.attempts + 1,
                worker_id=f"api-process:{os.getpid()}",
                started_at=now,
                heartbeat_at=now,
                progress=0.0,
                stage="processing",
                progress_message=_progress_message("processing"),
                error=None,
                revision=Download.revision + 1,
                updated_at=now,
            )
        )
        db.commit()
        if claimed.rowcount != 1:
            return
        stop_heartbeat = _start_task_heartbeat(download_id)
        stop_memory_sampler = _start_memory_sampler(download_id)

        dl = db.get(Download, uuid.UUID(download_id))
        if dl and _resume_pending_storage(db, dl, download_id, cancelled=cancelled):
            published = True
            return
        if not dl or not dl.source_path:
            raise RuntimeError("O video enviado nao esta mais disponivel.")
        queue_wait_seconds = max(
            0.0, (now - (dl.queued_at or dl.created_at)).total_seconds(),
        )
        publish_download_event(dl, "download.started")
        source = safe_storage_file(dl.source_path)
        if source is None:
            raise RuntimeError("O video enviado nao esta mais disponivel.")
        options = GifOptions.model_validate_json(dl.conversion_options or "{}")
        if cancelled():
            raise GifConversionCancelled()

        started = time.monotonic()
        ffmpeg_dir = ensure_ffmpeg()
        if not ffmpeg_dir:
            raise RuntimeError("FFmpeg indisponivel no servidor.")
        ffmpeg = str(Path(ffmpeg_dir) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"))
        ffprobe = str(Path(ffmpeg_dir) / ("ffprobe.exe" if os.name == "nt" else "ffprobe"))
        probe = probe_video(ffprobe, source)
        if probe["duration"] and options.end > probe["duration"] + 0.25:
            raise RuntimeError("O trecho escolhido ultrapassa a duracao do video.")
        if probe["width"] > 7680 or probe["height"] > 4320:
            raise RuntimeError("A resolucao do video excede o limite permitido.")

        _ensure_temporary_storage_capacity()
        out_dir = _task_directory(download_id, str(dl.device_id))
        out_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"gif-{download_id}-", dir=out_dir) as temp_name:
            temp_dir = Path(temp_name)

            def _attempt_progress(attempt: int, value: float, fps_reduced: bool) -> None:
                if attempt == 0:
                    stage = "processing"
                elif fps_reduced:
                    stage = "optimizing_fps"
                else:
                    stage = "optimizing"
                base = 5.0 if attempt == 0 else 72.0
                span = 90.0 if attempt == 0 else 23.0
                _store_progress(download_id, round(base + value * span / 100, 1), stage)

            with global_job_slot(
                "ffmpeg", dl.id, cancelled=cancelled,
                cancel_exception=GifConversionCancelled,
            ):
                generated, final_options, conversion_stats = convert_within_limit(
                    ffmpeg, source, temp_dir, options,
                    probe["width"], probe["height"],
                    _attempt_progress, cancelled,
                    **_gif_conversion_limits(),
                )
            filepath = out_dir / f"{uuid.uuid4().hex}.gif"
            _publish_generated_file(generated, filepath, cancelled)

        if final_options.resolution == "original":
            result_width, result_height = probe["width"], probe["height"]
        elif final_options.resolution == "custom":
            result_width = final_options.custom_width
            result_height = final_options.custom_height
        else:
            result_height = int(final_options.resolution.removesuffix("p"))
            ratio = probe["width"] / probe["height"] if probe["height"] else 16 / 9
            result_width = max(2, round((result_height * ratio) / 2) * 2)

        result = {
            "size": filepath.stat().st_size,
            "width": result_width,
            "height": result_height,
            "fps": final_options.fps,
            "duration": round((final_options.end - final_options.start) / final_options.speed, 2),
            "attempts": conversion_stats.encodings,
            "ffmpeg_processes": conversion_stats.ffmpeg_processes,
        }
        logger.info(
            "gif_conversion_metrics result=completed attempts=%s ffmpeg_processes=%s "
            "palette_seconds=%.3f gif_seconds=%.3f cleanup_seconds=%.3f "
            "retry_reasons=%s input_bytes=%s output_bytes=%s fps=%s "
            "resolution=%sx%s colors=%s queue_wait_seconds=%.3f",
            conversion_stats.encodings, conversion_stats.ffmpeg_processes,
            conversion_stats.palette_seconds, conversion_stats.gif_seconds,
            conversion_stats.cleanup_seconds, len(conversion_stats.retry_reasons),
            source.stat().st_size, result["size"], final_options.fps,
            result_width, result_height, final_options.colors, queue_wait_seconds,
        )
        payload = options.model_dump()
        payload["result"] = result
        dl.conversion_options = json.dumps(payload)
        storage_service.persist_source_video(
            db, dl, source,
            downloaded_source=False,
            cancel_requested=cancelled,
        )
        dl.title = f"{source.stem} - GIF"
        dl.heartbeat_at = datetime.now(timezone.utc)
        published = True
        keep_source = True
        storage_service.persist_completed_output(
            db, dl, filepath, cancel_requested=cancelled,
        )
        dl.processing_time = round(time.monotonic() - started, 2)
        dl.completed_at = datetime.now(timezone.utc)
        dl.heartbeat_at = dl.completed_at
        dl.source_path = None
        dl.status = DownloadStatus.completed
        dl.progress = 100.0
        dl.stage = "completed"
        dl.progress_message = _progress_message("completed")
        dl.error = None
        record_download_completion(db, dl)
        db.commit()
        keep_source = False
        publish_download_event(dl, "download.completed")
        _cleanup_completed_remote_task(dl)

    except GifConversionCancelled:
        db.rollback()
        dl = db.get(Download, uuid.UUID(download_id))
        if dl:
            dl.status = DownloadStatus.cancelled
            dl.error = None
            dl.stage = "cancelled"
            dl.progress_message = _progress_message("cancelled")
            dl.source_path = None
            dl.completed_at = datetime.now(timezone.utc)
            db.commit()
            publish_download_event(dl, "download.cancelled")
            cleanup_task_files(download_id, str(dl.device_id))
    except Exception as exc:
        db.rollback()
        dl = db.get(Download, uuid.UUID(download_id))
        if dl and _schedule_retry(dl, exc):
            db.commit()
            publish_download_event(dl, "download.queued")
            keep_source = True
            return
        if dl:
            dl.status = DownloadStatus.failed
            dl.stage = "failed"
            dl.progress_message = _progress_message("failed")
            dl.source_path = None
            safe_message = str(exc)
            safe_prefixes = (
                "O video enviado", "O trecho escolhido", "A resolucao",
                "FFmpeg indisponivel", "Nao foi possivel atingir",
            )
            dl.error = (
                safe_message[:500]
                if safe_message.startswith(safe_prefixes)
                else "Nao foi possivel processar este video. Tente outro arquivo ou trecho."
            )
            dl.completed_at = datetime.now(timezone.utc)
            db.commit()
            publish_download_event(dl, "download.failed")
            if not dl.file_path and not dl.storage_key:
                cleanup_task_files(download_id, str(dl.device_id))
    finally:
        _clear_progress(download_id)
        if stop_heartbeat is not None:
            stop_heartbeat()
        if stop_memory_sampler is not None:
            stop_memory_sampler()
        db.close()
        if source is not None and not keep_source:
            source.unlink(missing_ok=True)
        if filepath is not None and not published:
            filepath.unlink(missing_ok=True)
        release_freed_memory()


def recover_stale_downloads() -> int:
    """Recupera tarefas sem heartbeat com lock e limite de tentativas."""
    from datetime import timedelta

    db = SessionLocal()
    recovered_tasks: list[tuple[str, str]] = []
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.task_stale_seconds)
        stale = (
            db.execute(
                select(Download)
                .where(
                    Download.status == DownloadStatus.processing,
                    or_(Download.cache_role.is_(None), Download.cache_role != "waiter"),
                    or_(Download.heartbeat_at.is_(None), Download.heartbeat_at < cutoff),
                )
                .with_for_update(skip_locked=True)
                .limit(50)
            )
            .scalars()
            .all()
        )
        for dl in stale:
            if dl.attempts >= settings.task_max_retries + 1:
                dl.status = DownloadStatus.failed
                dl.stage = "failed"
                dl.error = "A tarefa foi interrompida repetidamente no worker."
                continue
            dl.status = DownloadStatus.queued
            dl.stage = "retrying"
            dl.worker_id = None
            dl.queued_at = datetime.now(timezone.utc)
            task_name = (
                "workers.process_gif_upload"
                if dl.mode == "gif" and dl.source_path
                else "workers.process_gif" if dl.mode == "gif"
                else "workers.process_download"
            )
            recovered_tasks.append((task_name, str(dl.id)))
        db.commit()
        for dl in stale:
            publish_download_event(
                dl,
                "download.failed" if dl.status == DownloadStatus.failed else "download.queued",
            )
    finally:
        db.close()

    if recovered_tasks:
        from backend.tasks.manager import notify_task_manager

        notify_task_manager()
    return len(recovered_tasks)


def recover_media_cache() -> int:
    """Reconcilia produtores compartilhados interrompidos."""
    return media_cache.recover_stale_entries()


def cleanup_expired_files() -> int:
    """Exclui objetos locais/remotos expirados em lotes idempotentes."""
    removed = 0
    cleanup_db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        legacy_cutoff = now - timedelta(
            hours=settings.download_file_ttl_hours,
        )
        expired = (
            cleanup_db.execute(
                select(Download)
                .where(
                    Download.storage_state != StorageState.deleted.value,
                    or_(
                        Download.expires_at <= now,
                        (
                            Download.expires_at.is_(None)
                            & Download.completed_at.is_not(None)
                            & (Download.completed_at <= legacy_cutoff)
                            & or_(
                                Download.file_path.is_not(None),
                                Download.storage_key.is_not(None),
                            )
                        ),
                    ),
                )
                .order_by(Download.expires_at.asc().nulls_last())
                .with_for_update(skip_locked=True)
                .limit(200)
            )
            .scalars()
            .all()
        )
        for dl in expired:
            try:
                removed += int(storage_service.delete_download_file(cleanup_db, dl))
            except Exception as exc:  # noqa: BLE001 - mantém delete_pending para retry
                logger.warning("expired_storage_cleanup_failed type=%s", type(exc).__name__)
        cleanup_db.execute(
            delete(SecurityNonce).where(
                SecurityNonce.expires_at <= now
            )
        )
        cleanup_db.commit()
    finally:
        cleanup_db.close()
    return removed


def cleanup_retained_tasks() -> int:
    """Remove registros terminais antigos somente após a saída dos arquivos."""
    db = SessionLocal()
    removed = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=settings.task_retention_time,
        )
        rows = db.execute(
            select(Download)
            .where(
                Download.status.in_({
                    DownloadStatus.completed,
                    DownloadStatus.failed,
                    DownloadStatus.cancelled,
                }),
                Download.updated_at <= cutoff,
                Download.file_path.is_(None),
                Download.storage_key.is_(None),
                Download.source_path.is_(None),
                Download.source_storage_key.is_(None),
            )
            .order_by(Download.updated_at)
            .with_for_update(skip_locked=True)
            .limit(200)
        ).scalars().all()
        for download in rows:
            cleanup_task_files(str(download.id), str(download.device_id))
            db.delete(download)
            removed += 1
        local_cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        local_result = db.execute(
            delete(LocalOperation).where(
                LocalOperation.updated_at <= local_cutoff,
                LocalOperation.status.in_({
                    "authorized", "completed", "failed", "cancelled",
                }),
            )
        )
        removed += int(local_result.rowcount or 0)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return removed


def migrate_fallback_storage() -> int:
    """Tenta migrar cópias locais de fallback sem repetir o processamento."""
    if settings.storage_backend != "google_drive":
        return 0
    db = SessionLocal()
    migrated = 0
    try:
        abandoned_cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=settings.remote_upload_timeout_seconds + 60,
        )
        candidate_ids = db.execute(
            select(Download.id)
            .where(
                or_(
                    Download.storage_state == StorageState.local_fallback.value,
                    and_(
                        Download.storage_state.in_({
                            StorageState.remote_ready.value,
                            StorageState.local_delete_pending.value,
                        }),
                        Download.storage_key.is_not(None),
                    ),
                    and_(
                        Download.storage_state == StorageState.remote_uploading.value,
                        Download.updated_at < abandoned_cutoff,
                    ),
                ),
                Download.file_path.is_not(None),
                or_(
                    Download.storage_error.is_(None),
                    Download.storage_error != "StorageAuthorizationError",
                ),
            )
            .order_by(Download.updated_at)
            .limit(20)
        ).scalars().all()
        for candidate_id in candidate_ids:
            dl = db.execute(
                select(Download)
                .where(Download.id == candidate_id)
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            reclaimable = bool(
                dl
                and (
                    dl.storage_state == StorageState.local_fallback.value
                    or (
                        dl.storage_state in {
                            StorageState.remote_ready.value,
                            StorageState.local_delete_pending.value,
                        }
                        and dl.storage_key
                    )
                    or (
                        dl.storage_state == StorageState.remote_uploading.value
                        and dl.updated_at < abandoned_cutoff
                    )
                )
            )
            if not reclaimable:
                db.rollback()
                continue
            recovery_expires_at = dl.expires_at
            if dl.storage_state not in {
                StorageState.remote_ready.value,
                StorageState.local_delete_pending.value,
            }:
                dl.storage_state = StorageState.remote_uploading.value
            db.commit()
            path = safe_storage_file(dl.file_path)
            if path is None:
                if (
                    dl.storage_key
                    and dl.storage_state in {
                        StorageState.remote_ready.value,
                        StorageState.local_delete_pending.value,
                    }
                ):
                    storage_service.finalize_confirmed_remote_output(db, dl, None)
                    migrated += 1
                    continue
                dl.storage_state = StorageState.local_fallback.value
                dl.storage_error = "LocalFallbackMissing"
                db.commit()
                continue
            try:
                metadata = storage_service.persist_completed_output(db, dl, path)
                if metadata.backend == "google_drive":
                    migrated += 1
                else:
                    dl.expires_at = recovery_expires_at
                    db.commit()
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                dl = db.get(Download, candidate_id)
                if dl is None:
                    continue
                if (
                    dl.storage_backend == "google_drive"
                    and dl.storage_key
                    and dl.storage_state in {
                        StorageState.remote_ready.value,
                        StorageState.local_delete_pending.value,
                    }
                ):
                    dl.storage_error = type(exc).__name__
                    dl.expires_at = recovery_expires_at
                    db.commit()
                    continue
                dl.storage_backend = "local"
                dl.storage_state = StorageState.local_fallback.value
                dl.storage_key = str(path)
                dl.storage_error = type(exc).__name__
                dl.expires_at = recovery_expires_at
                db.commit()
    finally:
        db.close()
    return migrated


def retry_storage_upload(download_id: str) -> None:
    """Nova tentativa administrativa sem baixar ou converter novamente."""
    db = SessionLocal()
    stop_heartbeat: Callable[[], None] = lambda: None
    try:
        dl = db.execute(
            select(Download)
            .where(Download.id == uuid.UUID(download_id))
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        if not dl:
            return
        now = datetime.now(timezone.utc)
        heartbeat = dl.heartbeat_at
        if heartbeat and heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        if (
            dl.status == DownloadStatus.processing
            and heartbeat
            and heartbeat > now - timedelta(minutes=15)
        ):
            return
        if not dl.file_path and not (
            dl.storage_key
            and dl.storage_state in {
                StorageState.remote_ready.value,
                StorageState.local_delete_pending.value,
            }
        ):
            return
        dl.status = DownloadStatus.processing
        dl.stage = "storage_uploading"
        dl.error = None
        dl.heartbeat_at = now
        db.commit()
        stop_heartbeat = _start_task_heartbeat(download_id)
        if not _resume_pending_storage(db, dl, download_id):
            path = safe_storage_file(dl.file_path)
            if path is None:
                raise RuntimeError("Arquivo local de recuperação indisponível.")
            storage_service.persist_completed_output(
                db, dl, path,
            )
            dl.status = DownloadStatus.completed
            dl.stage = "completed"
            dl.progress = 100.0
            dl.error = None
            dl.completed_at = dl.completed_at or datetime.now(timezone.utc)
            record_download_completion(db, dl)
            db.commit()
            publish_download_event(dl, "download.completed")
            _cleanup_completed_remote_task(dl)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        dl = db.get(Download, uuid.UUID(download_id))
        if dl:
            dl.status = DownloadStatus.failed
            dl.stage = "storage_failed"
            dl.error = "Falha ao persistir o arquivo no armazenamento privado."
            dl.storage_error = type(exc).__name__
            db.commit()
    finally:
        stop_heartbeat()
        db.close()


def audit_remote_storage() -> int:
    """Audita uma página gerenciada e remove somente órfãos antigos do app."""
    if settings.storage_backend != "google_drive":
        return 0
    db = SessionLocal()
    removed = 0
    release_audit_lock: Callable[[], None] | None = None
    try:
        release_audit_lock = storage_service.acquire_remote_audit_lock(db)
        if release_audit_lock is None:
            return 0
        cursor = db.get(StorageCursor, "google_drive_managed")
        try:
            files, next_page = storage_service.scan_managed_remote_files(
                cursor.cursor if cursor else None,
            )
        except Exception as exc:  # noqa: BLE001
            if cursor is not None:
                cursor.cursor = None
                db.commit()
            logger.warning("remote_storage_audit_failed type=%s", type(exc).__name__)
            return 0

        keys = [str(item.get("id") or "") for item in files if item.get("id")]
        rows = (
            db.execute(select(Download).where(or_(
                Download.storage_key.in_(keys),
                Download.source_storage_key.in_(keys),
                Download.storage_reserved_key.in_(keys),
                Download.source_storage_reserved_key.in_(keys),
            )))
            .scalars()
            .all()
            if keys else []
        )
        cache_rows = (
            db.execute(select(MediaCache).where(or_(
                MediaCache.storage_key.in_(keys),
                MediaCache.storage_reserved_key.in_(keys),
            ))).scalars().all()
            if keys else []
        )
        by_key: dict[str, tuple[Download, bool]] = {}
        cache_by_key: dict[str, MediaCache] = {}
        protected_keys: set[str] = set()
        for row in rows:
            if row.storage_key:
                by_key[str(row.storage_key)] = (row, False)
                protected_keys.add(str(row.storage_key))
            if row.source_storage_key:
                by_key[str(row.source_storage_key)] = (row, True)
                protected_keys.add(str(row.source_storage_key))
            if row.storage_reserved_key:
                protected_keys.add(str(row.storage_reserved_key))
            if row.source_storage_reserved_key:
                protected_keys.add(str(row.source_storage_reserved_key))
        for cache in cache_rows:
            if cache.storage_key:
                cache_by_key[str(cache.storage_key)] = cache
                protected_keys.add(str(cache.storage_key))
            if cache.storage_reserved_key:
                protected_keys.add(str(cache.storage_reserved_key))
        audit_errors = storage_service.audit_managed_remote_keys(
            list(set(by_key) | set(cache_by_key)),
        )
        now = datetime.now(timezone.utc)
        unsafe_sources: list[str] = []
        unsafe_cache: list[str] = []
        for key, error_type in audit_errors.items():
            cache = cache_by_key.get(key)
            if cache is not None:
                cache.last_error = error_type
                if error_type in {"StorageSecurityError", "StorageNotFoundError"}:
                    if error_type == "StorageSecurityError":
                        unsafe_cache.append(key)
                    media_cache.invalidate_remote_entry(db, cache, error_type)
                continue
            reference = by_key.get(key)
            if reference is None:
                continue
            row, is_source = reference
            if is_source:
                row.storage_error = f"source:{error_type}"
                if error_type == "StorageSecurityError":
                    unsafe_sources.append(key)
                elif error_type == "StorageNotFoundError":
                    row.source_storage_backend = None
                    row.source_storage_key = None
                    row.source_storage_reserved_key = None
                    row.source_remote_parent_id = None
                    row.source_remote_size = None
                    row.source_remote_checksum = None
                    row.source_remote_mime_type = None
                    row.source_remote_uploaded_at = None
                continue
            row.storage_error = error_type
            if error_type == "StorageSecurityError":
                row.storage_state = StorageState.security_blocked.value
            elif error_type == "StorageNotFoundError":
                row.storage_state = StorageState.remote_missing.value
                row.file_deleted_at = now

        unsafe_source_errors = storage_service.delete_managed_remote_keys(
            unsafe_sources + unsafe_cache,
        )
        for key in unsafe_sources:
            if key in unsafe_source_errors:
                continue
            reference = by_key.get(key)
            if reference is None:
                continue
            row, _ = reference
            row.source_storage_backend = None
            row.source_storage_key = None
            row.source_storage_reserved_key = None
            row.source_remote_parent_id = None
            row.source_remote_size = None
            row.source_remote_checksum = None
            row.source_remote_mime_type = None
            row.source_remote_uploaded_at = None

        orphan_keys: list[str] = []
        cutoff = now - timedelta(hours=1)
        for item in files:
            key = str(item.get("id") or "")
            if not key or key in protected_keys:
                continue
            try:
                created = datetime.fromisoformat(
                    str(item.get("createdTime") or "").replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if created <= cutoff:
                orphan_keys.append(key)
        delete_errors = storage_service.delete_managed_remote_keys(orphan_keys)
        removed = len(orphan_keys) - len(delete_errors)

        if cursor is None:
            cursor = StorageCursor(key="google_drive_managed")
            db.add(cursor)
        cursor.cursor = next_page
        cursor.updated_at = now
        db.commit()
        privacy_alerts = sum(
            1 for error_type in audit_errors.values()
            if error_type == "StorageSecurityError"
        )
        if privacy_alerts:
            logger.error("remote_storage_privacy_audit_alert count=%s", privacy_alerts)
        return removed
    finally:
        if release_audit_lock is not None:
            release_audit_lock()
        db.close()


def cleanup_orphaned_files() -> int:
    """Varredura de segurança diária para resíduos sem registro persistente."""
    root = Path(settings.downloads_dir)
    if not root.is_dir():
        return 0
    db = SessionLocal()
    cutoff = time.time() - settings.temp_file_retention
    removed = 0
    directories: list[Path] = []
    candidates: list[Path] = []

    def flush_candidates() -> None:
        nonlocal removed
        if not candidates:
            return
        values = [str(path) for path in candidates]
        registered: set[str] = set()
        for column in (Download.file_path, Download.source_path):
            registered.update(
                str(Path(path))
                for path in db.execute(
                    select(column).where(column.in_(values))
                ).scalars()
                if path
            )
        for path in candidates:
            if str(path) in registered:
                continue
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError as exc:
                logger.warning("orphan_cleanup_failed type=%s", type(exc).__name__)
        candidates.clear()

    try:
        for entry in root.rglob("*"):
            try:
                if entry.is_symlink():
                    entry.unlink(missing_ok=True)
                    removed += 1
                elif entry.is_dir():
                    directories.append(entry)
                elif (
                    entry.is_file()
                    # Última barreira: a varredura jamais apaga código-fonte,
                    # mesmo que DOWNLOADS_DIR seja apontado errado.
                    and entry.suffix not in _SOURCE_SUFFIXES
                    and entry.stat().st_mtime <= cutoff
                ):
                    temporary = (
                        entry.suffix in {".part", ".ytdl"}
                        or entry.name.endswith("_palette.png")
                    )
                    if temporary:
                        entry.unlink(missing_ok=True)
                        removed += 1
                    else:
                        candidates.append(entry)
                        if len(candidates) >= 500:
                            flush_candidates()
            except OSError as exc:
                logger.warning("orphan_cleanup_failed type=%s", type(exc).__name__)
        flush_candidates()
    finally:
        db.close()
    for directory in reversed(directories):
        try:
            directory.rmdir()
        except OSError:
            continue
    return removed


def run_operation(download_id: str, operation_type: str) -> None:
    """Despacha somente operações internas conhecidas; nunca executa texto livre."""
    handlers: dict[str, Callable[[str], None]] = {
        "download": process_download,
        "gif_url": process_gif,
        "gif_upload": process_gif_upload,
        "storage_retry": retry_storage_upload,
    }
    handler = handlers.get(operation_type)
    if handler is None:
        raise ValueError("Tipo interno de tarefa inválido.")
    handler(download_id)
