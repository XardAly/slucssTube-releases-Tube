"""
Endpoints de download.

O cliente NUNCA toca yt-dlp/FFmpeg/worker: ele só cria tarefas e
consulta status. Todo o processamento acontece na rede privada.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.requests import ClientDisconnect
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.api.deps import AuthContext, get_auth_context
from backend.api.config import get_settings
from backend.database.models import DeviceStatus, Download, DownloadStatus, LocalOperation
from backend.database.session import get_db
from backend.downloads import service
from backend.downloads import cache as media_cache
from backend.notifications.discord import notify_local_operation_completed
from backend.storage import service as private_storage
from backend.storage.base import (
    StorageNotFoundError,
    StoragePermanentError,
    StorageRangeNotSatisfiableError,
    StorageTemporaryError,
)
from backend.storage.manager import get_api_storage_backend
from backend.downloads.schemas import (
    ConversionCreateRequest,
    DownloadCreateRequest,
    GifOptions,
    LocalOperationAuthorizeRequest,
    LocalOperationProgressRequest,
    VideoInfoRequest,
)
from backend.observability import release_freed_memory, rss_mb
from backend.security import fraud
from backend.security.challenge import consume_challenge
from backend.security.signing import server_sign
from backend.workers.tasks import _resolve_cookiefile as resolve_cookiefile
from backend.workers.tasks import fetch_info
from backend.realtime import publish_download_event
from backend.tasks.manager import notify_task_manager

router = APIRouter(tags=["downloads"])
settings = get_settings()
logger = logging.getLogger(__name__)
_video_info_slots = threading.BoundedSemaphore(settings.video_info_concurrency)
_video_info_cache: OrderedDict[tuple[str, str], tuple[float, dict]] = OrderedDict()
_video_info_cache_lock = threading.Lock()

# Deduplicação de consultas simultâneas à mesma mídia: só a primeira executa
# o yt-dlp (que spawna um Deno de ~200 MB); as demais aguardam o resultado.
_VIDEO_INFO_WAIT_SECONDS = 60.0
# Com a extração em ~3 s, esperar a vaga vale mais a pena do que recusar: em
# 2 s o segundo usuário a buscar ao mesmo tempo recebia erro antes mesmo de a
# primeira consulta terminar. 8 s cobrem duas consultas na fila.
_VIDEO_INFO_SLOT_WAIT_SECONDS = 8.0
_video_info_inflight: dict[tuple[str, str], "_InflightInfo"] = {}
_video_info_inflight_lock = threading.Lock()


class _InflightInfo:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict | None = None
        self.error: HTTPException | None = None


@router.get("/stats/public", tags=["stats"])
def public_download_stats(response: Response, db: Session = Depends(get_db)):
    """Métricas agregadas para o site, sem expor dispositivos ou tarefas."""
    response.headers["Cache-Control"] = "public, max-age=5"
    return service.public_download_stats(db)


def _video_info_key(url: str, *, extracted_id: object = None) -> tuple[str, str]:
    identity = media_cache.identify_media(url, extracted_id=extracted_id)
    return identity.platform, identity.media_id


def _cached_video_info(url: str) -> dict | None:
    key = _video_info_key(url)
    now = time.monotonic()
    with _video_info_cache_lock:
        cached = _video_info_cache.get(key)
        if cached is None:
            return None
        expires_at, info = cached
        if now >= expires_at:
            _video_info_cache.pop(key, None)
            return None
        _video_info_cache.move_to_end(key)
        return info


def _store_video_info(url: str, info: dict) -> None:
    ttl = settings.video_info_cache_ttl_seconds
    if ttl <= 0:
        return
    primary_key = _video_info_key(url)
    extracted_key = _video_info_key(url, extracted_id=info.get("id"))
    # O alias pelo ID extraido conecta links curtos/playlist ao link direto.
    # A chave da URL consultada fica por ultimo (mais recente no LRU).
    keys = (
        (extracted_key, primary_key)
        if extracted_key != primary_key
        else (primary_key,)
    )
    expires_at = time.monotonic() + ttl
    with _video_info_cache_lock:
        for key in keys:
            _video_info_cache[key] = (expires_at, info)
            _video_info_cache.move_to_end(key)
        while len(_video_info_cache) > settings.video_info_cache_size:
            _video_info_cache.popitem(last=False)


def _owned_download(db: Session, download_id: uuid.UUID, device_id: uuid.UUID) -> Download | None:
    return db.execute(
        select(Download).where(Download.id == download_id, Download.device_id == device_id)
    ).scalar_one_or_none()


def _video_signature_matches(suffix: str, prefix: bytes) -> bool:
    if suffix in {".mp4", ".mov", ".m4v"}:
        return len(prefix) >= 12 and prefix[4:8] == b"ftyp"
    if suffix in {".webm", ".mkv"}:
        return prefix.startswith(b"\x1aE\xdf\xa3")
    if suffix == ".avi":
        return len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"AVI "
    return False


def _discard_source(download: Download) -> None:
    if not download.source_path:
        return
    source = service.safe_storage_file(download.source_path)
    if source is not None:
        try:
            source.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "conversion_source_cleanup_failed type=%s",
                type(exc).__name__,
            )
    download.source_path = None


def _persist_and_enqueue(
    db: Session,
    download: Download,
    *,
    extracted_media_id: object = None,
) -> tuple[Download, bool]:
    """Persiste na fila do banco e acorda o gerenciador embutido da API."""
    # Serializa deduplicação, limites e INSERT por dispositivo na mesma
    # transação. A restrição parcial no banco fecha a corrida residual.
    db.execute(select(func.pg_advisory_xact_lock(
        func.hashtext(f"download:{download.device_id}"),
    )))
    duplicate = service.active_duplicate(
        db, download.device_id, download.deduplication_key or "",
    )
    if duplicate is not None:
        _discard_source(download)
        db.commit()
        logger.info(
            "task_duplicate_reused task_id=%s operation=%s",
            duplicate.id, duplicate.operation_type,
        )
        return duplicate, False

    try:
        service.enforce_usage_limits(db, download.device_id)
    except Exception:
        db.rollback()
        _discard_source(download)
        raise
    try:
        cache_decision = media_cache.reserve_request(
            db, download, extracted_id=extracted_media_id,
        )
    except media_cache.CacheValidationUnavailable as exc:
        db.rollback()
        _discard_source(download)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "O armazenamento está temporariamente indisponível. Tente novamente.",
            headers={"Retry-After": "10"},
        ) from exc
    db.add(download)
    try:
        if cache_decision.kind == "hit":
            db.flush()
            service.record_download_completion(db, download)
        db.commit()
    except IntegrityError:
        db.rollback()
        _discard_source(download)
        duplicate = service.active_duplicate(
            db, download.device_id, download.deduplication_key or "",
        )
        if duplicate is not None:
            return duplicate, False
        raise
    except Exception:
        db.rollback()
        _discard_source(download)
        raise

    if download.status == DownloadStatus.completed:
        publish_download_event(download, "download.completed")
    elif cache_decision.kind == "wait":
        publish_download_event(download, "download.started")
    else:
        publish_download_event(download, "download.queued")
        notify_task_manager()
        logger.info(
            "task_queued task_id=%s operation=%s",
            download.id, download.operation_type,
        )
    return download, cache_decision.kind in {"owner", "bypass"}


def _delivery_response(path: Path, title: str | None) -> FileResponse | Response:
    filename = service.safe_filename(title, path.suffix)
    if not settings.nginx_internal_downloads_enabled:
        return FileResponse(
            path,
            filename=filename,
            media_type="application/octet-stream",
        )
    root = Path(settings.downloads_dir).resolve(strict=True)
    relative = path.relative_to(root)
    internal_path = "/".join(quote(part, safe="") for part in relative.parts)
    ascii_name = filename.encode("ascii", "ignore").decode() or "download"
    ascii_name = ascii_name.replace('"', "")
    disposition = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )
    return Response(
        media_type="application/octet-stream",
        headers={
            "X-Accel-Redirect": (
                f"{settings.nginx_internal_downloads_uri}/{internal_path}"
            ),
            "Content-Disposition": disposition,
        },
    )

def _download_suffix(download: Download) -> str:
    if download.file_path:
        suffix = Path(download.file_path).suffix
        if suffix:
            return suffix
    output_format = (download.output_format or "bin").lower()
    return f".{output_format}" if output_format.isalnum() else ".bin"


async def _remote_delivery_response(
    download: Download,
    range_header: str | None,
) -> StreamingResponse:
    if range_header and not re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", range_header):
        headers = {}
        if download.remote_size is not None:
            headers["Content-Range"] = f"bytes */{download.remote_size}"
        raise HTTPException(
            status.HTTP_416_RANGE_NOT_SATISFIABLE,
            "Intervalo de download inválido.",
            headers=headers,
        )
    try:
        backend = get_api_storage_backend("google_drive")
        stream = await backend.open_download_stream(
            download.storage_key or "", byte_range=range_header,
        )
    except StorageNotFoundError as exc:
        raise HTTPException(status.HTTP_410_GONE, "Arquivo expirado e removido.") from exc
    except StorageRangeNotSatisfiableError as exc:
        headers = {}
        if download.remote_size is not None:
            headers["Content-Range"] = f"bytes */{download.remote_size}"
        raise HTTPException(
            status.HTTP_416_RANGE_NOT_SATISFIABLE,
            "Intervalo de download não satisfazível.",
            headers=headers,
        ) from exc
    except StorageTemporaryError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Armazenamento temporariamente indisponível.",
            headers={"Retry-After": "5"},
        ) from exc
    except StoragePermanentError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Arquivo temporariamente indisponível.",
        ) from exc

    filename = service.safe_filename(download.title, _download_suffix(download))
    ascii_name = filename.encode("ascii", "ignore").decode() or "download"
    ascii_name = ascii_name.replace('"', "")
    headers = {
        "Content-Disposition": (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(filename, safe='')}"
        ),
        "Accept-Ranges": stream.accept_ranges,
    }
    if stream.content_length is not None:
        headers["Content-Length"] = str(stream.content_length)
    if stream.content_range:
        headers["Content-Range"] = stream.content_range

    started = time.monotonic()
    transferred = 0

    async def body():
        nonlocal transferred
        try:
            with anyio.fail_after(settings.remote_download_timeout_seconds):
                async for chunk in stream.chunks:
                    transferred += len(chunk)
                    yield chunk
        finally:
            await stream.aclose()
            logger.info(
                "storage_download backend=google_drive bytes=%s duration_seconds=%.3f status=%s",
                transferred, time.monotonic() - started, stream.status_code,
            )

    return StreamingResponse(
        body(),
        status_code=stream.status_code,
        media_type=download.remote_mime_type or stream.content_type,
        headers=headers,
    )


@router.post("/video/info")
def video_info(
    data: VideoInfoRequest,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
):
    """Extrai metadados com cache curto, deduplicação e concorrência limitada."""
    url = str(data.url)
    request_id = getattr(request.state, "correlation_id", "-")
    started = time.monotonic()
    memory_before = rss_mb()

    def _log(source: str, result: str) -> None:
        logger.info(
            "video_info request_id=%s source=%s duration_ms=%.0f "
            "memory_before_mb=%.1f memory_after_mb=%.1f status=%s",
            request_id, source, (time.monotonic() - started) * 1000,
            memory_before, rss_mb(), result,
        )

    cached = _cached_video_info(url)
    if cached is not None:
        _log("cache", "success")
        return cached

    info_key = _video_info_key(url)
    with _video_info_inflight_lock:
        entry = _video_info_inflight.get(info_key)
        is_leader = entry is None
        if is_leader:
            entry = _InflightInfo()
            _video_info_inflight[info_key] = entry

    if not is_leader:
        completed = entry.event.wait(timeout=_VIDEO_INFO_WAIT_SECONDS)
        if completed and entry.result is not None:
            _log("dedup", "success")
            return entry.result
        if completed and entry.error is not None:
            _log("dedup", "failed")
            raise entry.error
        _log("dedup", "timeout")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "O servidor está analisando este vídeo. Tente novamente em instantes.",
            headers={"Retry-After": "2"},
        )

    try:
        if not _video_info_slots.acquire(timeout=_VIDEO_INFO_SLOT_WAIT_SECONDS):
            entry.error = HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "O servidor está analisando outros vídeos. Tente novamente em instantes.",
                headers={"Retry-After": "2"},
            )
            _log("extract", "busy")
            raise entry.error
        try:
            info = fetch_info(url)
        except Exception as exc:
            # 422 em vez de 502: a borda da Square substitui respostas 502 pela
            # página HTML dela, escondendo a mensagem do cliente e o erro do log.
            logger.warning(
                "video_info_failed request_id=%s host=%s type=%s",
                request_id,
                urlparse(url).hostname or "unknown",
                type(exc).__name__,
            )
            entry.error = HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Não foi possível obter informações do vídeo.",
            )
            _log("extract", "failed")
            raise entry.error
        finally:
            _video_info_slots.release()
        _store_video_info(url, info)
        entry.result = info
        _log("extract", "success")
        return info
    finally:
        with _video_info_inflight_lock:
            _video_info_inflight.pop(info_key, None)
        entry.event.set()
        # Devolve ao SO as páginas liberadas pelo dicionário do yt-dlp
        # (vários MB por extração); no-op fora do Linux/glibc.
        release_freed_memory()


@router.get("/video/cookies")
def video_cookies(ctx: AuthContext = Depends(get_auth_context)):
    """
    Entrega ao aplicativo os cookies que o servidor usa na extração.

    É a MESMA sessão do servidor: a partir daqui ela existe em cada aparelho e
    pode ser lida de lá. Só é servida com dispositivo autenticado e assinatura
    válida, e apenas para os apps oficiais (Android e Windows) — a busca já
    passa pela API com esses cookies; sem eles aqui, o DOWNLOAD local (que
    nunca passa pelo servidor) caía direto no bot-check do YouTube assim que a
    extração sem cookies parava de bastar. Se o Google invalidar a sessão, o
    servidor cai junto: desligar em COOKIES_SHARING_ENABLED e reexportar o
    cookies.txt.
    """
    settings_atuais = get_settings()
    if not settings_atuais.cookies_sharing_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recurso indisponível.")
    if ctx.device.platform not in {"android", "windows"}:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Disponível apenas nos aplicativos oficiais.",
        )
    caminho = resolve_cookiefile()
    if not caminho:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Cookies não configurados no servidor.",
        )
    try:
        conteudo = Path(caminho).read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("cookies_read_failed device_id=%s", ctx.device.id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Cookies indisponíveis no momento.",
        ) from None
    # Sem o conteúdo no log, em nenhum nível.
    logger.info("cookies_served device_id=%s", ctx.device.id)
    return {"cookies": conteudo}


@router.post("/download/create", status_code=202)
def create_download(
    data: DownloadCreateRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    # Challenge-response: prova de posse da chave privada do dispositivo
    # imediatamente antes da operação sensível (anti-replay forte).
    if not consume_challenge(db, data.challenge, ctx.device.public_key, data.challenge_signature):
        fraud.log_event(db, "challenge_failed", device_id=ctx.device.id, ip=ctx.ip)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Challenge inválido. Solicite um novo.")

    if data.mode == "p":
        output_format = "jpg"
    elif data.mode == "a":
        output_format = data.audio_format
    else:
        output_format = data.video_format

    request_url = str(data.url)
    cached_info = _cached_video_info(request_url)
    download = Download(
        device_id=ctx.device.id,
        url=request_url,
        format_spec=service.build_format_spec(
            data.mode, data.height, data.format_id, output_format,
        ),
        mode=data.mode,
        output_format=output_format,
        operation_type="download",
        stage="queued",
        progress=0.0,
        progress_message="Aguardando na fila.",
    )
    download.deduplication_key = service.build_deduplication_key(
        device_id=download.device_id,
        url=download.url,
        operation_type=download.operation_type,
        mode=download.mode,
        format_spec=download.format_spec,
        output_format=download.output_format,
    )
    queued, created = _persist_and_enqueue(
        db, download,
        extracted_media_id=cached_info.get("id") if cached_info else None,
    )
    return {
        "id": str(queued.id),
        "status": queued.status.value,
        "duplicate": not created,
        "cache": queued.cache_role or "bypass",
    }


def _canonical_local_permit(payload: dict) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except (AttributeError, ValueError):
        return (0,)


@router.post("/local/authorize", status_code=201)
def authorize_local_operation(
    data: LocalOperationAuthorizeRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    """Autoriza trabalho local sem colocar arquivos na fila do servidor."""
    if ctx.device.platform not in {"windows", "android"}:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Esta operação está disponível somente nos aplicativos oficiais.",
        )
    if data.operation == "upscale":
        runtime_settings = get_settings()
        platform_enabled = (
            runtime_settings.upscale_enabled
            if ctx.device.platform == "windows"
            else runtime_settings.android_upscale_enabled
        )
        if (
            ctx.device.status != DeviceStatus.authorized
            or not platform_enabled
        ):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Este dispositivo não possui autorização para usar o Upscale.",
            )
        minimum_version = (
            runtime_settings.upscale_minimum_version
            if ctx.device.platform == "windows"
            else runtime_settings.android_upscale_minimum_version
        )
        if _version_tuple(ctx.device.app_version) < _version_tuple(minimum_version):
            raise HTTPException(
                status.HTTP_426_UPGRADE_REQUIRED,
                "Esta versão do aplicativo não é compatível com o Upscale. "
                "Atualize para continuar.",
            )
        if ctx.device.platform == "android":
            expected_model = (
                runtime_settings.android_animevideov3_model_version
                if data.upscale_options.model == "realesr_animevideov3"
                else runtime_settings.android_realcugan_model_version
            )
            if (
                data.upscale_options.engine_version
                != runtime_settings.android_upscale_engine_version
                or data.upscale_options.model_version != expected_model
            ):
                raise HTTPException(
                    status.HTTP_426_UPGRADE_REQUIRED,
                    "O engine ou os pesos deste APK não são compatíveis. Atualize para continuar.",
                )
    if not consume_challenge(
        db, data.challenge, ctx.device.public_key, data.challenge_signature, False,
    ):
        fraud.log_event(db, "challenge_failed", device_id=ctx.device.id, ip=ctx.ip)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Challenge inválido. Solicite um novo.",
        )

    parameters: dict = {}
    if data.operation == "download":
        parameters = {
            "mode": data.mode,
            "height": data.height,
            "format_id": data.format_id,
            "video_format": data.video_format,
            "audio_format": data.audio_format,
        }
    elif data.operation == "gif":
        parameters = data.gif_options.model_dump(mode="json")
    elif data.operation == "upscale":
        parameters = data.upscale_options.model_dump(mode="json", exclude_none=True)

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=15)
    operation_id = uuid.uuid4()
    nonce = secrets.token_urlsafe(24)
    permit = {
        "version": 1,
        "operation_id": str(operation_id),
        "device_id": ctx.device.device_id,
        "operation": data.operation,
        "url": str(data.url) if data.url is not None else "",
        "parameters": parameters,
        "issued_at": int(now.timestamp()),
        "expires_at": int(expires_at.timestamp()),
        "nonce": nonce,
    }
    record = LocalOperation(
        id=operation_id,
        device_id=ctx.device.id,
        operation=data.operation,
        url=permit["url"] or None,
        parameters=json.dumps(
            parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ),
        permit_nonce_hash=hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
        status="authorized",
        stage="authorized",
        progress=0.0,
        progress_message="Operação autorizada.",
        client_ip=ctx.ip,
        expires_at=expires_at,
    )
    db.add(record)
    db.commit()
    logger.info(
        "local_operation_authorized operation_id=%s type=%s",
        operation_id, data.operation,
    )
    return {"permit": permit, "signature": server_sign(_canonical_local_permit(permit))}


@router.post("/local/{operation_id}/progress")
def update_local_operation(
    operation_id: uuid.UUID,
    data: LocalOperationProgressRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    record = db.execute(
        select(LocalOperation).where(
            LocalOperation.id == operation_id,
            LocalOperation.device_id == ctx.device.id,
        ).with_for_update()
    ).scalar_one_or_none()
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Operação não encontrada.")

    terminal = {"completed", "failed", "cancelled"}
    if record.status in terminal and data.status != record.status:
        raise HTTPException(status.HTTP_409_CONFLICT, "A operação já foi encerrada.")

    now = datetime.now(timezone.utc)
    if record.status == "authorized" and data.status == "running":
        if now > record.expires_at:
            raise HTTPException(status.HTTP_410_GONE, "A autorização expirou.")
        record.started_at = now
    record.status = data.status
    record.stage = data.stage
    record.progress = (
        100.0 if data.status == "completed"
        else max(float(record.progress or 0), data.progress)
    )
    record.progress_message = data.message
    if data.status in terminal:
        record.completed_at = now
    db.commit()
    if data.status in terminal:
        # O motivo vem do cliente e só é registrado em falha: sem ele o log
        # mostrava apenas "status=failed", sem dizer o que quebrou no aparelho.
        reason = (
            " reason=%s" % " ".join((record.progress_message or "-").split())[:160]
            if record.status == "failed" else ""
        )
        logger.info(
            "local_operation_finished operation_id=%s type=%s status=%s%s",
            record.id, record.operation, record.status, reason,
        )
        if record.status == "completed":
            # O envio é enfileirado numa thread própria, mas uma falha ao montar
            # o aviso não pode derrubar o relato de conclusão do aparelho.
            try:
                notify_local_operation_completed(db, record)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "local_operation_webhook_failed operation_id=%s", record.id,
                )
    return {"id": str(record.id), "status": record.status}


@router.post("/conversion/create-upload", status_code=202)
async def create_conversion_upload(
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    x_content_sha256: str = Header(default=""),
    x_file_name_b64: str = Header(default=""),
    x_gif_options_b64: str = Header(default=""),
    x_challenge: str = Header(default=""),
    x_challenge_signature: str = Header(default=""),
):
    """Recebe um video local de ate 80 MB sem bufferizar o corpo na RAM."""
    if not consume_challenge(db, x_challenge, ctx.device.public_key, x_challenge_signature):
        fraud.log_event(db, "challenge_failed", device_id=ctx.device.id, ip=ctx.ip)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Challenge invalido. Solicite um novo.")

    service.enforce_usage_limits(db, ctx.device.id)
    if len(x_file_name_b64) > 1024 or len(x_gif_options_b64) > 8192:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Dados da conversao invalidos.")
    try:
        filename = base64.urlsafe_b64decode(x_file_name_b64.encode()).decode("utf-8")
        options_json = base64.urlsafe_b64decode(x_gif_options_b64.encode()).decode("utf-8")
        options = GifOptions.model_validate_json(options_json)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Dados da conversao invalidos.")

    suffix = Path(filename).suffix.lower()
    if suffix not in {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Formato de video nao suportado.")

    max_bytes = settings.max_gif_upload_mb * 1024 * 1024
    content_length = int(request.headers.get("content-length") or 0)
    if content_length <= 0 or content_length > max_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"O video deve ter no maximo {settings.max_gif_upload_mb} MB.",
        )

    task_id = uuid.uuid4()
    upload_dir = Path(settings.downloads_dir) / str(ctx.device.id) / str(task_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    source_path = upload_dir / f"source-upload{suffix}"
    digest = hashlib.sha256()
    received = 0
    prefix = bytearray()
    try:
        async with await anyio.open_file(source_path, "wb") as output:
            async for chunk in request.stream():
                received += len(chunk)
                if received > max_bytes:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"O video deve ter no maximo {settings.max_gif_upload_mb} MB.",
                    )
                digest.update(chunk)
                if len(prefix) < 16:
                    prefix.extend(chunk[:16 - len(prefix)])
                await output.write(chunk)
        if received != content_length or digest.hexdigest() != x_content_sha256.lower():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "O upload chegou incompleto ou corrompido.")
        if not _video_signature_matches(suffix, bytes(prefix)):
            raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Conteúdo do arquivo não corresponde a um vídeo permitido.")
    except ClientDisconnect:
        # Navegador fechou/perdeu a conexão no meio do envio: limpa e encerra
        # sem poluir o log com stack trace de erro não tratado.
        source_path.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "O envio foi interrompido.")
    except Exception:
        source_path.unlink(missing_ok=True)
        raise

    download = Download(
        id=task_id,
        device_id=ctx.device.id,
        url=f"upload://{Path(filename).name[:180]}",
        source_path=str(source_path),
        format_spec="upload",
        mode="gif",
        output_format="gif",
        conversion_options=options.model_dump_json(),
        operation_type="gif_upload",
        stage="queued",
        progress=0.0,
        progress_message="Aguardando na fila.",
    )
    download.deduplication_key = service.build_deduplication_key(
        device_id=download.device_id,
        url=download.url,
        operation_type=download.operation_type,
        mode=download.mode,
        format_spec=download.format_spec,
        output_format=download.output_format,
        conversion_options=download.conversion_options,
        content_digest=digest.hexdigest(),
    )
    queued, created = _persist_and_enqueue(db, download)
    return {
        "id": str(queued.id),
        "status": queued.status.value,
        "duplicate": not created,
        "cache": queued.cache_role or "bypass",
    }


@router.post("/conversion/create", status_code=202)
def create_conversion(
    data: ConversionCreateRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    """Cria uma conversão de vídeo por link para GIF."""
    if not consume_challenge(db, data.challenge, ctx.device.public_key, data.challenge_signature):
        fraud.log_event(db, "challenge_failed", device_id=ctx.device.id, ip=ctx.ip)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Challenge inválido. Solicite um novo.")

    request_url = str(data.url)
    cached_info = _cached_video_info(request_url)
    download = Download(
        device_id=ctx.device.id,
        url=request_url,
        format_spec="bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        mode="gif",
        output_format="gif",
        conversion_options=data.options.model_dump_json(),
        operation_type="gif_url",
        stage="queued",
        progress=0.0,
        progress_message="Aguardando na fila.",
    )
    download.deduplication_key = service.build_deduplication_key(
        device_id=download.device_id,
        url=download.url,
        operation_type=download.operation_type,
        mode=download.mode,
        format_spec=download.format_spec,
        output_format=download.output_format,
        conversion_options=download.conversion_options,
    )
    queued, created = _persist_and_enqueue(
        db, download,
        extracted_media_id=cached_info.get("id") if cached_info else None,
    )
    return {
        "id": str(queued.id),
        "status": queued.status.value,
        "duplicate": not created,
        "cache": queued.cache_role or "bypass",
    }


@router.post("/conversion/cancel/{download_id}")
def cancel_conversion(
    download_id: uuid.UUID,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    dl = _owned_download(db, download_id, ctx.device.id)
    if not dl or dl.mode != "gif":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversão não encontrada.")
    if dl.status in (
        DownloadStatus.completed,
        DownloadStatus.failed,
        DownloadStatus.cancelled,
    ):
        return {"status": dl.status.value}
    dl.cancel_requested = True
    dl.cancel_requested_at = datetime.now(timezone.utc)
    if dl.status == DownloadStatus.queued or dl.cache_role == media_cache.CACHE_ROLE_WAITER:
        dl.status = DownloadStatus.cancelled
        dl.error = None
        dl.stage = "cancelled"
        dl.progress_message = "Tarefa cancelada."
        dl.completed_at = datetime.now(timezone.utc)
        if dl.source_path:
            source = service.safe_storage_file(dl.source_path)
            if source is not None:
                source.unlink(missing_ok=True)
            dl.source_path = None
        affected = media_cache.fail_owner(db, dl, RuntimeError("cancelled"))
        db.commit()
        for follower in affected:
            publish_download_event(follower, "download.failed")
    else:
        db.commit()
    notify_task_manager()
    publish_download_event(
        dl,
        "download.cancelled" if dl.status == DownloadStatus.cancelled else "download.cancel_requested",
    )
    return {
        "status": "cancelled" if dl.status == DownloadStatus.cancelled else "cancelling",
    }


# GET e POST: alguns edges/CDN (ex.: Cloudflare) desafiam/bloqueiam GETs
# repetidos de polling. O cliente usa POST, que passa; o site segue com GET.
@router.api_route("/download/status/{download_id}", methods=["GET", "POST"])
def download_status(
    download_id: uuid.UUID,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    dl = _owned_download(db, download_id, ctx.device.id)
    if not dl:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Download não encontrado.")

    public_status = dl.status.value
    response: dict = {
        "id": str(dl.id),
        "status": public_status,
        "title": dl.title,
        "file_size": dl.file_size,
        "error": "Não foi possível processar este vídeo." if dl.status == DownloadStatus.failed else None,
        "processing_time": dl.processing_time,
        "progress": dl.progress,
        "stage": dl.stage,
        "progress_message": dl.progress_message,
        "queue_position": service.queue_position(db, dl),
        "revision": dl.revision,
        "updated_at": dl.updated_at.isoformat(),
    }
    if dl.status == DownloadStatus.completed and private_storage.download_has_file(dl):
        token = service.make_delivery_token(
            dl.id, dl.device_id, dl.revision, dl.expires_at,
        )
        response["file_url"] = f"/download/public/{dl.id}?token={token}"
        response["file_name"] = service.safe_filename(dl.title, _download_suffix(dl))
        if dl.mode == "gif" and dl.conversion_options:
            try:
                response["conversion"] = json.loads(dl.conversion_options).get("result", {})
            except (TypeError, ValueError):
                pass
    return response


@router.get("/download/history")
def download_history(
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
):
    limit = min(max(limit, 1), 100)
    rows = (
        db.execute(
            select(Download)
            .where(Download.device_id == ctx.device.id)
            .order_by(Download.created_at.desc())
            .limit(limit)
            .offset(max(offset, 0))
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(d.id),
            "url": d.url,
            "title": d.title,
            "status": d.status.value,
            "mode": d.mode or "va",
            "file_size": d.file_size,
            "created_at": d.created_at,
            "completed_at": d.completed_at,
        }
        for d in rows
    ]


@router.get("/download/file/{download_id}")
async def download_file(
    download_id: uuid.UUID,
    token: str | None = None,
    x_download_token: str | None = Header(default=None),
    range_header: str | None = Header(default=None, alias="Range"),
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    """Entrega o arquivo. Exige sessão assinada; token antigo ainda é aceito."""
    dl = _owned_download(db, download_id, ctx.device.id)
    if not dl or dl.status != DownloadStatus.completed or not private_storage.download_has_file(dl):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Arquivo não disponível.")
    legacy_token = token or x_download_token
    if legacy_token and not service.verify_delivery_token(
        download_id, dl.device_id, dl.revision, legacy_token,
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Link expirado.")

    if dl.storage_backend == "google_drive" and dl.storage_key:
        return await _remote_delivery_response(dl, range_header)

    path = service.safe_storage_file(dl.file_path)
    if path is None:
        raise HTTPException(status.HTTP_410_GONE, "Arquivo expirado e removido.")
    # Nome amigável (título) — o nome interno aleatório nunca é exposto.
    return _delivery_response(path, dl.title)


# GET e POST pelo mesmo motivo do status: o app baixa via POST (passa no
# edge); o site continua usando GET normalmente.
@router.api_route("/download/public/{download_id}", methods=["GET", "POST"])
async def download_public_file(
    download_id: uuid.UUID,
    token: str,
    range_header: str | None = Header(default=None, alias="Range"),
    db: Session = Depends(get_db),
):
    """Entrega temporaria por token HMAC; usado pelo app/site apos o processamento."""
    dl = db.get(Download, download_id)
    if (
        not dl
        or dl.status != DownloadStatus.completed
        or not private_storage.download_has_file(dl)
        or not service.verify_delivery_token(
            download_id, dl.device_id, dl.revision, token,
        )
    ):
        # Não revela se o ID existe, pertence a outro dispositivo ou só expirou.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Arquivo nao disponivel.")

    if dl.storage_backend == "google_drive" and dl.storage_key:
        return await _remote_delivery_response(dl, range_header)

    path = service.safe_storage_file(dl.file_path)
    if path is None:
        raise HTTPException(status.HTTP_410_GONE, "Arquivo expirado e removido.")

    return _delivery_response(path, dl.title)
