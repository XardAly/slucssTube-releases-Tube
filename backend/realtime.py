"""Eventos WebSocket transitórios; o estado oficial permanece no PostgreSQL."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.api.config import get_settings
from backend.api.deps import AuthContext, client_ip, get_auth_context
from backend.auth.tokens import create_websocket_token, decode_websocket_token
from backend.database.models import Device, DeviceStatus, Download, DownloadStatus, SecurityNonce
from backend.database.session import SessionLocal, get_db
from backend.security import fraud

logger = logging.getLogger(__name__)
settings = get_settings()
realtime_router = APIRouter(tags=["websocket"])

_event_loop: asyncio.AbstractEventLoop | None = None
_poll_task: asyncio.Task | None = None
_poll_stop: asyncio.Event | None = None
_counters: Counter[str] = Counter()
_counters_lock = threading.Lock()


def _count(name: str) -> None:
    with _counters_lock:
        _counters[name] += 1


def _counter_snapshot() -> dict[str, int]:
    with _counters_lock:
        return dict(_counters)


def _public_status(download: Download) -> str:
    if download.status == DownloadStatus.cancelled:
        return "cancelled"
    if download.status == DownloadStatus.failed and download.error == "__cancelled__":
        return "cancelled"
    return download.status.value


def infer_event_type(download: Download) -> str:
    public_status = _public_status(download)
    if public_status in {"completed", "failed", "cancelled"}:
        return f"download.{public_status}"
    if download.cancel_requested:
        return "download.cancel_requested"
    if public_status == "queued":
        return "download.queued"
    return "download.progress"


def build_download_event(download: Download, event_type: str | None = None) -> dict[str, Any]:
    event = {
        "version": 1,
        "type": event_type or infer_event_type(download),
        "download_id": str(download.id),
        "status": _public_status(download),
        "progress": download.progress,
        "progress_message": download.progress_message,
        "stage": download.stage,
        "revision": int(download.revision or 1),
        "updated_at": (download.updated_at or datetime.now(timezone.utc)).isoformat(),
    }
    if event["status"] == "failed":
        event.update({
            "error_code": "PROCESSING_FAILED",
            "message": "Não foi possível processar este vídeo.",
        })
    return event


def _dispatch_local(device_id: str, event: dict[str, Any]) -> bool:
    if not settings.websocket_enabled:
        return False
    loop = _event_loop
    safe_device_id = _safe_uuid(device_id)
    if loop is None or loop.is_closed() or safe_device_id is None:
        return False
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    try:
        if running is loop:
            loop.create_task(hub.dispatch(safe_device_id, event))
        else:
            asyncio.run_coroutine_threadsafe(
                hub.dispatch(safe_device_id, event), loop,
            )
        _count("events_dispatched")
        return True
    except RuntimeError:
        _count("dispatch_failures")
        return False


def publish_download_event(download: Download, event_type: str | None = None) -> bool:
    """Publica somente depois do commit; indisponibilidade nunca falha a tarefa."""
    event = build_download_event(download, event_type)
    published = _dispatch_local(str(download.device_id), event)
    if published:
        logger.debug(
            "websocket_event_dispatched type=%s download_id=%s revision=%s",
            event["type"], download.id, download.revision,
        )
    return published


@dataclass(eq=False)
class _Client:
    websocket: WebSocket
    device_id: uuid.UUID
    ip: str
    expires_at: float
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(settings.websocket_client_queue_size))
    subscriptions: set[uuid.UUID] = field(default_factory=set)
    last_pong: float = field(default_factory=time.monotonic)
    message_times: deque[float] = field(default_factory=deque)


class _Hub:
    def __init__(self) -> None:
        self._by_device: dict[uuid.UUID, set[_Client]] = defaultdict(set)
        self._by_ip: Counter[str] = Counter()
        self._lock = asyncio.Lock()
        self._state_lock = threading.Lock()

    async def add(self, client: _Client) -> bool:
        async with self._lock:
            with self._state_lock:
                existing = self._by_device.get(client.device_id)
                if len(existing or ()) >= settings.websocket_max_connections_per_device:
                    return False
                if self._by_ip[client.ip] >= settings.websocket_max_connections_per_ip:
                    return False
                self._by_device.setdefault(client.device_id, set()).add(client)
                self._by_ip[client.ip] += 1
            _count("connections_total")
            return True

    async def remove(self, client: _Client) -> None:
        async with self._lock:
            with self._state_lock:
                clients = self._by_device.get(client.device_id)
                if clients and client in clients:
                    clients.remove(client)
                    if not clients:
                        self._by_device.pop(client.device_id, None)
                    self._by_ip[client.ip] -= 1
                    if self._by_ip[client.ip] <= 0:
                        self._by_ip.pop(client.ip, None)

    async def dispatch(self, device_id: uuid.UUID, event: dict[str, Any]) -> None:
        async with self._lock:
            with self._state_lock:
                clients = tuple(self._by_device.get(device_id, ()))
        if not clients:
            _count("events_discarded")
            return
        download_id = event.get("download_id")
        for client in clients:
            if client.subscriptions and _safe_uuid(download_id) not in client.subscriptions:
                continue
            try:
                client.queue.put_nowait(event)
            except asyncio.QueueFull:
                _count("slow_clients")
                await client.websocket.close(code=1013, reason="Cliente lento; sincronize pela API.")

    async def targets(self) -> dict[uuid.UUID, set[uuid.UUID] | None]:
        """Snapshot das tarefas relevantes sem reter conexões fora do lock."""
        async with self._lock:
            with self._state_lock:
                result: dict[uuid.UUID, set[uuid.UUID] | None] = {}
                for device_id, clients in self._by_device.items():
                    if any(not client.subscriptions for client in clients):
                        result[device_id] = None
                    else:
                        result[device_id] = set().union(*(
                            client.subscriptions for client in clients
                        ))
                return result

    def metrics(self) -> dict[str, int]:
        with self._state_lock:
            active = sum(len(clients) for clients in self._by_device.values())
            result = {
                "active_connections": active,
                "active_devices": len(self._by_device),
                "queued_events": sum(
                    c.queue.qsize()
                    for clients in self._by_device.values()
                    for c in clients
                ),
            }
        result.update(_counter_snapshot())
        return result


hub = _Hub()


def realtime_metrics() -> dict[str, int]:
    return hub.metrics()


def _safe_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _client_ip(websocket: WebSocket) -> str:
    return client_ip(websocket)  # Request e WebSocket expõem headers/client iguais.


async def _subscribe(client: _Client, values: Any) -> None:
    if not isinstance(values, list) or len(values) > settings.websocket_max_subscriptions:
        raise ValueError("Limite de inscrições excedido.")
    ids = {_safe_uuid(value) for value in values}
    if None in ids:
        raise ValueError("download_id inválido.")
    clean_ids = {value for value in ids if value is not None}
    db = SessionLocal()
    try:
        owned_rows = db.query(Download).filter(
                Download.id.in_(clean_ids), Download.device_id == client.device_id
            ).all()
        owned = {row.id for row in owned_rows}
    finally:
        db.close()
    if owned != clean_ids:
        raise PermissionError("Tarefa não autorizada.")
    client.subscriptions = clean_ids
    for download in owned_rows:
        try:
            client.queue.put_nowait(build_download_event(download, "download.snapshot"))
        except asyncio.QueueFull:
            await client.websocket.close(code=1013, reason="Cliente lento; sincronize pela API.")
            break
    logger.debug("websocket_subscribed count=%s", len(clean_ids))


async def _receiver(client: _Client) -> None:
    while True:
        text = await client.websocket.receive_text()
        if len(text.encode("utf-8")) > settings.websocket_max_message_bytes:
            raise ValueError("Mensagem muito grande.")
        now = time.monotonic()
        client.message_times.append(now)
        while client.message_times and now - client.message_times[0] > 60:
            client.message_times.popleft()
        if len(client.message_times) > settings.websocket_messages_per_minute:
            raise ValueError("Limite de mensagens excedido.")
        try:
            message = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("JSON inválido.") from exc
        if not isinstance(message, dict):
            raise ValueError("Mensagem inválida.")
        message_type = message.get("type")
        if message_type == "pong":
            client.last_pong = now
        elif message_type == "subscribe":
            await _subscribe(client, message.get("download_ids"))
        elif message_type == "unsubscribe":
            client.subscriptions.clear()
            logger.debug("websocket_unsubscribed")
        else:
            raise ValueError("Tipo de mensagem não permitido.")


async def _sender(client: _Client) -> None:
    while True:
        event = await client.queue.get()
        await client.websocket.send_json(event)
        _count("events_sent")


async def _heartbeat(client: _Client) -> None:
    while True:
        await asyncio.sleep(settings.websocket_heartbeat_seconds)
        now = time.monotonic()
        if time.time() >= client.expires_at:
            await client.websocket.close(code=4001, reason="Sessão expirada.")
            return
        if now - client.last_pong > settings.websocket_pong_timeout_seconds:
            _count("heartbeat_timeouts")
            await client.websocket.close(code=4002, reason="Heartbeat expirado.")
            return
        await client.websocket.send_json({
            "version": 1,
            "type": "connection.ping",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


@realtime_router.post("/websocket/ticket")
def websocket_ticket(
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    token, expires_in = create_websocket_token(ctx.device.id)
    payload = decode_websocket_token(token)
    if not payload:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Ticket indisponível.")
    db.add(SecurityNonce(
        id=f"ws:{payload['jti']}",
        kind="websocket",
        device_public_key=ctx.device.public_key,
        expires_at=datetime.fromtimestamp(float(payload["exp"]), timezone.utc),
    ))
    db.commit()
    return {"token": token, "expires_in": expires_in}


@realtime_router.websocket("/websocket/downloads")
@realtime_router.websocket("/ws/tasks")
async def websocket_downloads(websocket: WebSocket) -> None:
    origin = (websocket.headers.get("origin") or "").rstrip("/")
    if origin and origin not in settings.websocket_origin_allowlist:
        await websocket.close(code=1008, reason="Origem não permitida.")
        return
    await websocket.accept()
    connection_ip = _client_ip(websocket)
    logger.info("IP conectado: %s", connection_ip)
    client: _Client | None = None
    tasks: list[asyncio.Task] = []
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=5)
        if len(raw.encode("utf-8")) > settings.websocket_max_message_bytes:
            raise ValueError("Mensagem muito grande.")
        auth = json.loads(raw)
        if not isinstance(auth, dict) or auth.get("type") != "authenticate":
            raise ValueError("Autenticação obrigatória.")
        payload = decode_websocket_token(str(auth.get("token") or ""))
        device_id = _safe_uuid(payload.get("dev")) if payload else None
        if not payload or not device_id:
            _count("authentication_failures")
            await websocket.close(code=4001, reason="Token inválido ou expirado.")
            return
        db = SessionLocal()
        try:
            device = db.get(Device, device_id)
            ip = _client_ip(websocket)
            ticket_used = db.execute(
                delete(SecurityNonce)
                .where(
                    SecurityNonce.id == f"ws:{payload['jti']}",
                    SecurityNonce.kind == "websocket",
                    SecurityNonce.expires_at > datetime.now(timezone.utc),
                )
                .returning(SecurityNonce.id)
            ).scalar_one_or_none()
            db.commit()
            allowed = bool(
                ticket_used and device and device.status != DeviceStatus.blocked
                and not fraud.is_banned(db, device_id=device_id, ip=ip)
                and (device.platform != "web" or bool(origin))
            )
        finally:
            db.close()
        if not allowed:
            await websocket.close(code=4003, reason="Dispositivo não autorizado.")
            return
        client = _Client(websocket, device_id, ip, float(payload["exp"]))
        if not await hub.add(client):
            _count("connection_limits")
            logger.warning("websocket_connection_limit_reached ip=%s", ip)
            await websocket.close(code=4008, reason="Limite de conexões atingido.")
            return
        logger.debug("websocket_authenticated device_id=%s", device_id)
        await websocket.send_json({"version": 1, "type": "connection.ready"})
        tasks = [
            asyncio.create_task(_receiver(client)),
            asyncio.create_task(_sender(client)),
            asyncio.create_task(_heartbeat(client)),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            error = task.exception()
            if error:
                raise error
    except (ValueError, PermissionError, json.JSONDecodeError) as exc:
        _count("invalid_messages")
        try:
            await websocket.close(code=1008, reason=str(exc)[:120])
        except RuntimeError:
            pass
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if client:
            await hub.remove(client)
        logger.info("IP desconectado: %s", connection_ip)


def _load_target_events(
    targets: dict[uuid.UUID, set[uuid.UUID] | None],
) -> list[tuple[uuid.UUID, dict[str, Any]]]:
    db = SessionLocal()
    events: list[tuple[uuid.UUID, dict[str, Any]]] = []
    try:
        for device_id, task_ids in targets.items():
            statement = select(Download).where(Download.device_id == device_id)
            if task_ids is not None:
                if not task_ids:
                    continue
                statement = statement.where(Download.id.in_(task_ids))
            else:
                statement = statement.order_by(Download.updated_at.desc()).limit(
                    settings.websocket_max_subscriptions,
                )
            for download in db.execute(statement).scalars():
                events.append((device_id, build_download_event(download)))
        return events
    finally:
        db.close()


async def _poll_persisted_events(stop: asyncio.Event) -> None:
    """Sincroniza revisões para os WebSockets desta instância da API."""
    revisions: dict[tuple[uuid.UUID, str], int] = {}
    while not stop.is_set():
        targets = await hub.targets()
        active_keys: set[tuple[uuid.UUID, str]] = set()
        if targets:
            try:
                events = await asyncio.to_thread(_load_target_events, targets)
                for device_id, event in events:
                    key = (device_id, str(event["download_id"]))
                    active_keys.add(key)
                    revision = int(event.get("revision") or 0)
                    if revisions.get(key, -1) >= revision:
                        continue
                    revisions[key] = revision
                    await hub.dispatch(device_id, event)
                    _count("events_synchronized")
            except Exception as exc:  # noqa: BLE001 - próxima rodada reconcilia
                _count("synchronization_failures")
                logger.warning("websocket_state_sync_failed type=%s", type(exc).__name__)
        for key in tuple(revisions):
            if key not in active_keys:
                revisions.pop(key, None)
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=settings.progress_update_interval,
            )
        except asyncio.TimeoutError:
            pass


async def start_realtime() -> None:
    global _event_loop, _poll_task, _poll_stop
    if not settings.websocket_enabled or _poll_task:
        return
    _event_loop = asyncio.get_running_loop()
    _poll_stop = asyncio.Event()
    _poll_task = asyncio.create_task(
        _poll_persisted_events(_poll_stop), name="websocket-state-sync",
    )


async def stop_realtime() -> None:
    global _event_loop, _poll_task, _poll_stop
    if _poll_stop:
        _poll_stop.set()
    if _poll_task:
        _poll_task.cancel()
        await asyncio.gather(_poll_task, return_exceptions=True)
    _poll_task = None
    _poll_stop = None
    _event_loop = None
