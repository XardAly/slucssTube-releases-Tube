"""Contrato unitário do canal em tempo real, sem serviços externos."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from types import SimpleNamespace

import pytest

from backend.auth.tokens import (
    create_websocket_token,
    decode_access_token,
    decode_websocket_token,
)
from backend.database.models import Download, DownloadStatus
from backend.realtime import (
    _Client,
    _Hub,
    _safe_uuid,
    _heartbeat,
    _receiver,
    build_download_event,
    infer_event_type,
    publish_download_event,
    settings,
    websocket_downloads,
)


def _download(**changes) -> Download:
    values = {
        "id": uuid.uuid4(),
        "device_id": uuid.uuid4(),
        "url": "https://example.invalid/video",
        "status": DownloadStatus.queued,
        "progress": 0.0,
        "stage": "queued",
        "revision": 1,
    }
    values.update(changes)
    return Download(**values)


class _Socket:
    def __init__(self, messages=None) -> None:
        self.closed = []
        self.sent = []
        self.messages = iter(messages or [])

    async def close(self, **kwargs) -> None:
        self.closed.append(kwargs)

    async def send_json(self, message) -> None:
        self.sent.append(message)

    async def receive_text(self) -> str:
        return next(self.messages)


class _ConnectionSocket(_Socket):
    def __init__(self, ip: str) -> None:
        super().__init__(["{\"type\":\"authenticate\",\"token\":\"invalido\"}"])
        self.headers = {}
        self.client = SimpleNamespace(host=ip)
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True


def test_conexao_registra_somente_ip_conectado_e_desconectado(caplog):
    socket = _ConnectionSocket("203.0.113.7")

    with caplog.at_level(logging.INFO, logger="backend.realtime"):
        asyncio.run(websocket_downloads(socket))

    assert socket.accepted
    assert [record.getMessage() for record in caplog.records] == [
        "IP conectado: 203.0.113.7",
        "IP desconectado: 203.0.113.7",
    ]


def test_token_websocket_valido_carrega_dispositivo():
    device_id = uuid.uuid4()
    token, ttl = create_websocket_token(device_id)
    assert ttl > 0
    assert decode_websocket_token(token)["dev"] == str(device_id)


def test_token_websocket_nao_e_aceito_como_access_token():
    token, _ = create_websocket_token(uuid.uuid4())
    assert decode_access_token(token) is None


def test_token_websocket_adulterado_e_rejeitado():
    token, _ = create_websocket_token(uuid.uuid4())
    assert decode_websocket_token(token + "x") is None


def test_token_websocket_expirado_e_rejeitado(monkeypatch):
    monkeypatch.setattr(
        settings,
        "websocket_ticket_ttl_seconds",
        -(settings.jwt_clock_skew_seconds + 1),
    )
    token, _ = create_websocket_token(uuid.uuid4())
    assert decode_websocket_token(token) is None


def test_evento_queued_e_versionado():
    event = build_download_event(_download())
    assert event["version"] == 1
    assert event["type"] == "download.queued"


def test_evento_started_explicito():
    event = build_download_event(
        _download(status=DownloadStatus.processing), "download.started"
    )
    assert event["type"] == "download.started"


def test_evento_progress_carrega_etapa_e_percentual():
    event = build_download_event(_download(
        status=DownloadStatus.processing, progress=48.0, stage="downloading", revision=18,
    ))
    assert event["progress"] == 48.0
    assert event["stage"] == "downloading"
    assert event["revision"] == 18


def test_evento_completed_e_terminal():
    event = build_download_event(_download(status=DownloadStatus.completed, progress=100.0))
    assert event["type"] == "download.completed"


def test_evento_failed_nao_expoe_excecao_original():
    event = build_download_event(_download(
        status=DownloadStatus.failed,
        error="Traceback /srv/private/token=segredo",
    ))
    assert event["error_code"] == "PROCESSING_FAILED"
    assert "Traceback" not in event["message"]
    assert "segredo" not in str(event)


def test_cancelamento_interno_vira_status_publico_cancelled():
    event = build_download_event(_download(
        status=DownloadStatus.cancelled, stage="cancelled",
    ))
    assert event["status"] == "cancelled"
    assert event["type"] == "download.cancelled"


def test_cancel_requested_tem_tipo_proprio():
    assert infer_event_type(_download(
        status=DownloadStatus.processing, cancel_requested=True,
    )) == "download.cancel_requested"


def test_evento_nao_expoe_worker_caminho_url_ou_device():
    event = build_download_event(_download(
        worker_id="worker-private", file_path="/srv/private/file.mp4",
        storage_key="drive-private-id", storage_reserved_key="drive-reserved-id",
        remote_parent_id="drive-private-folder",
    ))
    assert not {
        "worker_id", "file_path", "url", "device_id",
        "storage_key", "storage_reserved_key", "remote_parent_id", "remote_checksum",
    }.intersection(event)


def test_safe_uuid_aceita_uuid_valido():
    value = uuid.uuid4()
    assert _safe_uuid(str(value)) == value


def test_safe_uuid_rejeita_valor_invalido():
    assert _safe_uuid("nao-e-uuid") is None


def test_hub_entrega_somente_ao_dispositivo_correto():
    async def scenario():
        hub = _Hub()
        device = uuid.uuid4()
        own = _Client(_Socket(), device, "1.1.1.1", time.time() + 60)
        other = _Client(_Socket(), uuid.uuid4(), "2.2.2.2", time.time() + 60)
        assert await hub.add(own)
        assert await hub.add(other)
        await hub.dispatch(device, {"download_id": str(uuid.uuid4())})
        assert own.queue.qsize() == 1
        assert other.queue.empty()
    asyncio.run(scenario())


def test_hub_respeita_filtro_de_inscricao():
    async def scenario():
        hub = _Hub()
        device = uuid.uuid4()
        wanted, ignored = uuid.uuid4(), uuid.uuid4()
        client = _Client(_Socket(), device, "1.1.1.1", time.time() + 60)
        client.subscriptions = {wanted}
        await hub.add(client)
        await hub.dispatch(device, {"download_id": str(ignored)})
        assert client.queue.empty()
        await hub.dispatch(device, {"download_id": str(wanted)})
        assert client.queue.qsize() == 1
    asyncio.run(scenario())


def test_hub_fecha_cliente_lento_sem_crescer_fila(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "websocket_client_queue_size", 1)
        hub = _Hub()
        socket = _Socket()
        device = uuid.uuid4()
        client = _Client(socket, device, "1.1.1.1", time.time() + 60)
        client.queue = asyncio.Queue(maxsize=1)
        await hub.add(client)
        event = {"download_id": str(uuid.uuid4())}
        await hub.dispatch(device, event)
        await hub.dispatch(device, event)
        assert client.queue.qsize() == 1
        assert socket.closed[0]["code"] == 1013
    asyncio.run(scenario())


def test_hub_limita_conexoes_por_dispositivo(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "websocket_max_connections_per_device", 1)
        hub = _Hub()
        device = uuid.uuid4()
        assert await hub.add(_Client(_Socket(), device, "1.1.1.1", time.time() + 60))
        assert not await hub.add(_Client(_Socket(), device, "1.1.1.2", time.time() + 60))
    asyncio.run(scenario())


def test_hub_limita_conexoes_por_ip(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "websocket_max_connections_per_ip", 1)
        hub = _Hub()
        assert await hub.add(_Client(_Socket(), uuid.uuid4(), "1.1.1.1", time.time() + 60))
        assert not await hub.add(_Client(_Socket(), uuid.uuid4(), "1.1.1.1", time.time() + 60))
    asyncio.run(scenario())


def test_hub_remove_libera_contadores():
    async def scenario():
        hub = _Hub()
        client = _Client(_Socket(), uuid.uuid4(), "1.1.1.1", time.time() + 60)
        await hub.add(client)
        await hub.remove(client)
        assert hub.metrics()["active_connections"] == 0
        assert hub.metrics()["active_devices"] == 0
    asyncio.run(scenario())


def test_eventos_repetidos_sao_idempotentes_por_revisao():
    first = build_download_event(_download(revision=7))
    duplicate = dict(first)
    assert duplicate["revision"] == first["revision"]


def test_publicacao_entrega_exatamente_um_evento_ao_hub(monkeypatch):
    import backend.realtime as realtime

    async def scenario():
        calls = []

        async def dispatch(device_id, event):
            calls.append((device_id, event))

        monkeypatch.setattr(realtime, "_event_loop", asyncio.get_running_loop())
        monkeypatch.setattr(realtime.hub, "dispatch", dispatch)
        assert publish_download_event(_download())
        await asyncio.sleep(0)
        assert len(calls) == 1

    try:
        asyncio.run(scenario())
    finally:
        realtime._event_loop = None


def test_cliente_sem_pong_e_fechado(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "websocket_heartbeat_seconds", 0)
        monkeypatch.setattr(settings, "websocket_pong_timeout_seconds", -1)
        socket = _Socket()
        client = _Client(socket, uuid.uuid4(), "1.1.1.1", time.time() + 60)
        await _heartbeat(client)
        assert socket.closed[0]["code"] == 4002
    asyncio.run(scenario())


def test_conexao_fecha_quando_sessao_expira(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "websocket_heartbeat_seconds", 0)
        socket = _Socket()
        client = _Client(socket, uuid.uuid4(), "1.1.1.1", time.time() - 1)
        await _heartbeat(client)
        assert socket.closed[0]["code"] == 4001
    asyncio.run(scenario())


def test_receiver_rejeita_json_invalido():
    client = _Client(_Socket(["{"]), uuid.uuid4(), "1.1.1.1", time.time() + 60)
    with pytest.raises(ValueError, match="JSON"):
        asyncio.run(_receiver(client))


def test_receiver_rejeita_tipo_nao_permitido():
    client = _Client(
        _Socket(['{"type":"execute_worker"}']),
        uuid.uuid4(), "1.1.1.1", time.time() + 60,
    )
    with pytest.raises(ValueError, match="Tipo"):
        asyncio.run(_receiver(client))


def test_receiver_rejeita_mensagem_grande(monkeypatch):
    monkeypatch.setattr(settings, "websocket_max_message_bytes", 8)
    client = _Client(_Socket(["x" * 20]), uuid.uuid4(), "1.1.1.1", time.time() + 60)
    with pytest.raises(ValueError, match="grande"):
        asyncio.run(_receiver(client))


def test_receiver_aplica_limite_de_mensagens(monkeypatch):
    monkeypatch.setattr(settings, "websocket_messages_per_minute", 1)
    client = _Client(
        _Socket(['{"type":"pong"}', '{"type":"pong"}']),
        uuid.uuid4(), "1.1.1.1", time.time() + 60,
    )
    with pytest.raises(ValueError, match="Limite"):
        asyncio.run(_receiver(client))
