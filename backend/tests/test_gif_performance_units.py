"""Regressões de configuração, entrega e sinalização do conversor."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from backend import main as backend_main
from backend.api.config import Settings, effective_cpu_count
from backend.database.models import DownloadStatus
from backend.downloads import router
from backend.workers import tasks
from backend.workers.gif_converter import GifConversionCancelled


def test_limites_automaticos_nao_ultrapassam_cpu_efetiva():
    settings = Settings(
        environment="test",
        jwt_secret="test-secret",
        max_concurrent_tasks=32,
        gif_ffmpeg_threads=32,
        gif_ffmpeg_filter_threads=32,
    )
    assert 1 <= settings.resolved_task_concurrency <= effective_cpu_count()
    assert 1 <= settings.resolved_gif_ffmpeg_threads <= 8
    assert 1 <= settings.resolved_gif_ffmpeg_filter_threads <= 4


def test_x_accel_nao_expoe_caminho_real(monkeypatch, tmp_path):
    device = tmp_path / "device"
    device.mkdir()
    output = device / "arquivo final.gif"
    output.write_bytes(b"GIF89a")
    monkeypatch.setattr(router.settings, "downloads_dir", str(tmp_path))
    monkeypatch.setattr(router.settings, "nginx_internal_downloads_enabled", True)
    monkeypatch.setattr(router.settings, "nginx_internal_downloads_uri", "/internal-downloads")

    response = router._delivery_response(output.resolve(), "Teste")
    redirect = response.headers["x-accel-redirect"]
    assert redirect == "/internal-downloads/device/arquivo%20final.gif"
    assert str(tmp_path) not in redirect
    assert "Teste.gif" in response.headers["content-disposition"]


def test_cancelamento_consulta_fonte_persistente_com_intervalo(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks.settings, "task_cancel_check_interval", 60)
    monkeypatch.setattr(
        tasks,
        "_cancel_requested",
        lambda task_id: calls.append(task_id) or True,
    )
    checker = tasks._make_cancel_checker("abc")
    assert checker()
    assert checker()
    assert calls == ["abc"]


def test_progresso_nao_derruba_tarefa_quando_postgres_falha(monkeypatch):
    class BrokenSession:
        def get(self, *_args):
            raise RuntimeError("postgres offline")

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(tasks, "SessionLocal", BrokenSession)
    tasks._progress_last_write.clear()
    tasks._store_progress("00000000-0000-0000-0000-000000000001", 10, "processing")
    tasks._progress_last_write.clear()


@pytest.mark.parametrize("states", [(True,), (False, True)])
def test_cancelamento_na_publicacao_atomica_remove_destino(tmp_path, states):
    generated = tmp_path / "result.gif"
    destination = tmp_path / "published.gif"
    generated.write_bytes(b"GIF89a")
    values = iter(states)

    with pytest.raises(GifConversionCancelled):
        tasks._publish_generated_file(
            generated,
            destination,
            lambda: next(values),
        )

    assert not destination.exists()


def test_falha_do_banco_remove_upload_sem_abandonar_arquivo(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    download = SimpleNamespace(
        id=uuid.uuid4(),
        device_id=uuid.uuid4(),
        deduplication_key="a" * 64,
        operation_type="gif_upload",
        source_path=str(source),
        status=DownloadStatus.queued,
        stage=None,
        error=None,
    )

    class FakeDb:
        def __init__(self):
            self.commits = 0

        def add(self, _download):
            return None

        def execute(self, _statement):
            return None

        def commit(self):
            self.commits += 1
            raise RuntimeError("banco indisponível")

        def rollback(self):
            return None

    db = FakeDb()
    monkeypatch.setattr(
        router.service,
        "safe_storage_file",
        lambda value: source if value == str(source) and source.exists() else None,
    )
    monkeypatch.setattr(
        router.service, "active_duplicate", lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(router.service, "enforce_usage_limits", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="banco"):
        router._persist_and_enqueue(db, download)

    assert db.commits == 1
    assert download.source_path is None
    assert not source.exists()


def test_main_inicia_somente_a_api(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(backend_main, "_load_env", lambda: None)
    monkeypatch.setattr(backend_main, "_ensure_dirs", lambda: None)
    monkeypatch.setattr(backend_main, "_run_api", lambda: calls.append("api"))
    backend_main.main()
    assert calls == ["api"]
