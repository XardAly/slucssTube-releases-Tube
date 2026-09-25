"""
Auto-setup do FFmpeg para o worker.

Na primeira execução, detecta o sistema operacional e, se o ffmpeg
não estiver no PATH, baixa um build estático para uma pasta local.
Funciona em Windows (dev) e Linux (host/Docker).

O binário só é baixado UMA VEZ; depois fica cacheado em disco.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

from backend.api.config import get_settings
from backend.workers.binary_install import install_lock, verified_archive

log = logging.getLogger(__name__)

# Pasta onde o ffmpeg portátil é instalado (dentro do projeto)
_FFMPEG_DIR = Path(__file__).resolve().parent.parent / ".devdownloads" / "ffmpeg"

# Build imutável do fork mantido pelo yt-dlp, fixado no commit a09be9b91e.
_FFMPEG_VERSION_MARKER = "N-125551-ga09be9b91e"
_FFMPEG_RELEASE = "autobuild-2026-07-12-15-07"
_WINDOWS_ARCHIVE = "ffmpeg-N-125551-ga09be9b91e-win64-gpl.zip"
_LINUX_ARCHIVE = "ffmpeg-N-125551-ga09be9b91e-linux64-gpl.tar.xz"
_RELEASE_BASE = f"https://github.com/yt-dlp/FFmpeg-Builds/releases/download/{_FFMPEG_RELEASE}"
_WINDOWS_URL = f"{_RELEASE_BASE}/{_WINDOWS_ARCHIVE}"
_LINUX_URL = f"{_RELEASE_BASE}/{_LINUX_ARCHIVE}"
_DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com"}

_cached_location: str | None = None
_MAX_ARCHIVE_BYTES = 300 * 1024 * 1024


def _exe(name: str) -> str:
    """Nome do executável ajustado ao OS."""
    return f"{name}.exe" if sys.platform == "win32" else name


def _is_valid(path: Path) -> bool:
    """Verifica se o binário existe e é executável."""
    return path.is_file() and os.access(path, os.X_OK)


def _supports_compatibility_codecs(path: Path) -> bool:
    """Confirma os encoders exigidos pela saída MP4 para o After Effects."""
    if not _is_valid(path):
        return False
    try:
        result = subprocess.run(
            [str(path), "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = f"{result.stdout or ''}\n{result.stderr or ''}"
    return bool(
        result.returncode == 0
        and re.search(r"(?m)^\s*[A-Z.]{6}\s+libx264\s", output)
        and re.search(r"(?m)^\s*[A-Z.]{6}\s+aac\s", output)
    )


def _safe_archive_name(name: str) -> bool:
    normalized = name.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    return bool(parts) and not normalized.startswith("/") and ".." not in parts


def _verify_version(path: Path) -> None:
    result = subprocess.run(
        [str(path), "-version"], capture_output=True, text=True, timeout=15,
    )
    first_line = (result.stdout or "").splitlines()[:1]
    if result.returncode != 0 or not first_line or _FFMPEG_VERSION_MARKER not in first_line[0]:
        raise RuntimeError("Versão extraída do FFmpeg não corresponde ao release fixado.")


def _download_windows() -> Path:
    """Baixa o ffmpeg essentials para Windows (.zip)."""
    log.info("Baixando FFmpeg para Windows...")
    _FFMPEG_DIR.parent.mkdir(parents=True, exist_ok=True)
    with verified_archive(
        _WINDOWS_URL,
        get_settings().ffmpeg_windows_archive_sha256,
        max_bytes=_MAX_ARCHIVE_BYTES,
        allowed_hosts=_DOWNLOAD_HOSTS,
        prefix="xard-ffmpeg-",
    ) as archive, tempfile.TemporaryDirectory(
        prefix="ffmpeg-install-", dir=_FFMPEG_DIR.parent,
    ) as staging_name:
        staging = Path(staging_name)
        with zipfile.ZipFile(archive) as zf:
            entries = zf.infolist()
            if any(
                not _safe_archive_name(item.filename)
                or stat.S_ISLNK(item.external_attr >> 16)
                for item in entries
            ):
                raise RuntimeError("Pacote FFmpeg contém caminho ou symlink inseguro.")
            candidates = [
                item for item in entries
                if os.path.basename(item.filename) in {"ffmpeg.exe", "ffprobe.exe"}
            ]
            if sorted(os.path.basename(item.filename) for item in candidates) != ["ffmpeg.exe", "ffprobe.exe"]:
                raise RuntimeError("Pacote FFmpeg contém executáveis ausentes ou duplicados.")
            for item in candidates:
                base = os.path.basename(item.filename)
                if item.file_size > 200 * 1024 * 1024:
                    raise RuntimeError("Executável FFmpeg excede o limite permitido.")
                with zf.open(item) as src, (staging / base).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        _verify_version(staging / "ffmpeg.exe")
        _FFMPEG_DIR.mkdir(parents=True, exist_ok=True)
        for base in ("ffmpeg.exe", "ffprobe.exe"):
            os.replace(staging / base, _FFMPEG_DIR / base)

    return _FFMPEG_DIR


def _download_linux() -> Path:
    """Baixa o ffmpeg static para Linux (.tar.xz)."""
    log.info("Baixando FFmpeg para Linux...")
    _FFMPEG_DIR.parent.mkdir(parents=True, exist_ok=True)
    with verified_archive(
        _LINUX_URL,
        get_settings().ffmpeg_linux_archive_sha256,
        max_bytes=_MAX_ARCHIVE_BYTES,
        allowed_hosts=_DOWNLOAD_HOSTS,
        prefix="xard-ffmpeg-",
    ) as archive, tempfile.TemporaryDirectory(
        prefix="ffmpeg-install-", dir=_FFMPEG_DIR.parent,
    ) as staging_name:
        staging = Path(staging_name)
        with tarfile.open(archive, mode="r:xz") as tf:
            members = tf.getmembers()
            if any(
                not _safe_archive_name(member.name)
                or member.issym() or member.islnk()
                for member in members
            ):
                raise RuntimeError("Pacote FFmpeg contém caminho ou link inseguro.")
            candidates = [
                member for member in members
                if os.path.basename(member.name) in {"ffmpeg", "ffprobe"} and member.isfile()
            ]
            if sorted(os.path.basename(member.name) for member in candidates) != ["ffmpeg", "ffprobe"]:
                raise RuntimeError("Pacote FFmpeg contém executáveis ausentes ou duplicados.")
            for member in candidates:
                base = os.path.basename(member.name)
                if member.size > 200 * 1024 * 1024:
                    raise RuntimeError("Executável FFmpeg excede o limite permitido.")
                source = tf.extractfile(member)
                if source is None:
                    raise RuntimeError("Executável FFmpeg não pôde ser extraído.")
                target = staging / base
                with source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                target.chmod(target.stat().st_mode | stat.S_IEXEC)
        _verify_version(staging / "ffmpeg")
        _FFMPEG_DIR.mkdir(parents=True, exist_ok=True)
        for base in ("ffmpeg", "ffprobe"):
            os.replace(staging / base, _FFMPEG_DIR / base)

    return _FFMPEG_DIR


def ensure_ffmpeg(*, require_compatibility_codecs: bool = False) -> str | None:
    """
    Garante que o ffmpeg está disponível. Retorna o caminho da PASTA
    contendo o binário (formato aceito por yt-dlp ``ffmpeg_location``).

    Ordem de busca:
      1. Cache em memória (já resolvido nesta sessão)
      2. Binário portátil já baixado em .devdownloads/ffmpeg/
      3. ffmpeg no PATH do sistema
      4. Download automático do build estático

    Retorna ``None`` apenas se o download falhar (o worker continua
    funcionando para downloads que não precisam de merge).
    """
    global _cached_location  # noqa: PLW0603
    if _cached_location:
        cached_exe = Path(_cached_location) / _exe("ffmpeg")
        if (
            _is_valid(cached_exe)
            and (
                not require_compatibility_codecs
                or _supports_compatibility_codecs(cached_exe)
            )
        ):
            return _cached_location
        _cached_location = None

    # 1. Já temos o binário portátil baixado?
    local_exe = _FFMPEG_DIR / _exe("ffmpeg")
    local_probe = _FFMPEG_DIR / _exe("ffprobe")
    if (
        _is_valid(local_exe)
        and _is_valid(local_probe)
        and (
            not require_compatibility_codecs
            or _supports_compatibility_codecs(local_exe)
        )
    ):
        _cached_location = str(_FFMPEG_DIR)
        log.info("FFmpeg encontrado (portátil): %s", _cached_location)
        return _cached_location

    # 2. Está no PATH do sistema? (Docker, host com apt install, etc.)
    system = shutil.which("ffmpeg")
    system_is_compatible = bool(
        system
        and (
            not require_compatibility_codecs
            or _supports_compatibility_codecs(Path(system))
        )
    )
    if system and system_is_compatible:
        _cached_location = str(Path(system).parent)
        log.info("FFmpeg encontrado (sistema): %s", _cached_location)
        return _cached_location

    # 3. Não encontrado — baixar automaticamente, se explicitamente habilitado.
    if not get_settings().worker_binary_downloads_enabled:
        if system and require_compatibility_codecs:
            raise RuntimeError(
                "O FFmpeg da imagem não oferece os encoders libx264 e AAC."
            )
        raise RuntimeError("FFmpeg não está instalado na imagem do worker.")
    try:
        with install_lock(_FFMPEG_DIR.parent / ".ffmpeg-install.lock"):
            if (
                _is_valid(local_exe)
                and _is_valid(local_probe)
                and (
                    not require_compatibility_codecs
                    or _supports_compatibility_codecs(local_exe)
                )
            ):
                folder = _FFMPEG_DIR
            elif sys.platform == "win32":
                folder = _download_windows()
            else:
                folder = _download_linux()
        installed_exe = Path(folder) / _exe("ffmpeg")
        if (
            require_compatibility_codecs
            and not _supports_compatibility_codecs(installed_exe)
        ):
            raise RuntimeError(
                "O FFmpeg instalado não oferece os encoders libx264 e AAC."
            )
        _cached_location = str(folder)
        log.info("FFmpeg instalado com sucesso em: %s", _cached_location)
        return _cached_location
    except Exception:
        log.exception("Falha ao baixar o FFmpeg automaticamente.")
        raise
