"""Contratos da fila persistente e do isolamento de processos."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from types import SimpleNamespace

import pytest

from backend import observability
from backend.database.models import DownloadStatus
from backend.downloads.service import build_deduplication_key, build_format_spec
from backend.tasks import manager as manager_module
from backend.tasks.manager import TaskManager, _RunningTask
from backend.workers import tasks


def test_mp4_prefere_h264_e_aac_nativos_na_resolucao_exata():
    spec = build_format_spec("va", 1080, "vp9-60", "mp4")

    assert spec == (
        "(bv[vcodec^=avc1][height=1080]/vp9-60)+"
        "(ba[ext=m4a]/ba)/b[height<=1080]"
    )
    assert len(spec) <= 128


def test_mkv_preserva_seletor_anterior_sem_forcar_h264():
    assert build_format_spec("va", 2160, "av1-4k", "mkv") == (
        "av1-4k+bestaudio/bestvideo[height<=2160]+bestaudio/"
        "best[height<=2160]"
    )


def test_video_sem_audio_mp4_tambem_prefere_h264_nativo():
    assert build_format_spec("v", 720, "vp9-720", "mp4") == (
        "bv[vcodec^=avc1][height=720]/vp9-720/b[height<=720]"
    )


def test_seletor_mp4_cabe_na_coluna_do_banco_com_format_id_maximo():
    format_id = "x" * 32

    assert len(build_format_spec("va", 4320, format_id, "mp4")) <= 128
    assert len(build_format_spec("v", 4320, format_id, "mp4")) <= 128


def test_ytdlp_desativa_progresso_nativo_do_console():
    options = tasks._ytdlp_base_opts()

    assert options["quiet"] is True
    assert options["no_warnings"] is True
    assert options["noprogress"] is True


def test_progresso_do_download_emite_uma_linha_por_marco_de_cinco(caplog):
    task_id = "12345678-0000-0000-0000-000000000000"
    state = {"logged_percent": -1}

    with caplog.at_level(logging.INFO, logger="backend.workers.tasks"):
        tasks._log_download_progress(task_id, 12.8, state)
        tasks._log_download_progress(task_id, 12.9, state)
        tasks._log_download_progress(task_id, 20.1, state)
        tasks._log_download_progress(task_id, 18.0, state)

    assert [record.getMessage() for record in caplog.records] == [
        "Download 12345678: 0%",
        "Download 12345678: 5%",
        "Download 12345678: 10%",
        "Download 12345678: 15%",
        "Download 12345678: 20%",
    ]
    assert state["logged_percent"] == 20


def test_limpeza_de_memoria_antes_da_conversao(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks.gc, "collect", lambda: calls.append("gc"))
    monkeypatch.setattr(
        tasks, "release_freed_memory", lambda: calls.append("malloc_trim"),
    )

    tasks._release_memory_before_conversion()

    assert calls == ["gc", "malloc_trim"]


def test_chave_de_deduplicacao_e_estavel_e_isolada_por_usuario():
    device = uuid.uuid4()
    values = dict(
        device_id=device,
        url="https://www.youtube.com/watch?v=abc",
        operation_type="download",
        mode="va",
        format_spec="best",
        output_format="mp4",
    )
    first = build_deduplication_key(**values)
    assert first == build_deduplication_key(**values)
    assert first != build_deduplication_key(**{**values, "device_id": uuid.uuid4()})
    assert len(first) == 64


def test_dispatch_rejeita_operacao_arbitraria():
    with pytest.raises(ValueError, match="Tipo interno"):
        tasks.run_operation("id", "executar-comando")


def test_limpeza_por_tarefa_preserva_somente_upload_em_retry(monkeypatch, tmp_path):
    task_id = str(uuid.uuid4())
    device_id = str(uuid.uuid4())
    task_dir = tmp_path / device_id / task_id
    task_dir.mkdir(parents=True)
    source = task_dir / "source-upload.mp4"
    residue = task_dir / "fragment.part"
    source.write_bytes(b"video")
    residue.write_bytes(b"partial")
    monkeypatch.setattr(tasks.settings, "downloads_dir", str(tmp_path))

    tasks.cleanup_task_files(task_id, device_id, preserve_source=True)
    assert source.exists()
    assert not residue.exists()

    tasks.cleanup_task_files(task_id, device_id)
    assert not task_dir.exists()


def test_retry_tem_backoff_persistido(monkeypatch):
    monkeypatch.setattr(tasks.settings, "task_max_retries", 3)
    download = SimpleNamespace(
        attempts=2,
        status=DownloadStatus.processing,
        stage="processing",
        progress_message=None,
        error="erro",
        worker_id="processo",
        heartbeat_at=object(),
        queued_at=None,
    )
    assert tasks._schedule_retry(download, TimeoutError())
    assert download.status == DownloadStatus.queued
    assert download.stage == "retrying"
    assert download.worker_id is None
    assert download.queued_at is not None


def test_timeout_encerra_arvore_e_finaliza_registro(monkeypatch):
    task_id = uuid.uuid4()

    class Process:
        pid = 123
        exitcode = None

        def is_alive(self):
            return True

    process = Process()
    manager = TaskManager()
    manager._running[task_id] = _RunningTask(
        process=process,
        operation_type="download",
        started_monotonic=time.monotonic() - 10,
    )
    terminated = []
    finalized = []
    monkeypatch.setattr(manager_module.settings, "task_timeout", 1)
    monkeypatch.setattr(
        manager,
        "_task_snapshots",
        lambda _ids: {task_id: (DownloadStatus.processing, False)},
    )
    monkeypatch.setattr(
        manager_module,
        "_terminate_process_tree",
        lambda item: terminated.append(item),
    )
    monkeypatch.setattr(
        manager,
        "_finalize_terminated",
        lambda item, reason: finalized.append((item, reason)),
    )

    asyncio.run(manager._reap_processes())

    assert terminated == [process]
    assert finalized == [(task_id, "timeout")]
    assert task_id not in manager._running


def test_admissao_libera_tarefas_quando_nao_ha_cgroup_real(monkeypatch):
    manager = TaskManager()
    monkeypatch.setattr(manager_module, "container_memory_snapshot", lambda: None)

    assert manager._memory_admits_new_task() is True


def test_admissao_bloqueia_com_pouca_memoria_disponivel(monkeypatch):
    manager = TaskManager()
    snapshot = lambda: (1024 * 1024 * 1024, 900 * 1024 * 1024)  # noqa: E731
    monkeypatch.setattr(manager_module, "container_memory_snapshot", snapshot)
    monkeypatch.setattr(observability, "container_memory_snapshot", snapshot)
    monkeypatch.setattr(manager_module.settings, "worker_memory_limit_mb", None)
    monkeypatch.setattr(manager_module.settings, "task_admission_min_available_mb", 200)

    assert manager._memory_admits_new_task() is False


def test_start_available_tasks_nao_consulta_fila_com_memoria_baixa(monkeypatch):
    task_id = uuid.uuid4()
    process = SimpleNamespace(pid=1)
    manager = TaskManager()
    manager._running[task_id] = _RunningTask(
        process=process,
        operation_type="download",
        started_monotonic=time.monotonic(),
    )
    monkeypatch.setattr(manager, "_memory_admits_new_task", lambda: False)

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("Não deveria consultar candidatos com memória baixa")

    monkeypatch.setattr(manager, "_queued_candidates", must_not_run)

    asyncio.run(manager._start_available_tasks())

    assert manager._memory_admission_blocked is True
    assert task_id in manager._running
