"""Notificacao de downloads concluidos no Discord."""

from __future__ import annotations

import json
import hashlib
import hmac
import ipaddress
import logging
import queue
import re
import threading
import time
import urllib.request
from collections import deque
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.api.config import get_settings
from backend.database.models import ApiRequest, Device, Download, LocalOperation

_MODE_LABELS = {
    "va": "Video + Audio",
    "v": "Apenas Video",
    "a": "Apenas Audio",
}

_OPERATION_LABELS = {
    "download": "Download",
    "gif": "GIF",
    "compatibility": "Conversao para MP4",
    "upscale": "Upscale com IA",
}

_PLATFORM_LABELS = {
    "windows": "EXE (Windows)",
    "android": "APK (Android)",
}

_SEVERITY = {"low": 10, "medium": 20, "high": 30, "critical": 40}
_QUEUE: queue.Queue[tuple[str, dict[str, Any], float] | None] = queue.Queue(maxsize=128)
_THREAD: threading.Thread | None = None
_THREAD_LOCK = threading.Lock()
_RATE_LOCK = threading.Lock()
_SENT_AT: deque[float] = deque()
_SUPPRESSED = 0
log = logging.getLogger(__name__)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _valid_webhook_url(url: str) -> bool:
    parsed = urlsplit(url)
    return bool(
        parsed.scheme == "https"
        and parsed.hostname in {"discord.com", "discordapp.com"}
        and parsed.path.startswith("/api/webhooks/")
        and not parsed.username
        and not parsed.password
        and parsed.port in (None, 443)
    )


def _mask_ip(value: str | None) -> str:
    try:
        address = ipaddress.ip_address(value or "")
    except ValueError:
        return "unknown"
    if address.version == 4:
        parts = str(address).split(".")
        return ".".join([*parts[:3], "xxx"])
    network = ipaddress.ip_network(f"{address}/64", strict=False)
    return f"{network.network_address.compressed}/64"


def _ip_reference(value: str | None) -> str | None:
    key = get_settings().security_log_hmac_key
    if not key or not value:
        return None
    digest = hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()[:12]
    return f"ip_{digest}"


def _safe_text(value: Any, limit: int = 500) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"(?i)bearer\s+[a-z0-9._~-]+", "Bearer [redacted]", text)
    text = re.sub(r"https://(?:discord(?:app)?\.com)/api/webhooks/\S+", "[webhook redacted]", text)
    return text[:limit]


def _send_worker() -> None:
    opener = urllib.request.build_opener(_NoRedirect)
    while True:
        item = _QUEUE.get()
        try:
            if item is None:
                return
            url, payload, timeout = item
            request = urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "XARD-Security/1.0"},
                method="POST",
            )
            with opener.open(request, timeout=timeout) as response:
                response.read(1024)
        except Exception:  # noqa: BLE001 - observabilidade nunca quebra a operação
            log.warning("security_webhook_delivery_failed")
        finally:
            _QUEUE.task_done()


def _enqueue(url: str, payload: dict[str, Any], timeout: float) -> bool:
    global _THREAD
    if not _valid_webhook_url(url):
        return False
    with _THREAD_LOCK:
        if _THREAD is None or not _THREAD.is_alive():
            _THREAD = threading.Thread(target=_send_worker, name="xard-security-webhook", daemon=True)
            _THREAD.start()
    try:
        _QUEUE.put_nowait((url, payload, timeout))
        return True
    except queue.Full:
        log.warning("security_webhook_queue_full")
        return False


def shutdown_discord_notifications(timeout: float = 2.0) -> None:
    global _THREAD
    with _THREAD_LOCK:
        thread = _THREAD
        if thread is None:
            return
        try:
            _QUEUE.put_nowait(None)
        except queue.Full:
            return
    thread.join(timeout)
    if not thread.is_alive():
        _THREAD = None


def report_security_event(
    *,
    event_type: str,
    severity: str,
    ip: str | None = None,
    request_id: str | None = None,
    route: str | None = None,
    device_id: Any = None,
    details: Any = None,
) -> bool:
    """Enfileira alerta sanitizado, limitado e não bloqueante."""
    global _SUPPRESSED
    settings = get_settings()
    level = _SEVERITY.get(severity.lower(), 0)
    minimum = _SEVERITY.get(settings.discord_security_webhook_min_severity.lower(), 20)
    url = settings.discord_security_webhook_url.strip()
    if not settings.discord_security_webhook_enabled or level < minimum or not url:
        return False
    now = time.monotonic()
    with _RATE_LOCK:
        while _SENT_AT and now - _SENT_AT[0] >= 60:
            _SENT_AT.popleft()
        if len(_SENT_AT) >= max(1, settings.discord_security_webhook_rate_limit_per_minute):
            _SUPPRESSED += 1
            return False
        suppressed, _SUPPRESSED = _SUPPRESSED, 0
        _SENT_AT.append(now)
    shown_ip = ip if settings.discord_security_webhook_include_full_ip and level >= 30 else _mask_ip(ip)
    fields = [
        {"name": "Evento", "value": _safe_text(event_type, 120), "inline": False},
        {"name": "Severidade", "value": severity.upper(), "inline": True},
        {"name": "Ambiente", "value": _safe_text(settings.discord_security_webhook_environment, 40), "inline": True},
        {"name": "IP", "value": shown_ip, "inline": True},
    ]
    for name, value in (
        ("Referência IP", _ip_reference(ip)), ("Request ID", request_id),
        ("Rota", route), ("Dispositivo", str(device_id)[:12] if device_id else None),
        ("Detalhes", details), ("Suprimidos", suppressed or None),
    ):
        if value is not None:
            fields.append({"name": name, "value": _safe_text(value), "inline": False})
    return _enqueue(
        url,
        {"username": "XARD Security", "embeds": [{"title": "Alerta de segurança", "color": 15158332, "fields": fields[:12]}]},
        max(1.0, min(float(settings.discord_security_webhook_timeout_seconds), 10.0)),
    )


def _quality_label(download: Download) -> str:
    spec = download.format_spec or ""
    height = re.search(r"height<=([0-9]+)", spec)
    if height:
        return f"{height.group(1)}p"
    if download.mode == "a":
        return download.output_format.upper() if download.output_format else "Audio"
    return "Melhor qualidade"


def _latest_create_request(db: Session, download: Download) -> ApiRequest | None:
    stmt = (
        select(ApiRequest)
        .where(ApiRequest.device_id == download.device_id)
        .where(ApiRequest.endpoint == "/download/create")
        .where(ApiRequest.timestamp >= download.created_at)
        .order_by(ApiRequest.timestamp.asc())
        .limit(1)
    )
    request = db.execute(stmt).scalars().first()
    if request:
        return request
    stmt = (
        select(ApiRequest)
        .where(ApiRequest.device_id == download.device_id)
        .where(ApiRequest.endpoint == "/download/create")
        .order_by(ApiRequest.timestamp.desc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


def notify_download_completed(db: Session, download: Download) -> None:
    webhook_url = get_settings().discord_download_webhook_url.strip()
    if not webhook_url:
        return

    device = db.get(Device, download.device_id)
    request_log = _latest_create_request(db, download)
    source = (device.platform if device else None) or "unknown"
    ip = (request_log.ip if request_log else None) or "unknown"
    mode = _MODE_LABELS.get(download.mode or "va", download.mode or "unknown")
    quality = _quality_label(download)

    payload = {
        "username": "XARD Downloads",
        "embeds": [{
            "title": "Download concluido",
            "color": 15010068,
            "fields": [
                {"name": "Titulo", "value": (download.title or "Sem titulo")[:1024], "inline": False},
                {"name": "Qualidade", "value": quality[:1024], "inline": True},
                {"name": "Tipo", "value": mode[:1024], "inline": True},
                {"name": "Origem", "value": source[:1024], "inline": True},
                {"name": "IP", "value": _mask_ip(ip), "inline": True},
            ],
        }],
    }
    _enqueue(webhook_url, payload, 5.0)


def notify_app_updated(device: Device, previous_version: str) -> None:
    """Avisa quando um dispositivo conhecido abre uma versão mais nova do app."""
    webhook_url = get_settings().discord_download_webhook_url.strip()
    if not webhook_url:
        return

    platform = _PLATFORM_LABELS.get(device.platform, device.platform or "Desconhecida")
    _enqueue(
        webhook_url,
        {
            "username": "XARD Atualizacoes",
            "embeds": [{
                "title": "1 usuario atualizou",
                "color": 5763719,
                "fields": [
                    {"name": "Aplicativo", "value": _safe_text(platform, 40), "inline": True},
                    {
                        "name": "Versao anterior",
                        "value": _safe_text(previous_version, 16),
                        "inline": True,
                    },
                    {
                        "name": "Nova versao",
                        "value": _safe_text(device.app_version, 16),
                        "inline": True,
                    },
                ],
            }],
        },
        5.0,
    )


def _local_quality_label(parameters: dict[str, Any]) -> str:
    height = parameters.get("height")
    if height:
        return f"{height}p"
    if parameters.get("mode") == "a":
        return str(parameters.get("audio_format") or "Audio").upper()
    return "Melhor qualidade"


def notify_local_operation_completed(db: Session, operation: LocalOperation) -> None:
    """
    Aviso das tarefas processadas no proprio aparelho.

    O caminho do Celery tem o aviso dele em notify_download_completed. Desde que
    o desktop e o Android passaram a processar localmente, nada mais criava
    linha em `downloads` e o webhook deixou de receber qualquer coisa.
    """
    webhook_url = get_settings().discord_download_webhook_url.strip()
    if not webhook_url:
        return

    try:
        parameters = json.loads(operation.parameters or "{}")
    except (json.JSONDecodeError, TypeError):
        parameters = {}
    if not isinstance(parameters, dict):
        parameters = {}

    device = db.get(Device, operation.device_id)
    source = (device.platform if device else None) or "unknown"
    fields: list[dict[str, Any]] = [
        {
            "name": "Tarefa",
            "value": _safe_text(_OPERATION_LABELS.get(operation.operation, operation.operation), 60),
            "inline": True,
        },
        {"name": "Origem", "value": _safe_text(source, 40), "inline": True},
        {"name": "IP", "value": _mask_ip(operation.client_ip), "inline": True},
    ]
    if operation.operation == "download":
        fields.insert(1, {
            "name": "Qualidade", "value": _local_quality_label(parameters), "inline": True,
        })
        fields.insert(2, {
            "name": "Tipo",
            "value": _MODE_LABELS.get(str(parameters.get("mode")), "Desconhecido"),
            "inline": True,
        })
    elif operation.operation == "upscale":
        fields.insert(1, {
            "name": "Modelo",
            "value": _safe_text(parameters.get("model"), 40),
            "inline": True,
        })
        fields.insert(2, {
            "name": "Escala",
            "value": f"{int(parameters.get('scale') or 0)}x",
            "inline": True,
        })
        fields.insert(3, {
            "name": "Saida",
            "value": _safe_text(parameters.get("output_format"), 20).upper(),
            "inline": True,
        })
    if operation.url:
        fields.append({"name": "Link", "value": _safe_text(operation.url, 300), "inline": False})

    _enqueue(
        webhook_url,
        {
            "username": "XARD Downloads",
            "embeds": [{
                "title": "Tarefa concluida no aparelho",
                "color": 15010068,
                "fields": fields,
            }],
        },
        5.0,
    )
