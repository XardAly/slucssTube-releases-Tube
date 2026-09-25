"""Backends privados de armazenamento de arquivos concluídos."""

from backend.storage.base import (
    StorageBackend,
    StorageDownload,
    StorageError,
    StorageMetadata,
    StorageNotFoundError,
    StoragePermanentError,
    StorageRangeNotSatisfiableError,
    StorageSecurityError,
    StorageTemporaryError,
)

__all__ = [
    "StorageBackend",
    "StorageDownload",
    "StorageError",
    "StorageMetadata",
    "StorageNotFoundError",
    "StoragePermanentError",
    "StorageRangeNotSatisfiableError",
    "StorageSecurityError",
    "StorageTemporaryError",
]
