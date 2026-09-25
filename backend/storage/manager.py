"""Seleção do backend e ciclo de vida do cliente usado pela API."""

from __future__ import annotations

from pathlib import Path

import httpx

from backend.api.config import Settings, get_settings
from backend.storage.base import StorageBackend
from backend.storage.google_drive import GoogleDriveStorageBackend
from backend.storage.local import LocalStorageBackend

_api_backends: dict[str, StorageBackend] = {}


def create_storage_backend(
    settings: Settings | None = None,
    *,
    backend_name: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> StorageBackend:
    selected = settings or get_settings()
    name = backend_name or selected.storage_backend
    if name == "google_drive":
        return GoogleDriveStorageBackend(selected, client=client)
    if name != "local":
        raise ValueError("Backend de armazenamento desconhecido.")
    return LocalStorageBackend(
        Path(selected.downloads_dir),
        chunk_size=selected.remote_download_chunk_size_mb * 1024 * 1024,
    )


def get_api_storage_backend(backend_name: str | None = None) -> StorageBackend:
    name = backend_name or get_settings().storage_backend
    if name not in _api_backends:
        _api_backends[name] = create_storage_backend(backend_name=name)
    return _api_backends[name]


async def shutdown_storage_backend() -> None:
    backends = list(_api_backends.values())
    _api_backends.clear()
    for backend in backends:
        await backend.close()
