"""Contrato comum dos backends de armazenamento."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


class StorageError(RuntimeError):
    """Erro normalizado que nunca contém credenciais ou URLs privadas."""


class StorageTemporaryError(StorageError):
    """Falha transitória que pode ser repetida com backoff."""


class StoragePermanentError(StorageError):
    """Falha que exige correção de configuração ou ação administrativa."""


class StorageAuthorizationError(StoragePermanentError):
    """A autorização remota foi revogada, expirou ou ficou inválida."""


class StorageNotFoundError(StoragePermanentError):
    """O objeto não existe mais no backend configurado."""


class StorageRangeNotSatisfiableError(StoragePermanentError):
    """O intervalo HTTP solicitado é inválido ou não pode ser atendido."""


class StorageSecurityError(StoragePermanentError):
    """A pasta ou o arquivo possui uma permissão pública proibida."""


@dataclass(frozen=True, slots=True)
class StorageMetadata:
    backend: str
    key: str
    size: int
    checksum: str | None
    mime_type: str
    parent_id: str | None
    uploaded_at: datetime


@dataclass(slots=True)
class StorageDownload:
    status_code: int
    chunks: AsyncIterator[bytes]
    content_length: int | None = None
    content_range: str | None = None
    content_type: str = "application/octet-stream"
    accept_ranges: str = "bytes"
    _close: Callable[[], Awaitable[None]] | None = None

    async def aclose(self) -> None:
        close, self._close = self._close, None
        if close is not None:
            await close()


class StorageBackend(Protocol):
    name: str

    async def upload_file(
        self,
        local_path: Path,
        *,
        kind: str,
        idempotency_key: str,
        mime_type: str,
        reserved_key: str | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> StorageMetadata: ...

    async def open_download_stream(
        self,
        key: str,
        *,
        byte_range: str | None = None,
    ) -> StorageDownload: ...

    async def delete_file(self, key: str) -> None: ...

    async def file_exists(self, key: str) -> bool: ...

    async def get_metadata(self, key: str) -> StorageMetadata: ...

    async def close(self) -> None: ...
