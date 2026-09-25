"""Auto-setup do Deno para o yt-dlp resolver desafios JS do YouTube."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from backend.api.config import get_settings
from backend.workers.binary_install import install_lock, verified_archive

log = logging.getLogger(__name__)

_DENO_DIR = Path(__file__).resolve().parent.parent / ".devdownloads" / "deno"
_DENO_VERSION = "v2.8.1"
_WINDOWS_URL = f"https://github.com/denoland/deno/releases/download/{_DENO_VERSION}/deno-x86_64-pc-windows-msvc.zip"
_LINUX_URL = f"https://github.com/denoland/deno/releases/download/{_DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip"

_cached_path: str | None = None
_MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
_DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com"}


def _exe() -> str:
    return "deno.exe" if sys.platform == "win32" else "deno"


def _is_valid(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _safe_zip_entry(item: zipfile.ZipInfo) -> bool:
    name = item.filename.replace("\\", "/")
    parts = [part for part in name.split("/") if part]
    return bool(parts) and not name.startswith("/") and ".." not in parts and not stat.S_ISLNK(item.external_attr >> 16)


def _verify_version(path: Path) -> None:
    result = subprocess.run(
        [str(path), "--version"], capture_output=True, text=True, timeout=15,
    )
    first_line = (result.stdout or "").splitlines()[:1]
    # Saída real: "deno 2.8.1 (stable, release, x86_64-...)" — compara os
    # dois primeiros tokens, não a linha inteira.
    tokens = first_line[0].split()[:2] if first_line else []
    if result.returncode != 0 or tokens != ["deno", "2.8.1"]:
        raise RuntimeError("Versão extraída do Deno não corresponde ao release fixado.")


def _download_deno() -> Path:
    url = _WINDOWS_URL if sys.platform == "win32" else _LINUX_URL
    log.info("Baixando Deno para resolver desafios JS do YouTube...")
    expected = (
        get_settings().deno_windows_archive_sha256
        if sys.platform == "win32"
        else get_settings().deno_linux_archive_sha256
    ).strip().lower()
    _DENO_DIR.parent.mkdir(parents=True, exist_ok=True)
    exe_name = _exe()
    with verified_archive(
        url,
        expected,
        max_bytes=_MAX_ARCHIVE_BYTES,
        allowed_hosts=_DOWNLOAD_HOSTS,
        prefix="xard-deno-",
    ) as archive, tempfile.TemporaryDirectory(
        prefix="deno-install-", dir=_DENO_DIR.parent,
    ) as staging_name:
        staging = Path(staging_name) / exe_name
        with zipfile.ZipFile(archive) as zf:
            entries = zf.infolist()
            if any(not _safe_zip_entry(item) for item in entries):
                raise RuntimeError("Pacote Deno contém caminho ou symlink inseguro.")
            candidates = [
                item for item in entries
                if os.path.basename(item.filename) == exe_name
            ]
            if len(candidates) != 1:
                raise RuntimeError("Pacote Deno contém executável ausente ou duplicado.")
            item = candidates[0]
            if item.file_size > 250 * 1024 * 1024:
                raise RuntimeError("Executável Deno excede o limite permitido.")
            with zf.open(item) as src, staging.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        staging.chmod(staging.stat().st_mode | stat.S_IEXEC)
        _verify_version(staging)
        _DENO_DIR.mkdir(parents=True, exist_ok=True)
        deno = _DENO_DIR / exe_name
        os.replace(staging, deno)
        return deno


def ensure_deno() -> str | None:
    """Retorna o caminho do Deno, baixando um binario portatil se preciso."""
    global _cached_path  # noqa: PLW0603
    if _cached_path:
        return _cached_path

    local = _DENO_DIR / _exe()
    if _is_valid(local):
        _cached_path = str(local)
        return _cached_path

    system = shutil.which("deno")
    if system:
        _cached_path = system
        return _cached_path

    if not get_settings().worker_binary_downloads_enabled:
        raise RuntimeError("Deno não está instalado na imagem do worker.")
    try:
        with install_lock(_DENO_DIR.parent / ".deno-install.lock"):
            if _is_valid(local):
                _cached_path = str(local)
            else:
                _cached_path = str(_download_deno())
        return _cached_path
    except Exception:
        log.exception("Falha ao preparar o Deno automaticamente.")
        raise
