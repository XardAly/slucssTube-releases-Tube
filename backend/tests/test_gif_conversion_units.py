"""Validação de parâmetros e montagem segura dos filtros de GIF."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.downloads.schemas import GifOptions
from backend.workers.gif_converter import (
    ConversionStats,
    GifConversionCancelled,
    build_ffmpeg_args,
    build_filter_complex,
    build_palette_args,
    convert_within_limit,
    run_gif_conversion,
)


def test_rejeita_trecho_maior_que_trinta_segundos():
    with pytest.raises(ValidationError):
        GifOptions(start=0, end=31)


def test_rejeita_final_anterior_ao_inicio():
    with pytest.raises(ValidationError):
        GifOptions(start=10, end=5)


def test_filtro_preserva_proporcao_e_usa_paleta(tmp_path):
    options = GifOptions(resolution="720p", fps=15, colors=256)
    filters = build_filter_complex(options)
    assert "scale=-2:720:flags=lanczos" in filters
    # Paleta em duas passadas: split+palettegen em passada única bufferiza
    # todos os frames na RAM e estoura o container com vídeos maiores.
    palette_args = build_palette_args(
        "ffmpeg", tmp_path / "s.mp4", tmp_path / "p.png", options)
    gif_args = build_ffmpeg_args(
        "ffmpeg", tmp_path / "s.mp4", tmp_path / "p.png", tmp_path / "o.gif", options)
    assert "palettegen=stats_mode=diff:max_colors=256" in " ".join(palette_args)
    assert "paletteuse=dither=sierra2_4a:diff_mode=rectangle" in " ".join(gif_args)
    assert "split" not in " ".join(gif_args)
    assert str(tmp_path / "p.png") in gif_args


def test_filtros_desativados_nao_sao_incluidos():
    options = GifOptions(
        enhancement="off", denoise="off", sharpen=0,
        brightness=0, contrast=0, saturation=0, gamma=1.0,
    )
    filters = build_filter_complex(options)
    assert "hqdn3d" not in filters
    assert "unsharp" not in filters
    assert "eq=" not in filters


def test_argumentos_nao_usam_shell_nem_texto_livre(tmp_path):
    source = tmp_path / "source.mp4"
    palette = tmp_path / "palette.png"
    output = tmp_path / "result.gif"
    args = build_ffmpeg_args("ffmpeg", source, palette, output, GifOptions())
    assert isinstance(args, list)
    assert args[0] == "ffmpeg"
    assert str(source) in args
    assert str(output) in args
    assert "; rm " not in " ".join(args)


def test_argumentos_limitam_threads_e_descartam_fluxos_extras(tmp_path):
    args = build_ffmpeg_args(
        "ffmpeg", tmp_path / "source.mp4", tmp_path / "palette.png",
        tmp_path / "result.gif", GifOptions(), threads=2, filter_threads=2,
    )
    assert ["-threads", "2"] == args[args.index("-threads"):args.index("-threads") + 2]
    assert "-filter_complex_threads" in args
    assert "-an" in args and "-sn" in args and "-dn" in args
    assert args[args.index("-map") + 1] == "[out]"


@pytest.mark.parametrize("failure_call", [1, 2])
def test_falha_em_qualquer_passada_remove_paleta_e_saida(
    monkeypatch, tmp_path, failure_call,
):
    calls = 0
    output = tmp_path / "result.gif"
    palette = tmp_path / "result_palette.png"

    def fake_run(_args, _cancelled, _deadline, on_out_time, on_idle=None):  # noqa: ARG001
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise RuntimeError("falha ffmpeg")
        if calls == 1:
            palette.write_bytes(b"palette")
        else:
            output.write_bytes(b"GIF89a")

    monkeypatch.setattr("backend.workers.gif_converter._run_ffmpeg", fake_run)
    with pytest.raises(RuntimeError, match="falha ffmpeg"):
        run_gif_conversion(
            "ffmpeg", tmp_path / "source.mp4", output, GifOptions(),
            lambda _value: None, lambda: False,
        )
    assert not palette.exists()
    assert not output.exists()


def test_timeout_remove_paleta_e_saida(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "backend.workers.gif_converter._run_ffmpeg",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("timeout")),
    )
    output = tmp_path / "result.gif"
    with pytest.raises(TimeoutError):
        run_gif_conversion(
            "ffmpeg", tmp_path / "source.mp4", output, GifOptions(),
            lambda _value: None, lambda: False,
        )
    assert not output.exists()
    assert not (tmp_path / "result_palette.png").exists()


def _fake_encoder(monkeypatch, sizes: list[int]):
    calls: list[GifOptions] = []

    def run(_ffmpeg, _source, output, options, _progress, cancelled, *_args):
        if cancelled():
            raise GifConversionCancelled()
        size = sizes[min(len(calls), len(sizes) - 1)]
        with output.open("wb") as stream:
            stream.truncate(size)
        calls.append(options)
        return ConversionStats(encodings=1, ffmpeg_processes=2)

    monkeypatch.setattr("backend.workers.gif_converter.run_gif_conversion", run)
    return calls


def test_sem_limite_preserva_primeira_configuracao(monkeypatch, tmp_path):
    calls = _fake_encoder(monkeypatch, [150 * 1024 * 1024])
    options = GifOptions(max_size_mb=None, fps=24, resolution="720p")
    output, final, stats = convert_within_limit(
        "ffmpeg", tmp_path / "source.mp4", tmp_path, options,
        1280, 720, lambda *_args: None, lambda: False,
    )
    assert output.stat().st_size == 150 * 1024 * 1024
    assert final == options
    assert len(calls) == stats.encodings == 1


@pytest.mark.parametrize("sizes,expected", [
    ([500_000], 1),
    ([1_100_000, 900_000], 2),
    ([2_100_000, 900_000], 2),
    ([8_000_000, 2_000_000, 900_000], 3),
])
def test_reducao_adaptativa_limita_codificacoes(monkeypatch, tmp_path, sizes, expected):
    calls = _fake_encoder(monkeypatch, sizes)
    output, _final, stats = convert_within_limit(
        "ffmpeg", tmp_path / "source.mp4", tmp_path,
        GifOptions(max_size_mb=1, resolution="720p", fps=24, colors=256),
        1280, 720, lambda *_args: None, lambda: False,
    )
    assert output.stat().st_size <= 1024 * 1024
    assert len(calls) == stats.encodings == expected
    assert stats.encodings <= 3


def test_limite_impossivel_remove_resultado_rejeitado(monkeypatch, tmp_path):
    calls = _fake_encoder(monkeypatch, [8_000_000])
    with pytest.raises(RuntimeError, match="tamanho máximo"):
        convert_within_limit(
            "ffmpeg", tmp_path / "source.mp4", tmp_path,
            GifOptions(max_size_mb=1, resolution="360p", fps=5, colors=32),
            640, 360, lambda *_args: None, lambda: False,
        )
    assert len(calls) <= 3
    assert not (tmp_path / "result.gif").exists()


def test_cancelamento_interrompe_antes_de_codificar(tmp_path):
    with pytest.raises(GifConversionCancelled):
        convert_within_limit(
            "ffmpeg", tmp_path / "source.mp4", tmp_path, GifOptions(),
            640, 360, lambda *_args: None, lambda: True,
        )
