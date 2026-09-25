"""
XARD YT SYSTEM — API principal.

Ponto de entrada FastAPI. Sobe atrás do Nginx + Cloudflare; nunca
exposta diretamente à internet.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.observability import setup_logging

# Antes de qualquer router/serviço: todos os logs do processo da API já
# nascem no formato estruturado (data | nível | serviço | logger | mensagem).
setup_logging(os.environ.get("XARD_SERVICE", "api"))

from backend.admin.router import router as admin_router
from backend.announcements.router import active_announcement, announcement_payload
from backend.api.config import get_settings
from backend.api.deps import client_ip, require_admin
from backend.auth.router import router as session_router
from backend.database.models import Download, DownloadStatus, SecurityLog
from backend.database.session import Base, engine, get_db
from backend.database.session import SessionLocal
from backend.notifications.releases import publish_release
from backend.downloads.router import router as downloads_router
from backend.notifications.android_events import router as android_events_router
from backend.tasks.router import router as tasks_router
from backend.security.router import router as security_router
from backend.security.integrity import shutdown_integrity_http
from backend.security.signing import ensure_server_keys, server_public_key_pem, server_sign
from backend.realtime import realtime_metrics, realtime_router, start_realtime, stop_realtime
from backend.tasks.manager import (
    start_task_manager,
    stop_task_manager,
    task_manager_metrics,
)
from backend.notifications.discord import shutdown_discord_notifications
from backend.storage.manager import get_api_storage_backend, shutdown_storage_backend

settings = get_settings()
logger = logging.getLogger(__name__)
_android_publication: dict[str, str] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_server_keys()
    # Valida configuração/credenciais sem imprimir valores nem chamar o Drive.
    if settings.storage_backend == "google_drive":
        get_api_storage_backend("google_drive")
    # Em produção, prefira migrações Alembic; create_all cobre o bootstrap.
    Base.metadata.create_all(bind=engine)
    # O enum precisa ser ampliado e confirmado antes de qualquer UPDATE usar o
    # novo valor em instalações existentes.
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TYPE downloadstatus ADD VALUE IF NOT EXISTS 'cancelled'"
        ))
    # Migração aditiva e idempotente para instalações existentes.
    with engine.begin() as connection:
        for statement in (
            "ALTER TABLE media_cache ADD COLUMN IF NOT EXISTS bytes_downloaded_external BIGINT NOT NULL DEFAULT 0",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS conversion_options TEXT",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS source_path TEXT",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS progress DOUBLE PRECISION",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS stage VARCHAR(24)",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS worker_id VARCHAR(255)",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS queued_at TIMESTAMPTZ DEFAULT NOW()",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS operation_type VARCHAR(32) NOT NULL DEFAULT 'download'",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS deduplication_key VARCHAR(64)",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS media_cache_id UUID REFERENCES media_cache(id) ON DELETE SET NULL",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS cache_role VARCHAR(16)",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS progress_message VARCHAR(255)",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS interrupted_at TIMESTAMPTZ",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS statistics_counted_at TIMESTAMPTZ",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS storage_backend VARCHAR(24) NOT NULL DEFAULT 'local'",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS storage_state VARCHAR(32) NOT NULL DEFAULT 'local_ready'",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS storage_key TEXT",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS storage_reserved_key TEXT",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS storage_idempotency_key VARCHAR(200)",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS remote_parent_id TEXT",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS remote_size BIGINT",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS remote_checksum VARCHAR(128)",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS remote_mime_type VARCHAR(255)",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS remote_uploaded_at TIMESTAMPTZ",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS local_delete_pending BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS file_deleted_at TIMESTAMPTZ",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS storage_error TEXT",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS source_storage_backend VARCHAR(24)",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS source_storage_key TEXT",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS source_storage_reserved_key TEXT",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS source_storage_idempotency_key VARCHAR(200)",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS source_remote_parent_id TEXT",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS source_remote_size BIGINT",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS source_remote_checksum VARCHAR(128)",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS source_remote_mime_type VARCHAR(255)",
            "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS source_remote_uploaded_at TIMESTAMPTZ",
            "CREATE INDEX IF NOT EXISTS ix_downloads_heartbeat_at ON downloads (heartbeat_at)",
            "CREATE INDEX IF NOT EXISTS ix_downloads_storage_pending ON downloads (storage_state, updated_at)",
            "CREATE INDEX IF NOT EXISTS ix_downloads_storage_expires ON downloads (expires_at, storage_state)",
            "CREATE INDEX IF NOT EXISTS ix_downloads_operation_status ON downloads (operation_type, status)",
            "CREATE INDEX IF NOT EXISTS ix_downloads_media_cache ON downloads (media_cache_id, cache_role)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_downloads_active_deduplication ON downloads (device_id, deduplication_key) WHERE deduplication_key IS NOT NULL AND status IN ('queued', 'processing')",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_downloads_storage_idempotency ON downloads (storage_idempotency_key) WHERE storage_idempotency_key IS NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_downloads_source_storage_idempotency ON downloads (source_storage_idempotency_key) WHERE source_storage_idempotency_key IS NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_downloads_storage_reserved_key ON downloads (storage_reserved_key) WHERE storage_reserved_key IS NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_downloads_source_storage_reserved_key ON downloads (source_storage_reserved_key) WHERE source_storage_reserved_key IS NOT NULL",
        ):
            connection.execute(text(statement))
        connection.execute(text(
            "UPDATE downloads SET operation_type = CASE "
            "WHEN mode = 'gif' AND source_path IS NOT NULL THEN 'gif_upload' "
            "WHEN mode = 'gif' THEN 'gif_url' ELSE 'download' END "
            "WHERE operation_type = 'download'"
        ))
        connection.execute(text(
            "UPDATE downloads SET status = 'cancelled', stage = 'cancelled', "
            "error = NULL WHERE status = 'failed' AND error = '__cancelled__'"
        ))
        connection.execute(text(
            "WITH counted AS ("
            "UPDATE downloads AS download SET statistics_counted_at = "
            "COALESCE(download.completed_at, download.updated_at, NOW()) "
            "FROM devices AS device WHERE download.device_id = device.id "
            "AND download.status = 'completed' "
            "AND download.statistics_counted_at IS NULL AND device.platform = 'web' "
            "RETURNING download.id"
            "), delta AS (SELECT COUNT(*)::BIGINT AS value FROM counted) "
            "INSERT INTO system_counters (key, value, updated_at) "
            "SELECT 'site_downloads_completed', value, NOW() FROM delta "
            "ON CONFLICT (key) DO UPDATE SET "
            "value = system_counters.value + EXCLUDED.value, updated_at = NOW()"
        ))
        connection.execute(text(
            "UPDATE downloads SET statistics_counted_at = "
            "COALESCE(completed_at, updated_at, NOW()) "
            "WHERE status = 'completed' AND statistics_counted_at IS NULL"
        ))
    await start_realtime()
    await start_task_manager()
    try:
        with SessionLocal() as db:
            release, created = publish_release(
                db, release_id=settings.android_release_id or f"android-{settings.android_latest_version_code}",
                version=settings.android_latest_version, version_code=settings.android_latest_version_code,
                artifacts={"sha256": settings.android_download_sha256, "downloads": _android_downloads()},
            )
            _android_publication.update(release_id=release.release_id,
                published_at=release.published_at.isoformat())
            logger.info("android_release_publication version_code=%s new=%s", release.version_code, created)
        yield
    finally:
        await stop_task_manager()
        await stop_realtime()
        await shutdown_integrity_http()
        await shutdown_storage_backend()
        shutdown_discord_notifications()
        engine.dispose()


app = FastAPI(
    title="XARD YT SYSTEM API",
    version=settings.app_latest_version,
    lifespan=lifespan,
    # Documentação interativa desligada em produção: não facilitar mapeamento
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.environment != "production" else None,
)

# CORS: apenas o site oficial. Apps nativos não usam CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.web_origin],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Device-ID",
        "X-App-Version",
        "X-Timestamp",
        "X-Request-ID",
        "X-Signature",
        "X-Content-SHA256",
        "X-File-Name-B64",
        "X-Gif-Options-B64",
        "X-Challenge",
        "X-Challenge-Signature",
        "X-Admin-Key",
        "Range",
    ],
    expose_headers=[
        "Accept-Ranges",
        "Content-Disposition",
        "Content-Length",
        "Content-Range",
    ],
    max_age=600,
)


@app.middleware("http")
async def sensitive_response_headers(request: Request, call_next):
    correlation_id = str(uuid.uuid4())
    request.state.correlation_id = correlation_id
    try:
        declared_length = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared_length = -1
    limit = settings.max_gif_upload_mb * 1024 * 1024 if request.url.path == "/conversion/create-upload" else 64 * 1024
    if declared_length < 0 or declared_length > limit:
        return JSONResponse(status_code=413, content={"detail": "Requisição muito grande."})
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith(("/session", "/security", "/download", "/conversion", "/video", "/local", "/app", "/admin", "/websocket")):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response

app.include_router(session_router)
app.include_router(security_router)
app.include_router(downloads_router)
app.include_router(android_events_router)
app.include_router(tasks_router)
app.include_router(admin_router)
app.include_router(realtime_router)


@app.get("/app/version", tags=["app"])
def app_version():
    """
    Manifesto de versão, ASSINADO com a chave privada do servidor.
    O app verifica a assinatura com a chave pública embutida — impede
    que um MITM/proxy force downgrade ou aponte para um instalador falso.
    """
    manifest = {
        "version": settings.app_latest_version,
        "minimum_version": settings.app_minimum_version,
        "required": settings.app_minimum_version == settings.app_latest_version,
        "download_url": settings.app_download_url,
        "sha256": settings.app_download_sha256.lower(),
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return {**manifest, "signature": server_sign(canonical.encode())}


@app.get("/app/installer", tags=["app"])
def app_installer(request: Request, db: Session = Depends(get_db)):
    """
    Instalador oficial do desktop, servido para o auto-update.
    Integridade garantida pelo cliente: SHA-256 publicado no manifesto
    assinado de /app/version. Rate limit por IP contra abuso de banda.
    """
    ip = client_ip(request)
    hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    count = db.execute(
        select(func.count(SecurityLog.id)).where(
            SecurityLog.ip == ip,
            SecurityLog.event == "installer_download",
            SecurityLog.created_at >= hour_ago,
        )
    ).scalar_one()
    if int(count) > 10:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Muitos downloads deste IP. Tente novamente mais tarde.")

    path = Path(settings.app_installer_file)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Instalador indisponível.")
    db.add(SecurityLog(ip=ip, event="installer_download"))
    db.commit()
    return FileResponse(
        path,
        filename=f"SLUCSS-YT-SYSTEM-Setup-{settings.app_latest_version}.exe",
        media_type="application/octet-stream",
    )


def _android_changelog() -> list[str]:
    """
    Aceita JSON (`["a","b"]`) ou lista separada por `|`.

    O painel da hospedagem não preserva valores com colchetes, aspas e vírgulas:
    a variável chegava vazia em produção e o app caía no texto genérico. O
    formato com `|` sobrevive ao painel e ainda é fácil de editar à mão.
    """
    value = (settings.android_changelog_json or "").strip()
    if not value:
        return []
    if value[0] in "[{":
        # JSON malformado não vira texto avulso: exibir `["Um","Do` ao usuário
        # é pior do que deixar o aplicativo usar a mensagem genérica dele.
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(parsed, list):
            return []
        items = [item for item in parsed if isinstance(item, str)]
    else:
        items = value.replace("\n", "|").split("|")
    return [text.strip()[:160] for text in items if text.strip()][:20]


def _android_downloads() -> dict[str, dict[str, str]]:
    """
    Fatias por arquitetura, indexadas pelo nome da ABI do Android.

    Só entram os pares completos: URL sem hash (ou o contrário) publicaria uma
    atualização que o aplicativo recusaria na verificação de integridade. O que
    ficar de fora simplesmente cai no `download_url` universal.
    """
    pares = {
        "arm64-v8a": (settings.android_download_url_arm64, settings.android_download_sha256_arm64),
        "armeabi-v7a": (settings.android_download_url_arm32, settings.android_download_sha256_arm32),
    }
    return {
        abi: {"url": url.strip(), "sha256": sha.strip().lower()}
        for abi, (url, sha) in pares.items()
        if url.strip() and sha.strip()
    }


@app.get("/app/android/version", tags=["app"])
def android_app_version():
    """Manifesto Android assinado, independente do ciclo do desktop."""
    manifest = {
        "latest_version": settings.android_latest_version,
        "release_id": _android_publication.get("release_id", settings.android_release_id or f"android-{settings.android_latest_version_code}"),
        "published_at": _android_publication.get("published_at", ""),
        "update_policy": settings.android_update_policy,
        "latest_version_code": settings.android_latest_version_code,
        "minimum_version_code": settings.android_minimum_version_code,
        "download_url": settings.android_download_url,
        "sha256": settings.android_download_sha256.lower(),
        # Clientes antigos ignoram este campo; como a assinatura é calculada
        # sobre o JSON inteiro que eles recebem, continuam validando normal.
        "downloads": _android_downloads(),
        "changelog": _android_changelog(),
        "processing_mode": settings.android_processing_mode,
        "server_fallback_enabled": settings.android_server_fallback_enabled,
        "upscale": {
            "enabled": settings.android_upscale_enabled,
            "minimum_version_code": settings.android_upscale_minimum_version_code,
            "engine_version": settings.android_upscale_engine_version,
            "models": {
                "realesr_animevideov3": settings.android_animevideov3_model_version,
                "realcugan": settings.android_realcugan_model_version,
            },
        },
    }
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {**manifest, "signature": server_sign(canonical.encode("utf-8"))}


@app.get("/app/android/files/{filename}", tags=["app"])
def android_release_file(filename: str):
    # Only immutable, packaged APKs, never user-provided filesystem paths.
    if filename not in {"Slucss-System.apk", "Slucss-System-arm64-v8a.apk", "Slucss-System-armeabi-v7a.apk"}:
        raise HTTPException(status_code=404, detail="APK não encontrado")
    path = Path(__file__).resolve().parents[1] / "releases" / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="APK não publicado")
    return FileResponse(path, filename=filename, media_type="application/vnd.android.package-archive",
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


def _register_android_download(request: Request, db: Session, event: str) -> None:
    """Aplica o limite público e registra a entrega de um artefato Android."""
    ip = client_ip(request)
    hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    count = db.execute(
        select(func.count(SecurityLog.id)).where(
            SecurityLog.ip == ip,
            SecurityLog.event == event,
            SecurityLog.created_at >= hour_ago,
        )
    ).scalar_one()
    if int(count) > 10:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Muitos downloads deste IP. Tente novamente mais tarde.",
        )
    db.add(SecurityLog(ip=ip, event=event))
    db.commit()


@app.get("/app/android/apk", tags=["app"])
def android_app_apk(
    request: Request,
    db: Session = Depends(get_db),
    abi: Literal["arm64", "arm32"] | None = None,
):
    """Redireciona ao APK da ABI pedida ou ao universal como fallback."""
    _register_android_download(request, db, "android_apk_download")

    external_url = ""
    if abi:
        android_abi = {
            "arm64": "arm64-v8a",
            "arm32": "armeabi-v7a",
        }[abi]
        selected_download = _android_downloads().get(android_abi)
        if selected_download:
            external_url = selected_download["url"]
    external_url = external_url or settings.android_download_url.strip()
    if external_url:
        return RedirectResponse(external_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    # Fallback útil apenas em desenvolvimento. O deploy de produção hospeda o
    # APK fora da API para não consumir disco, RAM ou banda do backend.
    path = Path(settings.android_apk_file)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Aplicativo Android indisponível.")
    return FileResponse(
        path,
        filename="Slucss-System.apk",
        media_type="application/vnd.android.package-archive",
    )


@app.get("/app/android/installer", tags=["app"])
def android_app_installer(request: Request, db: Session = Depends(get_db)):
    """Compatibilidade: encaminha links antigos ao APK completo oficial."""
    _register_android_download(request, db, "android_installer_download")
    # O bootstrap permanece apenas como candidato de homologação. Servi-lo ao
    # público aumenta o risco de falso positivo como downloader hostil no Play
    # Protect. O caminho legado continua funcionando, sem entregar esse binário.
    return RedirectResponse("/app/android/apk", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@app.get("/app/announcement", tags=["app"])
def app_announcement(platform: str = "all", db: Session = Depends(get_db)):
    """Mensagem/anuncio publico configurado no painel admin."""
    return announcement_payload(active_announcement(db, platform))


@app.get("/app/status", tags=["app"])
def app_status(db: Session = Depends(get_db)):
    """Status operacional público (sem detalhes internos)."""
    hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    processing = db.execute(
        select(func.count(Download.id)).where(Download.status == DownloadStatus.processing)
    ).scalar_one()
    completed_1h = db.execute(
        select(func.count(Download.id)).where(
            Download.status == DownloadStatus.completed, Download.completed_at >= hour_ago
        )
    ).scalar_one()
    return {"operational": True, "processing": processing, "completed_last_hour": completed_1h}


@app.get("/app/public-key", tags=["app"])
def app_public_key():
    """Chave pública do servidor (é pública por definição)."""
    return {"public_key": server_public_key_pem()}


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False, dependencies=[Depends(require_admin)])
def metrics(db: Session = Depends(get_db)):
    """Métricas internas — exige X-Admin-Key; nunca públicas."""
    day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    by_status = db.execute(
        select(Download.status, func.count(Download.id))
        .where(Download.created_at >= day_ago)
        .group_by(Download.status)
    ).all()
    avg_time = db.execute(
        select(func.avg(Download.processing_time)).where(
            Download.completed_at >= day_ago, Download.processing_time.isnot(None)
        )
    ).scalar_one()
    return {
        "downloads_24h_by_status": {s.value: c for s, c in by_status},
        "avg_processing_time_24h": round(avg_time, 2) if avg_time else None,
        "websocket": realtime_metrics(),
        "task_manager": task_manager_metrics(),
    }


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Nunca vazar stack trace / detalhes internos para o cliente.
    correlation_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    logger.exception("unhandled_request_error correlation_id=%s path=%s", correlation_id, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Erro interno.", "correlation_id": correlation_id},
        headers={"Cache-Control": "no-store", "X-Correlation-ID": correlation_id},
    )
