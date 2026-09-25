"""Contratos do transcode MP4 H.264/AAC aplicado aos downloads de vídeo."""

from __future__ import annotations

import os
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import observability
from backend.workers import ffmpeg_utils, tasks


class _FinishedProcess:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    def poll(self) -> int:
        return self.returncode


def test_argumentos_do_transcode_sao_fechados_e_sem_shell(tmp_path):
    source = tmp_path / "video de origem.mp4"
    output = tmp_path / "video final.mp4"

    args = tasks._build_compatible_mp4_args("ffmpeg", source, output)

    assert args == [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-nostdin",
        "-i", str(source),
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-threads", str(tasks.settings.resolved_compatibility_ffmpeg_threads),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-tag:v", "avc1",
        "-movflags", "+faststart",
        str(output),
    ]


@pytest.mark.parametrize(
    ("plan", "expected_codecs"),
    [
        ("copy", ["-c:v", "copy", "-c:a", "copy"]),
        (
            "copy_video",
            ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"],
        ),
    ],
)
def test_argumentos_de_copia_mantem_maps_tag_e_faststart(
    tmp_path, plan, expected_codecs,
):
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"

    args = tasks._build_compatible_mp4_args(
        "ffmpeg", source, output, plan=plan,
    )

    assert args[:12] == [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
        "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
    ]
    assert args[12:12 + len(expected_codecs)] == expected_codecs
    assert args[-5:] == [
        "-tag:v", "avc1", "-movflags", "+faststart", str(output),
    ]


def test_plano_de_compatibilidade_invalido_e_rejeitado(tmp_path):
    with pytest.raises(ValueError, match="Plano de compatibilidade inválido"):
        tasks._build_compatible_mp4_args(
            "ffmpeg", tmp_path / "in.mp4", tmp_path / "out.mp4", plan="x",
        )


@pytest.mark.parametrize(
    ("video", "audio", "expected"),
    [
        ({"codec_name": "h264", "pix_fmt": "yuv420p"}, None, "copy"),
        (
            {"codec_name": "h264", "pix_fmt": "yuv420p"},
            {"codec_name": "aac"},
            "copy",
        ),
        (
            {"codec_name": "h264", "pix_fmt": "yuv420p"},
            {"codec_name": "opus"},
            "copy_video",
        ),
        (
            {"codec_name": "vp9", "pix_fmt": "yuv420p"},
            {"codec_name": "aac"},
            "transcode",
        ),
        (
            {"codec_name": "h264", "pix_fmt": "yuv420p10le"},
            {"codec_name": "aac"},
            "transcode",
        ),
    ],
)
def test_probe_escolhe_o_menor_trabalho_seguro(
    tmp_path, monkeypatch, video, audio, expected,
):
    streams = iter((video, audio))
    monkeypatch.setattr(
        tasks,
        "_probe_first_compatibility_stream",
        lambda *_args, **_kwargs: next(streams),
    )

    assert tasks._compatibility_plan(
        "ffprobe", tmp_path / "source.mp4", lambda: False,
    ) == expected


def test_falha_do_probe_recai_em_transcode_seguro(tmp_path, monkeypatch):
    def fail(*_args, **_kwargs):
        raise TimeoutError("demorou")

    monkeypatch.setattr(tasks, "_probe_first_compatibility_stream", fail)

    assert tasks._compatibility_plan(
        "ffprobe", tmp_path / "source.mp4", lambda: False,
    ) == "transcode"


def test_video_incompativel_nao_executa_probe_de_audio(tmp_path, monkeypatch):
    selectors = []

    def fake_probe(_ffprobe, _source, selector, _cancelled):
        selectors.append(selector)
        return {"codec_name": "vp9", "pix_fmt": "yuv420p"}

    monkeypatch.setattr(tasks, "_probe_first_compatibility_stream", fake_probe)

    assert tasks._compatibility_plan(
        "ffprobe", tmp_path / "source.mp4", lambda: False,
    ) == "transcode"
    assert selectors == ["v:0"]


def test_probe_usa_primeiro_stream_limites_e_sem_shell(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        kwargs["stdout"].write(
            b'{"streams":[{"codec_name":"h264","pix_fmt":"yuv420p"}]}'
        )
        return _FinishedProcess(0)

    monkeypatch.setattr(tasks.subprocess, "Popen", fake_popen)

    result = tasks._probe_first_compatibility_stream(
        "ffprobe", source, "v:0", lambda: False,
    )

    assert result == {"codec_name": "h264", "pix_fmt": "yuv420p"}
    assert captured["args"][-1] == str(source)
    assert captured["args"][captured["args"].index("-select_streams") + 1] == "v:0"
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    assert captured["kwargs"]["stdout"] is not subprocess.PIPE
    assert captured["kwargs"]["stderr"] is not subprocess.PIPE
    assert captured["kwargs"]["start_new_session"] is True


def test_probe_cancela_processo_sem_deixar_pipe_em_memoria(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    checks = 0
    stopped = []

    class _RunningProbe:
        stopped = False

        def poll(self):
            return -15 if self.stopped else None

    process = _RunningProbe()

    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 2

    def fake_stop(received):
        stopped.append(received)
        received.stopped = True

    monkeypatch.setattr(tasks.subprocess, "Popen", lambda *_a, **_kw: process)
    monkeypatch.setattr(tasks, "_stop_ffmpeg_process", fake_stop)

    with pytest.raises(tasks.TaskProcessingCancelled):
        tasks._probe_first_compatibility_stream(
            "ffprobe", source, "v:0", cancelled,
        )

    assert stopped == [process]


def test_preferencia_h264_preserva_resolucao_e_evitar_transcode():
    formats = [
        {
            "format_id": "vp9-60", "height": 1080, "fps": 60,
            "vcodec": "vp9", "tbr": 5000,
        },
        {
            "format_id": "h264-30", "height": 1080, "fps": 30,
            "vcodec": "avc1.640028", "tbr": 6000,
        },
        {
            "format_id": "vp9-30", "height": 720, "fps": 30,
            "vcodec": "vp9", "tbr": 5000,
        },
        {
            "format_id": "h264-30-720", "height": 720, "fps": 30,
            "vcodec": "avc1.64001f", "tbr": 3000,
        },
    ]

    qualities = tasks._video_qualities(formats)

    assert next(q for q in qualities if q["height"] == 1080)["format_id"] == "h264-30"
    assert next(q for q in qualities if q["height"] == 720)["format_id"] == "h264-30-720"


def test_ffmpeg_precisa_oferecer_libx264_e_aac(monkeypatch, tmp_path):
    executable = tmp_path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    executable.write_bytes(b"stub")
    executable.chmod(0o755)

    monkeypatch.setattr(
        ffmpeg_utils.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                " V....D libx264  libx264 H.264 encoder\n"
                " A....D aac      AAC encoder\n"
            ),
            stderr="",
        ),
    )
    assert ffmpeg_utils._supports_compatibility_codecs(executable)

    monkeypatch.setattr(
        ffmpeg_utils.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=" A....D aac      AAC encoder\n",
            stderr="",
        ),
    )
    assert not ffmpeg_utils._supports_compatibility_codecs(executable)


def test_cgroup_de_512_mb_bloqueia_transcode_completo(tmp_path, monkeypatch):
    limit = tmp_path / "memory.max"
    usage = tmp_path / "memory.current"
    limit.write_text(str(512 * 1024 * 1024), encoding="ascii")
    usage.write_text(str(200 * 1024 * 1024), encoding="ascii")
    monkeypatch.setattr(observability, "_CGROUP_MEMORY_FILES", ((limit, usage),))

    allowed, limit_mb, available_mb = (
        tasks._compatibility_transcode_memory_status()
    )

    assert not allowed
    assert (limit_mb, available_mb) == (512, 312)


def test_cgroup_com_limite_e_folga_suficientes_permite_transcode(
    tmp_path, monkeypatch,
):
    limit = tmp_path / "memory.max"
    usage = tmp_path / "memory.current"
    limit.write_text(str(1024 * 1024 * 1024), encoding="ascii")
    usage.write_text(str(500 * 1024 * 1024), encoding="ascii")
    monkeypatch.setattr(observability, "_CGROUP_MEMORY_FILES", ((limit, usage),))

    assert tasks._compatibility_transcode_memory_status() == (
        True, 1024, 524,
    )


def test_cgroup_sem_limite_nao_inventa_ram_disponivel(tmp_path, monkeypatch):
    limit = tmp_path / "memory.max"
    usage = tmp_path / "memory.current"
    limit.write_text("max", encoding="ascii")
    usage.write_text("0", encoding="ascii")
    monkeypatch.setattr(observability, "_CGROUP_MEMORY_FILES", ((limit, usage),))

    assert tasks._compatibility_transcode_memory_status() == (True, 0, 0)


def test_limite_configurado_de_512_mb_protege_sem_cgroup(monkeypatch):
    monkeypatch.setattr(observability, "container_memory_snapshot", lambda: None)
    monkeypatch.setattr(tasks.settings, "worker_memory_limit_mb", 512)

    assert tasks._compatibility_transcode_memory_status() == (
        False, 512, 0,
    )


def test_ensure_ffmpeg_rejeita_binario_incompativel_do_path(monkeypatch, tmp_path):
    executable = tmp_path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    executable.write_bytes(b"stub")
    executable.chmod(0o755)
    monkeypatch.setattr(ffmpeg_utils, "_cached_location", None)
    monkeypatch.setattr(ffmpeg_utils, "_FFMPEG_DIR", tmp_path / "portable")
    monkeypatch.setattr(ffmpeg_utils.shutil, "which", lambda _name: str(executable))
    monkeypatch.setattr(
        ffmpeg_utils, "_supports_compatibility_codecs", lambda _path: False,
    )
    monkeypatch.setattr(
        ffmpeg_utils,
        "get_settings",
        lambda: SimpleNamespace(worker_binary_downloads_enabled=False),
    )

    with pytest.raises(RuntimeError, match="libx264 e AAC"):
        ffmpeg_utils.ensure_ffmpeg(require_compatibility_codecs=True)


@pytest.mark.parametrize(
    ("mode", "output_format", "expected"),
    [
        ("va", "mp4", True),
        ("v", "MP4", True),
        ("va", None, True),
        ("a", "mp4", False),
        ("p", "mp4", False),
        ("gif", "mp4", False),
        ("va", "mkv", False),
        ("v", "webm", False),
    ],
)
def test_transcode_e_aplicado_somente_a_video_mp4(
    mode, output_format, expected,
):
    assert tasks._requires_compatible_mp4_transcode(mode, output_format) is expected


def test_progresso_reserva_cinco_porcento_sem_voltar():
    first_stream = tasks._download_progress_percent(
        0, 100, 2, ceiling=95,
    )
    second_stream = tasks._download_progress_percent(
        1, 50, 2, ceiling=95, previous=first_stream,
    )
    completed_download = tasks._download_progress_percent(
        1, 100, 2, ceiling=95, previous=second_stream,
    )
    stale_update = tasks._download_progress_percent(
        0, 10, 2, ceiling=95, previous=completed_download,
    )

    assert (first_stream, second_stream, completed_download) == (47.5, 71.2, 95.0)
    assert stale_update == 95.0


def test_progresso_sem_compatibilidade_continua_chegando_a_cem():
    assert tasks._download_progress_percent(0, 100, 1) == 100.0


def test_progresso_ponderado_usa_bytes_reais_em_vez_de_50_50_por_arquivo():
    # Vídeo grande (900 MB) + áudio pequeno (100 MB): metade do vídeo baixado
    # deve valer bem menos que 50% da barra, ao contrário da divisão por
    # contagem de arquivos.
    video_total = 900 * 1024 * 1024
    audio_total = 100 * 1024 * 1024
    halfway_video = tasks._weighted_stream_progress(
        0, video_total // 2, video_total, video_total + audio_total,
    )
    assert halfway_video == pytest.approx(45.0, abs=0.1)

    # Áudio concluído depois do vídeo: bytes_done já soma o vídeo inteiro.
    audio_done = tasks._weighted_stream_progress(
        video_total, audio_total, audio_total, video_total + audio_total,
    )
    assert audio_done == pytest.approx(100.0, abs=0.01)


def test_progresso_ponderado_sem_tamanho_conhecido_retorna_none():
    assert tasks._weighted_stream_progress(0, 1024, 0, 0) is None


def test_ffmpeg_ausente_falha_com_erro_claro():
    with pytest.raises(RuntimeError, match="FFmpeg indisponível"):
        tasks._compatibility_ffmpeg_executable(None)


def test_sucesso_publica_atomicamente_e_remove_fonte_antiga(
    tmp_path, monkeypatch,
):
    source = tmp_path / "video de origem.webm"
    source.write_bytes(b"fonte")
    existing_mp4 = tmp_path / "video de origem.mp4"
    existing_mp4.write_bytes(b"arquivo-existente")
    captured: dict = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        Path(args[-1]).write_bytes(b"mp4-compativel")
        return _FinishedProcess(0)

    monkeypatch.setattr(tasks.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tasks, "_compatibility_plan", lambda *_args: "copy")

    result = tasks._transcode_compatible_mp4(
        "C:/FFmpeg/ffmpeg.exe", source, lambda: False,
    )

    assert result.parent == tmp_path
    assert result.suffix == ".mp4"
    assert result != existing_mp4
    assert result.read_bytes() == b"mp4-compativel"
    assert existing_mp4.read_bytes() == b"arquivo-existente"
    assert not source.exists()
    assert captured["args"][captured["args"].index("-i") + 1] == str(source)
    assert captured["args"][-1] != str(source)
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    assert captured["kwargs"]["stdout"] is subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] is not subprocess.PIPE
    assert captured["kwargs"]["start_new_session"] is True
    assert not list(tmp_path.glob("*.transcoding.mp4"))


@pytest.mark.parametrize("initial_plan", ["copy", "copy_video"])
def test_falha_da_copia_limpa_parcial_e_tenta_transcode_uma_vez(
    tmp_path, monkeypatch, initial_plan,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fonte-original")
    attempts = []
    temporary_paths = []

    def fake_popen(args, **kwargs):
        temporary = Path(args[-1])
        temporary_paths.append(temporary)
        video_codec = args[args.index("-c:v") + 1]
        attempts.append(video_codec)
        if len(attempts) == 1:
            temporary.write_bytes(b"parcial-da-copia")
            kwargs["stderr"].write(b"falha no remux")
            return _FinishedProcess(1)
        assert not temporary.exists()
        temporary.write_bytes(b"resultado-transcodificado")
        return _FinishedProcess(0)

    monkeypatch.setattr(tasks.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        tasks, "_compatibility_plan", lambda *_args: initial_plan,
    )

    result = tasks._transcode_compatible_mp4(
        "ffmpeg", source, lambda: False,
    )

    assert result == source
    assert result.read_bytes() == b"resultado-transcodificado"
    assert attempts == ["copy", "libx264"]
    assert len(set(temporary_paths)) == 1
    assert not temporary_paths[0].exists()


def test_cancelamento_apos_falha_da_copia_nao_inicia_retry(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fonte-original")
    checks = 0
    attempts = []

    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 4

    def fake_popen(args, **kwargs):
        attempts.append(args[args.index("-c:v") + 1])
        Path(args[-1]).write_bytes(b"parcial")
        kwargs["stderr"].write(b"falha na copia")
        return _FinishedProcess(1)

    monkeypatch.setattr(tasks.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tasks, "_compatibility_plan", lambda *_args: "copy")

    with pytest.raises(tasks.TaskProcessingCancelled):
        tasks._transcode_compatible_mp4("ffmpeg", source, cancelled)

    assert attempts == ["copy"]
    assert source.read_bytes() == b"fonte-original"
    assert not list(tmp_path.glob("*.transcoding.mp4"))


def test_retry_de_transcode_falho_propaga_erro_sem_terceira_tentativa(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fonte-original")
    attempts = []

    def fake_popen(args, **kwargs):
        video_codec = args[args.index("-c:v") + 1]
        attempts.append(video_codec)
        Path(args[-1]).write_bytes(b"parcial")
        kwargs["stderr"].write(
            b"falha final do transcode" if video_codec == "libx264"
            else b"falha inicial da copia"
        )
        return _FinishedProcess(1)

    monkeypatch.setattr(tasks.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tasks, "_compatibility_plan", lambda *_args: "copy")

    with pytest.raises(RuntimeError, match="falha final do transcode"):
        tasks._transcode_compatible_mp4("ffmpeg", source, lambda: False)

    assert attempts == ["copy", "libx264"]
    assert source.read_bytes() == b"fonte-original"
    assert not list(tmp_path.glob("*.transcoding.mp4"))


def test_falha_do_ffmpeg_preserva_fonte_e_remove_parcial(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fonte-original")
    temporary_paths: list[Path] = []

    def fake_popen(args, **kwargs):
        temporary = Path(args[-1])
        temporary_paths.append(temporary)
        temporary.write_bytes(b"saida-parcial")
        kwargs["stderr"].write(b"x" * 5000 + b"falha final do encoder")
        return _FinishedProcess(1)

    monkeypatch.setattr(tasks.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tasks, "_compatibility_plan", lambda *_args: "transcode")

    with pytest.raises(RuntimeError, match="falha final do encoder") as exc_info:
        tasks._transcode_compatible_mp4("ffmpeg", source, lambda: False)

    assert len(str(exc_info.value)) <= tasks._COMPATIBILITY_STDERR_TAIL_BYTES + 100
    assert source.read_bytes() == b"fonte-original"
    assert temporary_paths and not temporary_paths[0].exists()


def test_falha_da_compatibilidade_mantem_download_original(
    tmp_path, monkeypatch, caplog,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fonte-original")

    def fail(*_args, **_kwargs):
        raise RuntimeError("FFmpeg encerrou com código -9.")

    monkeypatch.setattr(tasks, "_transcode_compatible_mp4", fail)

    with caplog.at_level(logging.INFO, logger="backend.workers.tasks"):
        result = tasks._compatibilize_mp4_or_keep_original(
            "3e60cba3-0000-0000-0000-000000000000",
            "ffmpeg",
            source,
            lambda: False,
        )

    assert result == source
    assert result.read_bytes() == b"fonte-original"
    assert [record.getMessage() for record in caplog.records] == [
        "Download 3e60cba3: compatibilidade não aplicada; arquivo original mantido.",
    ]


def test_limite_de_512_mb_nunca_inicia_ffmpeg_e_mantem_original(
    tmp_path, monkeypatch, caplog,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fonte-original")
    monkeypatch.setattr(tasks, "_compatibility_plan", lambda *_args: "transcode")
    monkeypatch.setattr(
        observability,
        "container_memory_snapshot",
        lambda: (512 * 1024 * 1024, 200 * 1024 * 1024),
    )

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("FFmpeg não deveria iniciar com 512 MB")

    monkeypatch.setattr(tasks, "_run_compatibility_attempt", must_not_run)

    with caplog.at_level(logging.INFO, logger="backend.workers.tasks"):
        result = tasks._compatibilize_mp4_or_keep_original(
            "3e60cba3-0000-0000-0000-000000000000",
            "ffmpeg",
            source,
            lambda: False,
        )

    assert result == source
    assert result.read_bytes() == b"fonte-original"
    assert not list(tmp_path.glob("*.transcoding.mp4"))
    assert [record.getMessage() for record in caplog.records] == [
        "Download 3e60cba3: conversão ignorada para proteger a RAM "
        "(312 MB disponíveis de 512 MB).",
    ]


def test_remux_leve_continua_permitido_com_512_mb(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fonte-original")
    attempts = []
    monkeypatch.setattr(tasks, "_compatibility_plan", lambda *_args: "copy")
    monkeypatch.setattr(
        observability,
        "container_memory_snapshot",
        lambda: (512 * 1024 * 1024, 300 * 1024 * 1024),
    )

    def fake_attempt(_ffmpeg, _source, temporary, plan, _cancelled):
        attempts.append(plan)
        temporary.write_bytes(b"remux")

    monkeypatch.setattr(tasks, "_run_compatibility_attempt", fake_attempt)

    result = tasks._transcode_compatible_mp4(
        "ffmpeg", source, lambda: False,
    )

    assert result == source
    assert result.read_bytes() == b"remux"
    assert attempts == ["copy"]


def test_cancelamento_da_compatibilidade_nunca_entrega_original(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fonte-original")

    def cancel(*_args, **_kwargs):
        raise tasks.TaskProcessingCancelled()

    monkeypatch.setattr(tasks, "_transcode_compatible_mp4", cancel)

    with pytest.raises(tasks.TaskProcessingCancelled):
        tasks._compatibilize_mp4_or_keep_original(
            "3e60cba3-0000-0000-0000-000000000000",
            "ffmpeg",
            source,
            lambda: True,
        )


def test_saida_vazia_e_rejeitada_e_removida(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fonte-original")
    temporary_paths: list[Path] = []

    def fake_popen(args, **_kwargs):
        temporary = Path(args[-1])
        temporary_paths.append(temporary)
        temporary.touch()
        return _FinishedProcess(0)

    monkeypatch.setattr(tasks.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tasks, "_compatibility_plan", lambda *_args: "transcode")

    with pytest.raises(RuntimeError, match="arquivo MP4 vazio"):
        tasks._transcode_compatible_mp4("ffmpeg", source, lambda: False)

    assert source.read_bytes() == b"fonte-original"
    assert temporary_paths and not temporary_paths[0].exists()


def test_cancelamento_interrompe_processo_e_remove_parcial(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fonte-original")
    checks = 0
    stopped = []
    temporary_paths: list[Path] = []

    class _RunningProcess:
        stopped = False

        def poll(self):
            return -15 if self.stopped else None

    process = _RunningProcess()

    def fake_popen(args, **_kwargs):
        temporary = Path(args[-1])
        temporary_paths.append(temporary)
        temporary.write_bytes(b"saida-parcial")
        return process

    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 3

    def fake_stop(received):
        stopped.append(received)
        received.stopped = True

    monkeypatch.setattr(tasks.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tasks, "_stop_ffmpeg_process", fake_stop)
    monkeypatch.setattr(tasks, "_compatibility_plan", lambda *_args: "transcode")

    with pytest.raises(tasks.TaskProcessingCancelled):
        tasks._transcode_compatible_mp4("ffmpeg", source, cancelled)

    assert stopped == [process]
    assert source.read_bytes() == b"fonte-original"
    assert temporary_paths and not temporary_paths[0].exists()
