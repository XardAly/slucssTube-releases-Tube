"""Montagem e execução segura do pipeline FFmpeg para GIF."""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from backend.downloads.schemas import GifOptions


class GifConversionCancelled(Exception):
    pass


logger = logging.getLogger(__name__)


@dataclass
class ConversionStats:
    encodings: int = 0
    ffmpeg_processes: int = 0
    palette_seconds: float = 0.0
    gif_seconds: float = 0.0
    cleanup_seconds: float = 0.0
    retry_reasons: list[str] = field(default_factory=list)


_DENOISE = {
    "light": "hqdn3d=1:1:2:2",
    "medium": "hqdn3d=2:1.5:3:2.5",
    "strong": "hqdn3d=3:2.5:5:4",
}
_DITHER = {
    "auto": "sierra2_4a",
    "none": "none",
    "bayer": "bayer:bayer_scale=3",
    "sierra": "sierra2_4a",
    "floyd_steinberg": "floyd_steinberg",
}
_AUTO = {
    "off": (0, 0, 0, "off"),
    "light": (12, 8, 8, "light"),
    "medium": (25, 14, 12, "medium"),
    "strong": (38, 20, 16, "strong"),
}


def build_filter_complex(options: GifOptions) -> str:
    """Gera apenas filtros derivados de valores previamente validados."""
    # Ordem: velocidade -> ruído -> equalização -> escala -> nitidez -> FPS.
    filters: list[str] = [f"setpts=PTS/{options.speed:g}"]
    auto_sharpen, auto_contrast, auto_saturation, auto_denoise = _AUTO[options.enhancement]
    denoise = options.denoise if options.denoise != "off" else auto_denoise
    if denoise != "off":
        filters.append(_DENOISE[denoise])

    brightness = options.brightness / 1000
    contrast = 1 + ((options.contrast + auto_contrast) / 250)
    saturation = 1 + ((options.saturation + auto_saturation) / 200)
    if (options.brightness or options.contrast or options.saturation
            or options.gamma != 1.0 or options.enhancement != "off"):
        filters.append(
            f"eq=brightness={brightness:.3f}:contrast={contrast:.3f}:"
            f"saturation={saturation:.3f}:gamma={options.gamma:.3f}"
        )

    scale_flags = "lanczos" if options.antialias else "bicubic"
    if options.resolution == "custom":
        width, height = options.custom_width, options.custom_height
        filters.extend((
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags={scale_flags}",
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
        ))
    elif options.resolution != "original":
        height = int(options.resolution.removesuffix("p"))
        filters.append(f"scale=-2:{height}:flags={scale_flags}")

    sharpen = max(options.sharpen, auto_sharpen)
    if sharpen:
        amount = min(sharpen / 100, 0.8)
        filters.append(f"unsharp=5:5:{amount:.2f}:5:5:0")

    filters.append(f"fps={options.fps}")
    return ",".join(filters)


def build_palette_args(
    ffmpeg: str,
    source: Path,
    palette: Path,
    options: GifOptions,
    threads: int = 1,
    filter_threads: int = 1,
) -> list[str]:
    """Passada 1: gera somente a paleta em arquivo (memória constante)."""
    duration = options.end - options.start
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{options.start:.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(source),
        "-map", "0:v:0",
        "-an", "-sn", "-dn",
        "-threads", str(threads),
        "-filter_threads", str(filter_threads),
        "-vf", (
            f"{build_filter_complex(options)},"
            f"palettegen=stats_mode=diff:max_colors={options.colors}"
        ),
        "-progress", "pipe:1",
        "-nostats",
        "-y", str(palette),
    ]


def build_ffmpeg_args(
    ffmpeg: str,
    source: Path,
    palette: Path,
    output: Path,
    options: GifOptions,
    threads: int = 1,
    filter_threads: int = 1,
) -> list[str]:
    """
    Passada 2: aplica a paleta pronta. Em passada única (split+palettegen)
    o FFmpeg bufferiza TODOS os frames decodificados até a paleta ficar
    pronta — em 720p isso passa de 2 GB e derruba o container.
    """
    duration = options.end - options.start
    loop = 0 if options.loop == "infinite" else -1 if options.loop == "once" else options.loop_count
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{options.start:.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(source),
        "-i", str(palette),
        "-an", "-sn", "-dn",
        "-threads", str(threads),
        "-filter_complex_threads", str(filter_threads),
        "-filter_complex",
        f"[0:v]{build_filter_complex(options)}[gif];"
        f"[gif][1:v]paletteuse=dither={_DITHER[options.dither]}:diff_mode=rectangle[out]",
        "-map", "[out]",
        "-loop", str(loop),
        "-gifflags", "+transdiff",
        "-progress", "pipe:1",
        "-nostats",
        "-y", str(output),
    ]


def probe_video(ffprobe: str, source: Path) -> dict:
    result = subprocess.run(
        [
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate:format=duration",
            "-of", "json", str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
    )
    data = json.loads(result.stdout)
    stream = (data.get("streams") or [{}])[0]
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "duration": float((data.get("format") or {}).get("duration") or 0),
    }


def run_gif_conversion(
    ffmpeg: str,
    source: Path,
    output: Path,
    options: GifOptions,
    progress: Callable[[float], None],
    cancelled: Callable[[], bool],
    threads: int = 1,
    filter_threads: int = 1,
    timeout_seconds: int = 600,
) -> ConversionStats:
    """Duas passadas (paleta em arquivo -> GIF): decodifica o vídeo duas
    vezes, porém com memória constante — a passada única bufferiza todos os
    frames na RAM e estoura o container com vídeos maiores."""
    duration = max((options.end - options.start) / options.speed, 0.1)
    deadline = time.monotonic() + timeout_seconds
    palette = output.with_name(f"{output.stem}_palette.png")
    stats = ConversionStats(encodings=1)
    try:
        # Passada 1: palettegen só emite o out_time num único frame no fim
        # (a saída dela é 1 imagem, não um vídeo), então não dá pra medir
        # progresso real. Sem isso a barra ficava parada o tempo todo dessa
        # passada; aqui ela avança sozinha por tempo decorrido, desacelerando
        # perto do teto pra nunca "terminar" antes da hora.
        started = time.monotonic()
        _run_ffmpeg(
            build_palette_args(
                ffmpeg, source, palette, options, threads, filter_threads,
            ),
            cancelled, deadline, on_out_time=lambda _elapsed: None,
            on_idle=lambda elapsed: progress(38.0 * (1 - math.exp(-elapsed / 4.0))),
        )
        stats.palette_seconds = time.monotonic() - started
        stats.ffmpeg_processes += 1
        if not palette.is_file():
            raise RuntimeError("Não foi possível gerar o GIF com as opções escolhidas.")
        progress(40.0)
        started = time.monotonic()
        _run_ffmpeg(
            build_ffmpeg_args(
                ffmpeg, source, palette, output, options, threads, filter_threads,
            ),
            cancelled, deadline,
            on_out_time=lambda elapsed: progress(
                min(40.0 + elapsed * 59.0 / duration, 99.0)
            ),
        )
        stats.gif_seconds = time.monotonic() - started
        stats.ffmpeg_processes += 1
        if not output.is_file():
            raise RuntimeError("Não foi possível gerar o GIF com as opções escolhidas.")
        progress(100.0)
        return stats
    except Exception:
        output.unlink(missing_ok=True)
        raise
    finally:
        cleanup_started = time.monotonic()
        palette.unlink(missing_ok=True)
        stats.cleanup_seconds += time.monotonic() - cleanup_started


def _run_ffmpeg(
    args: list[str],
    cancelled: Callable[[], bool],
    deadline: float,
    on_out_time: Callable[[float], None],
    on_idle: Callable[[float], None] | None = None,
) -> None:
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        start_new_session=True,
    )
    lines: queue.Queue[str | None] = queue.Queue(maxsize=100)

    def drain_stdout() -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                try:
                    lines.put_nowait(line)
                except queue.Full:
                    # Progresso é transitório; manter o pipe drenado evita deadlock.
                    pass
        finally:
            while True:
                try:
                    lines.put_nowait(None)
                    break
                except queue.Full:
                    try:
                        lines.get_nowait()
                    except queue.Empty:
                        pass

    reader = threading.Thread(target=drain_stdout, name="ffmpeg-progress", daemon=True)
    reader.start()
    started = time.monotonic()
    try:
        while True:
            if cancelled():
                raise GifConversionCancelled()
            if time.monotonic() > deadline:
                raise TimeoutError("A conversão excedeu o tempo permitido.")

            try:
                line = lines.get(timeout=0.25)
            except queue.Empty:
                if process.poll() is not None and not reader.is_alive():
                    break
                if on_idle is not None:
                    on_idle(time.monotonic() - started)
                continue
            if line is None:
                break
            key, _, value = line.strip().partition("=")
            if key in {"out_time_ms", "out_time_us"}:
                try:
                    on_out_time(float(value) / 1_000_000)
                except ValueError:
                    pass

        if process.wait(timeout=5) != 0:
            raise RuntimeError("Não foi possível gerar o GIF com as opções escolhidas.")
    except Exception:
        _stop_process(process)
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
        reader.join(timeout=2)
        if reader.is_alive():
            logger.warning("ffmpeg_progress_reader_did_not_stop pid=%s", process.pid)


def _stop_process(process: subprocess.Popen) -> None:
    """Encerra o grupo cooperativamente e força após o prazo."""
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        logger.error("ffmpeg_process_could_not_be_stopped pid=%s", process.pid)


def reduce_options(options: GifOptions, attempt: int) -> GifOptions:
    """Reduz custo/tamanho gradualmente sem ultrapassar listas permitidas."""
    values = options.model_dump()
    if attempt >= 1:
        values["fps"] = max((fps for fps in (5, 10, 15, 20, 24, 30) if fps < options.fps), default=5)
    if attempt >= 2:
        heights = {"original": "480p", "720p": "480p", "480p": "360p", "360p": "360p"}
        values["resolution"] = heights.get(options.resolution, "360p")
    if attempt >= 3:
        values["colors"] = min(options.colors, 128)
    return GifOptions.model_validate(values)


def _effective_pixels(options: GifOptions, src_width: int, src_height: int) -> int:
    if options.resolution == "custom":
        return options.custom_width * options.custom_height
    if options.resolution == "original":
        return max(src_width * src_height, 160 * 120)
    height = int(options.resolution.removesuffix("p"))
    ratio = src_width / src_height if src_height else 16 / 9
    return int(height * ratio) * height


def _predicted_bytes(measured: int, base: GifOptions, candidate: GifOptions,
                     src_width: int, src_height: int) -> int:
    """Extrapola o tamanho a partir de uma conversão já medida (linear em
    pixels e FPS; cores pesam menos que o proporcional)."""
    pixels = _effective_pixels(candidate, src_width, src_height) / _effective_pixels(base, src_width, src_height)
    fps = candidate.fps / base.fps
    colors = (candidate.colors / base.colors) ** 0.5
    return int(measured * pixels * fps * colors)


_FPS_VALUES = (5, 10, 15, 20, 24, 30)
_COLOR_VALUES = (32, 64, 128, 256)


def _floor_allowed(value: float, allowed: tuple[int, ...], minimum: int, maximum: int) -> int:
    candidates = [item for item in allowed if minimum <= item <= maximum]
    if not candidates:
        return maximum
    return max((item for item in candidates if item <= value), default=min(candidates))


def _adaptive_options(
    options: GifOptions,
    measured: int,
    max_bytes: int,
    src_width: int,
    src_height: int,
    safety_margin: float,
    min_fps: int,
    min_colors: int,
    min_width: int,
    min_height: int,
) -> tuple[GifOptions | None, str]:
    """Calcula a tentativa seguinte a partir do tamanho realmente produzido.

    FPS só é reduzido como último recurso: resolução e cores são
    empurradas até o mínimo permitido primeiro, porque cortar fps é o
    que mais prejudica a suavidade percebida do GIF.
    """
    ratio = max_bytes / max(measured, 1)
    target = min(ratio * safety_margin, 0.95)
    if target >= 1:
        return None, "resultado já está dentro do limite"

    values = options.model_dump()
    values["colors"] = _floor_allowed(
        options.colors * max(target, 0.01) ** 0.15,
        _COLOR_VALUES, min_colors, options.colors,
    )
    color_ratio = (values["colors"] / options.colors) ** 0.5
    pixel_ratio = min(target / max(color_ratio, 0.01), 0.90)
    scale = max(0.10, min(pixel_ratio ** 0.5, 0.95))

    if options.resolution == "custom":
        current_width, current_height = options.custom_width, options.custom_height
    elif options.resolution == "original":
        current_width, current_height = src_width, src_height
    else:
        current_height = int(options.resolution.removesuffix("p"))
        aspect = src_width / src_height if src_height else 16 / 9
        current_width = max(2, round((current_height * aspect) / 2) * 2)

    width = max(min_width, min(current_width, int(current_width * scale)))
    height = max(min_height, min(current_height, int(current_height * scale)))
    width -= width % 2
    height -= height % 2

    # Resolução e cores bateram no piso e ainda falta reduzir: só agora
    # o fps entra na conta, pelo tanto que ainda falta pra caber.
    values["fps"] = options.fps
    hit_floor = width <= min_width or height <= min_height
    if hit_floor:
        achieved_pixel_ratio = (width * height) / max(current_width * current_height, 1)
        remaining = target / max(achieved_pixel_ratio * color_ratio, 0.01)
        if remaining < 1:
            values["fps"] = _floor_allowed(
                options.fps * max(remaining, 0.01),
                _FPS_VALUES, min_fps, options.fps,
            )

    values.update(
        resolution="custom",
        custom_width=max(160, width),
        custom_height=max(120, height),
    )
    fitted = GifOptions.model_validate(values)
    if fitted == options:
        return None, "configuração mínima atingida"
    reason = (
        f"tamanho={measured} alvo={max_bytes} razão={ratio:.3f} "
        f"escala={scale:.3f} fps={options.fps}->{fitted.fps} "
        f"cores={options.colors}->{fitted.colors} "
        f"resolução={current_width}x{current_height}->"
        f"{fitted.custom_width}x{fitted.custom_height}"
    )
    return fitted, reason


def _accumulate_stats(total: ConversionStats, current: ConversionStats) -> None:
    total.encodings += current.encodings
    total.ffmpeg_processes += current.ffmpeg_processes
    total.palette_seconds += current.palette_seconds
    total.gif_seconds += current.gif_seconds
    total.cleanup_seconds += current.cleanup_seconds


def convert_within_limit(
    ffmpeg: str,
    source: Path,
    temp_dir: Path,
    options: GifOptions,
    src_width: int,
    src_height: int,
    progress: Callable[[int, float, bool], None],
    cancelled: Callable[[], bool],
    *,
    threads: int = 1,
    filter_threads: int = 1,
    timeout_seconds: int = 600,
    max_encodings: int = 3,
    safety_margin: float = 0.88,
    min_fps: int = 5,
    min_colors: int = 32,
    min_width: int = 160,
    min_height: int = 120,
) -> tuple[Path, GifOptions, ConversionStats]:
    """
    Converte respeitando max_size_mb sem desperdiçar passadas: mede a
    primeira conversão e PULA os degraus que a extrapolação mostra que não
    caberiam; se a escada não bastar, fecha com resolução calculada.
    Levanta RuntimeError quando o limite é inatingível.

    `progress` recebe (attempt, percent, fps_reduced) — fps_reduced indica
    se a tentativa atual já precisou cortar fps (último recurso), pra o
    cliente mostrar exatamente o que está sendo otimizado.
    """
    max_bytes = options.max_size_mb * 1024 * 1024 if options.max_size_mb else None
    current_options = options
    fps_reduced = False
    stats = ConversionStats()
    candidate = temp_dir / "result.gif"

    for attempt in range(max_encodings if max_bytes else 1):
        if cancelled():
            raise GifConversionCancelled()
        candidate.unlink(missing_ok=True)
        progress(attempt, 0.0, fps_reduced)
        current_stats = run_gif_conversion(
            ffmpeg, source, candidate, current_options,
            lambda value, a=attempt, r=fps_reduced: progress(a, value, r), cancelled,
            threads, filter_threads, timeout_seconds,
        )
        _accumulate_stats(stats, current_stats)
        if cancelled():
            candidate.unlink(missing_ok=True)
            raise GifConversionCancelled()
        size = candidate.stat().st_size
        if max_bytes is None or size <= max_bytes:
            return candidate, current_options, stats
        candidate.unlink(missing_ok=True)
        next_options, reason = _adaptive_options(
            current_options, size, max_bytes, src_width, src_height,
            safety_margin, min_fps, min_colors, min_width, min_height,
        )
        if next_options is None:
            break
        stats.retry_reasons.append(reason)
        logger.info("gif_retry_adaptive attempt=%s %s", attempt + 2, reason)
        current_options = next_options
        fps_reduced = current_options.fps < options.fps

    raise RuntimeError(
        "Não foi possível atingir o tamanho máximo escolhido sem perder qualidade demais."
    )
