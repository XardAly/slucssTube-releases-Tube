"""Backend local retrocompatível e restrito a DOWNLOADS_DIR."""

from __future__ import annotations

import mimetypes
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import anyio

from backend.storage.base import (
    StorageDownload,
    StorageMetadata,
    StoragePermanentError,
    StorageRangeNotSatisfiableError,
)


class LocalStorageBackend:
    name = "local"

    def __init__(self, root: Path, *, chunk_size: int = 1024 * 1024) -> None:
        self.root = root
        self.chunk_size = chunk_size

    def _safe_file(self, key: str | Path) -> Path:
        raw = Path(key)
        try:
            root = self.root.resolve(strict=True)
            if raw.is_symlink():
                raise StoragePermanentError("Arquivo local inválido.")
            path = raw.resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise StoragePermanentError("Arquivo local indisponível.")
            return path
        except OSError as exc:
            raise StoragePermanentError("Arquivo local indisponível.") from exc

    async def upload_file(
        self,
        local_path: Path,
        *,
        kind: str,
        idempotency_key: str,
        mime_type: str,
        reserved_key: str | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> StorageMetadata:
        del kind, idempotency_key, reserved_key
        path = self._safe_file(local_path)
        if cancel_requested and cancel_requested():
            raise StoragePermanentError("Operação de armazenamento cancelada.")
        return StorageMetadata(
            backend=self.name,
            key=str(path),
            size=path.stat().st_size,
            checksum=None,
            mime_type=mime_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            parent_id=None,
            uploaded_at=datetime.now(timezone.utc),
        )

    async def open_download_stream(
        self,
        key: str,
        *,
        byte_range: str | None = None,
    ) -> StorageDownload:
        path = self._safe_file(key)
        size = path.stat().st_size
        start, end = 0, size - 1
        status_code = 200
        content_range = None
        if byte_range:
            try:
                unit, value = byte_range.split("=", 1)
                first, last = value.split("-", 1)
                if unit != "bytes" or "," in value or (not first and not last):
                    raise ValueError
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                else:
                    suffix_length = int(last)
                    if suffix_length <= 0:
                        raise ValueError
                    start = max(size - suffix_length, 0)
                    end = size - 1
                if start < 0 or start > end or start >= size:
                    raise ValueError
            except ValueError as exc:
                raise StorageRangeNotSatisfiableError("Intervalo de download inválido.") from exc
            status_code = 206
            content_range = f"bytes {start}-{end}/{size}"

        async def chunks():
            remaining = end - start + 1
            async with await anyio.open_file(path, "rb") as source:
                await source.seek(start)
                while remaining:
                    block = await source.read(min(self.chunk_size, remaining))
                    if not block:
                        break
                    remaining -= len(block)
                    yield block

        return StorageDownload(
            status_code=status_code,
            chunks=chunks(),
            content_length=end - start + 1,
            content_range=content_range,
            content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        )

    async def delete_file(self, key: str) -> None:
        path = self._safe_file(key)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise StoragePermanentError("Não foi possível remover o arquivo local.") from exc

    async def file_exists(self, key: str) -> bool:
        try:
            self._safe_file(key)
            return True
        except StoragePermanentError:
            return False

    async def get_metadata(self, key: str) -> StorageMetadata:
        return await self.upload_file(
            Path(key), kind="local", idempotency_key="local",
            mime_type=mimetypes.guess_type(key)[0] or "application/octet-stream",
        )

    async def close(self) -> None:
        return None
