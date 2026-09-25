"""Download verificado em streaming e lock entre processos para binários."""

from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from backend.security.url_validation import open_public_url


@contextmanager
def install_lock(path: Path) -> Iterator[None]:
    """Serializa instalação entre filhos prefork sem dependência adicional."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def verified_archive(
    url: str,
    expected_hash: str,
    *,
    max_bytes: int,
    allowed_hosts: set[str],
    prefix: str,
) -> Iterator[Path]:
    """Baixa para disco em blocos, valida limite/SHA-256 e sempre limpa."""
    expected = expected_hash.strip().lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise RuntimeError("SHA-256 do pacote não configurado; download bloqueado.")

    fd, raw_path = tempfile.mkstemp(prefix=prefix, suffix=".archive")
    path = Path(raw_path)
    digest = hashlib.sha256()
    received = 0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "XardWorker/1.0"})
        with os.fdopen(fd, "wb") as output, open_public_url(
            request,
            timeout=120,
            https_only=True,
            allowed_hosts=allowed_hosts,
            max_redirects=3,
        ) as response:
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > max_bytes:
                raise RuntimeError("Pacote excede o limite permitido.")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > max_bytes:
                    raise RuntimeError("Pacote excede o limite permitido.")
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != expected:
            raise RuntimeError("Pacote não corresponde ao hash fixado.")
        yield path
    finally:
        path.unlink(missing_ok=True)
