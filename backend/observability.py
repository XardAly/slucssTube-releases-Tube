"""
Observabilidade compartilhada: logging estruturado e medição de memória.

Usado pela API e pelos processos isolados — cada processo chama setup_logging()
com o nome do serviço e todos os logs saem no mesmo formato filtrável:

    2026-07-21 17:28:33 | INFO | api | downloads.router | video_info request_id=... status=success
"""

from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)s | {service} | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Loggers de bibliotecas que poluem o INFO com detalhes internos
# (por exemplo, httpx loga cada requisição ao Google Drive).
_NOISY_LOGGERS = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "urllib3": logging.WARNING,
    "asyncio": logging.WARNING,
    "websockets": logging.WARNING,
    "uvicorn.access": logging.WARNING,
}

_SECRET_PATTERN = re.compile(
    r"(?i)\b(token|signature|challenge|cookie|authorization|secret|password"
    r"|api[_-]?key|refresh_token|access_token|private_key|x-goog-\w+)"
    r"(=|:\s*|\s+)([^\s&\"']+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")


class _MaskingFormatter(logging.Formatter):
    """Redige tokens/assinaturas/cookies na mensagem final formatada."""

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        rendered = _BEARER_PATTERN.sub("Bearer ***", rendered)
        return _SECRET_PATTERN.sub(r"\1\2***", rendered)


class _OperationalNoiseFilter(logging.Filter):
    """Oculta somente mensagens internas duplicadas das conexões WebSocket."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "uvicorn.error" or record.levelno >= logging.WARNING:
            return True
        message = record.getMessage()
        return not (
            ('"WebSocket ' in message and "[accepted]" in message)
            or message in {"connection open", "connection closed"}
        )


_CGROUP_MEMORY_FILES = (
    (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory.current")),
    (
        Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
        Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
    ),
)


def container_memory_snapshot() -> tuple[int, int] | None:
    """Retorna (limite, uso) em bytes do cgroup, sem confundir RAM física do host."""
    for limit_path, usage_path in _CGROUP_MEMORY_FILES:
        try:
            raw_limit = limit_path.read_text(encoding="ascii").strip()
            if raw_limit == "max":
                continue
            limit = int(raw_limit)
            usage = int(usage_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            continue
        # cgroup v1 usa números próximos de INT64_MAX para indicar "ilimitado".
        if 0 < limit < (1 << 50) and usage >= 0:
            return limit, min(usage, limit)
    return None


def memory_headroom_mb(configured_limit_mb: int | None) -> tuple[int, int] | None:
    """Retorna (limite_mb, disponível_mb) combinando cgroup e teto configurado.

    None quando não há como estimar (sem cgroup e sem limite configurado) —
    quem chama deve tratar isso como "sem informação", nunca como "memória livre".
    """
    snapshot = container_memory_snapshot()
    if snapshot is None and configured_limit_mb is None:
        return None
    mib = 1024 * 1024
    if snapshot is None:
        # Sem cgroup, uma configuração explícita continua fail-closed: sabemos
        # o teto, mas não inventamos quanto os demais processos estão usando.
        limit = int(configured_limit_mb or 0) * mib
        usage = limit
    else:
        limit, usage = snapshot
        if configured_limit_mb is not None:
            limit = min(limit, configured_limit_mb * mib)
            usage = min(usage, limit)
    return limit // mib, max(0, limit - usage) // mib


_malloc_trim = None
_malloc_trim_resolved = False


def release_freed_memory() -> None:
    """Devolve ao SO páginas já liberadas que o glibc retém nas arenas (Linux).

    Não substitui correção de vazamento: atua DEPOIS que os objetos grandes
    saíram de escopo, empurrando de volta memória que o malloc manteria
    reservada — é o que faz o RSS baixar após download/conversão/consulta.
    No-op no Windows/macOS e em libc sem malloc_trim (ex.: musl).
    """
    global _malloc_trim, _malloc_trim_resolved  # noqa: PLW0603
    if not _malloc_trim_resolved:
        _malloc_trim_resolved = True
        if sys.platform == "linux":
            try:
                import ctypes
                _malloc_trim = ctypes.CDLL("libc.so.6").malloc_trim
            except Exception:  # noqa: BLE001 - libc sem malloc_trim
                _malloc_trim = None
    if _malloc_trim is None:
        return
    try:
        _malloc_trim(0)
    except Exception:  # noqa: BLE001 - liberação é oportunista, nunca falha a tarefa
        pass


def rss_mb() -> float:
    """RSS do processo atual em MB; -1.0 se o psutil não estiver disponível."""
    try:
        import psutil
    except ImportError:
        return -1.0
    try:
        return round(psutil.Process().memory_info().rss / (1024 * 1024), 1)
    except Exception:  # noqa: BLE001 - instrumentação nunca pode derrubar a operação
        return -1.0


def setup_logging(service: str, level: int = logging.INFO) -> None:
    """Configura o root logger do processo. Idempotente."""
    root = logging.getLogger()
    if getattr(root, "_xard_service", None) == service:
        return
    root._xard_service = service  # type: ignore[attr-defined]

    formatter = _MaskingFormatter(
        _LOG_FORMAT.format(service=service), datefmt=_DATE_FORMAT,
    )
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    log_dir = os.environ.get("LOG_DIR", "").strip()
    if log_dir:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(
            directory / f"xard-{service}.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
            delay=True,
        ))

    root.handlers.clear()
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(_OperationalNoiseFilter())
        root.addHandler(handler)
    root.setLevel(level)

    for name, noisy_level in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(noisy_level)
