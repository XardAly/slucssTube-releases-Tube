"""Contratos das otimizações globais de recursos."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import tracemalloc
from contextlib import contextmanager

import httpx
import jwt

from backend.api.config import get_settings
from backend.database.models import ApiRequest, SecurityLog
from backend.downloads import service as download_service
from backend.security import integrity
from backend.tasks import runner as task_runner
from backend.workers import binary_install
from backend.workers import tasks


def test_pool_defaults_sao_limitados() -> None:
    settings = get_settings()
    assert settings.database_pool_size == 5
    assert settings.database_max_overflow == 5
    assert settings.max_concurrent_tasks == 2
    assert settings.max_queue_size == 100
    assert settings.api_limit_concurrency == 500
    assert settings.api_backlog == 256
    assert settings.video_info_concurrency == 2


def test_indices_compostos_cobrem_consultas_frequentes() -> None:
    api_indexes = {index.name for index in ApiRequest.__table__.indexes}
    security_indexes = {index.name for index in SecurityLog.__table__.indexes}
    assert "ix_api_requests_device_timestamp" in api_indexes
    assert "ix_api_requests_ip_timestamp" in api_indexes
    assert "ix_api_requests_device_endpoint_timestamp" in api_indexes
    assert "ix_security_logs_ip_event_created" in security_indexes


def test_processo_isolado_descarta_pool_e_despacha_operacao(monkeypatch) -> None:
    calls: list[bool] = []
    operations = []
    from backend.database import session as database_session

    monkeypatch.setattr(
        database_session.engine,
        "dispose",
        lambda *, close=True: calls.append(close),
    )
    monkeypatch.setattr(tasks, "run_operation", lambda *args: operations.append(args))
    task_runner.run_task_process("id", "download")
    assert calls == [False, True]
    assert operations == [("id", "download")]


def test_token_google_e_reutilizado_ate_expirar(tmp_path, monkeypatch) -> None:
    service_account = tmp_path / "service-account.json"
    service_account.write_text(json.dumps({
        "client_email": "worker@example.invalid",
        "token_uri": "https://oauth2.googleapis.com/token",
        "private_key": "test-key",
    }))
    monkeypatch.setattr(jwt, "encode", lambda *args, **kwargs: "assertion")
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"access_token": "cached-token", "expires_in": 3600},
        )

    async def scenario() -> None:
        integrity._access_token_cache = None
        integrity._token_lock = None
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as client:
            first = await integrity._service_account_access_token(service_account, client)
            second = await integrity._service_account_access_token(service_account, client)
        assert first == second == "cached-token"

    asyncio.run(scenario())
    integrity._access_token_cache = None
    integrity._token_lock = None
    assert calls == 1


def test_limpeza_de_orfaos_consulta_banco_em_lotes(tmp_path, monkeypatch) -> None:
    registered = tmp_path / "registered.mp4"
    orphan = tmp_path / "orphan.mp4"
    temporary = tmp_path / "unfinished.part"
    for path in (registered, orphan, temporary):
        path.write_bytes(b"x")
        old = time.time() - 48 * 3600
        os.utime(path, (old, old))

    class _Result:
        def scalars(self):
            return iter([str(registered)])

    class _Db:
        calls = 0
        closed = False

        def execute(self, _statement):
            self.calls += 1
            return _Result()

        def close(self):
            self.closed = True

    db = _Db()
    monkeypatch.setattr(tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(tasks.settings, "downloads_dir", str(tmp_path))
    monkeypatch.setattr(tasks.settings, "download_file_ttl_hours", 6)

    removed = tasks.cleanup_orphaned_files()

    assert registered.exists()
    assert not orphan.exists()
    assert not temporary.exists()
    assert removed == 2
    assert db.calls == 2
    assert db.closed


def test_heartbeat_independente_para_estagios_silenciosos(monkeypatch) -> None:
    sessions = []

    class _Result:
        rowcount = 1

    class _Db:
        def __init__(self):
            self.commits = 0
            self.closed = False
            sessions.append(self)

        def execute(self, _statement):
            return _Result()

        def commit(self):
            self.commits += 1

        def rollback(self):
            pass

        def close(self):
            self.closed = True

    monkeypatch.setattr(tasks, "SessionLocal", _Db)
    task_id = "5dbd92f7-70df-4c9d-a670-2d0359e5c2b0"
    stop = tasks._start_task_heartbeat(task_id, interval_seconds=0.01)
    deadline = time.monotonic() + 1
    while not sessions and time.monotonic() < deadline:
        time.sleep(0.01)
    stop()

    assert sessions and sessions[0].commits >= 1 and sessions[0].closed
    assert not any(
        thread.is_alive() and thread.name == "task-heartbeat-5dbd92f7"
        for thread in threading.enumerate()
    )


def test_limite_de_download_pode_serializar_contagem() -> None:
    statements = []

    class _Result:
        def scalar_one(self):
            return 0

    class _Db:
        def execute(self, statement):
            statements.append(str(statement))
            return _Result()

    download_service.enforce_usage_limits(
        _Db(),
        "5dbd92f7-70df-4c9d-a670-2d0359e5c2b0",
        serialize=True,
    )
    assert "pg_advisory_xact_lock" in statements[0]
    assert len(statements) == 5


def test_download_de_binario_e_streaming_e_remove_temporario(monkeypatch) -> None:
    payload_size = 20 * 1024 * 1024
    reads: list[int] = []

    class _Response:
        headers = {"Content-Length": str(payload_size)}
        remaining = payload_size

        def read(self, size: int) -> bytes:
            reads.append(size)
            amount = min(size, self.remaining)
            self.remaining -= amount
            return b"a" * amount

    @contextmanager
    def fake_open(*args, **kwargs):
        yield _Response()

    monkeypatch.setattr(binary_install, "open_public_url", fake_open)
    import hashlib
    expected = hashlib.sha256()
    for _ in range(20):
        expected.update(b"a" * (1024 * 1024))

    tracemalloc.start()
    with binary_install.verified_archive(
        "https://github.com/archive.zip",
        expected.hexdigest(),
        max_bytes=21 * 1024 * 1024,
        allowed_hosts={"github.com"},
        prefix="test-stream-",
    ) as archive:
        assert archive.stat().st_size == payload_size
        saved_path = archive
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert not saved_path.exists()
    assert reads and max(reads) == 1024 * 1024
    assert peak < 4 * 1024 * 1024
