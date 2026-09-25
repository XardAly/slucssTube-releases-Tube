"""Google Drive privado via API REST, com streaming e upload retomável."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import re
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import anyio
import httpx
import jwt as pyjwt

from backend.api.config import Settings
from backend.storage.base import (
    StorageAuthorizationError,
    StorageDownload,
    StorageMetadata,
    StorageNotFoundError,
    StoragePermanentError,
    StorageRangeNotSatisfiableError,
    StorageSecurityError,
    StorageTemporaryError,
)

_API = "https://www.googleapis.com/drive/v3"
_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_PROCESS_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_PROCESS_TOKEN_CACHE_LOCK = threading.Lock()
_DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,256}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9:_-]{8,200}$")
_TEMPORARY_STATUS = {408, 429, 500, 502, 503, 504}
_TEMPORARY_REASONS = {
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "backendError",
    "internalError",
}
_FOLDER_MIME = "application/vnd.google-apps.folder"
logger = logging.getLogger(__name__)


class GoogleDriveStorageBackend:
    name = "google_drive"

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=20,
                read=max(settings.remote_upload_timeout_seconds, settings.remote_download_timeout_seconds),
                write=settings.remote_upload_timeout_seconds,
                pool=20,
            ),
            limits=httpx.Limits(
                max_connections=(
                    settings.google_drive_max_concurrent_uploads
                    + settings.google_drive_max_concurrent_downloads
                    + 4
                ),
                max_keepalive_connections=8,
            ),
            follow_redirects=False,
        )
        self._token_lock = asyncio.Lock()
        self._token_cache: tuple[str, float] | None = None
        self._upload_slots = asyncio.Semaphore(settings.google_drive_max_concurrent_uploads)
        self._download_slots = asyncio.Semaphore(settings.google_drive_max_concurrent_downloads)
        self._folder_lock = asyncio.Lock()
        self._validated_parents: dict[str, float] = {}
        self._managed_parent_cache: dict[str, tuple[bool, float]] = {}
        self._system_folders: dict[str, str] = {}
        self._quota_cache: tuple[dict[str, int | float | None], float] | None = None
        self._random = random.SystemRandom()
        self._validate_configuration()

    def _token_cache_key(self) -> str:
        identity = (
            (
                f"{self.settings.google_drive_client_id}:"
                f"{hashlib.sha256(self.settings.google_drive_refresh_token.encode()).hexdigest()}"
            )
            if self.settings.google_drive_auth_mode == "oauth"
            else (
                self.settings.google_drive_credentials_file
                or hashlib.sha256(
                    self.settings.google_drive_credentials_base64.encode(),
                ).hexdigest()
            )
        )
        return f"{self.settings.google_drive_auth_mode}:{self.settings.google_drive_scope}:{identity}"

    def _load_process_token(self) -> None:
        with _PROCESS_TOKEN_CACHE_LOCK:
            cached = _PROCESS_TOKEN_CACHE.get(self._token_cache_key())
        if cached and time.monotonic() < cached[1]:
            self._token_cache = cached

    def _store_process_token(self, cached: tuple[str, float] | None) -> None:
        cache_key = self._token_cache_key()
        with _PROCESS_TOKEN_CACHE_LOCK:
            if cached is None:
                _PROCESS_TOKEN_CACHE.pop(cache_key, None)
                return
            if len(_PROCESS_TOKEN_CACHE) >= 8 and cache_key not in _PROCESS_TOKEN_CACHE:
                _PROCESS_TOKEN_CACHE.pop(next(iter(_PROCESS_TOKEN_CACHE)))
            _PROCESS_TOKEN_CACHE[cache_key] = cached

    def _validate_configuration(self) -> None:
        root_id = self.settings.google_drive_root_folder_id
        if not _DRIVE_ID_RE.fullmatch(root_id):
            raise StoragePermanentError("Pasta raiz do Google Drive não configurada corretamente.")
        optional_ids = (
            self.settings.google_drive_videos_folder_id,
            self.settings.google_drive_gifs_folder_id,
        )
        if any(value and not _DRIVE_ID_RE.fullmatch(value) for value in optional_ids):
            raise StoragePermanentError("Pasta de tipo do Google Drive inválida.")
        shared_drive = self.settings.google_drive_shared_drive_id
        if shared_drive and not _DRIVE_ID_RE.fullmatch(shared_drive):
            raise StoragePermanentError("Shared Drive inválido.")
        if self.settings.google_drive_auth_mode == "service_account":
            configured = bool(
                self.settings.google_drive_credentials_file
                or self.settings.google_drive_credentials_base64
            )
            if not configured:
                raise StoragePermanentError("Credencial de serviço do Google Drive ausente.")
            self._load_service_account()
        elif not all((
            self.settings.google_drive_client_id,
            self.settings.google_drive_client_secret,
            self.settings.google_drive_refresh_token,
        )):
            raise StoragePermanentError("Credencial OAuth do Google Drive incompleta.")

    def _load_service_account(self) -> dict:
        file_name = self.settings.google_drive_credentials_file.strip()
        encoded = self.settings.google_drive_credentials_base64.strip()
        if file_name and encoded:
            raise StoragePermanentError("Configure apenas uma fonte de credencial do Google Drive.")
        try:
            if file_name:
                path = Path(file_name)
                if not path.is_file() or path.is_symlink() or path.stat().st_size > 128 * 1024:
                    raise StoragePermanentError("Arquivo de credencial do Google Drive inválido.")
                if self.settings.environment.lower() == "production" and hasattr(path.stat(), "st_mode"):
                    if path.stat().st_mode & 0o077:
                        raise StorageSecurityError("Permissões do arquivo de credencial são excessivas.")
                raw = path.read_bytes()
            else:
                raw = base64.b64decode(encoded, validate=True)
                if len(raw) > 128 * 1024:
                    raise ValueError
            data = json.loads(raw)
            required = {"client_email", "private_key", "token_uri"}
            if not required.issubset(data) or data["token_uri"] != _TOKEN_URL:
                raise ValueError
            return data
        except StoragePermanentError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise StoragePermanentError("Credencial de serviço do Google Drive inválida.") from exc

    async def _access_token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh and self._token_cache is None:
            self._load_process_token()
        now = time.monotonic()
        if not force_refresh and self._token_cache and now < self._token_cache[1]:
            return self._token_cache[0]
        async with self._token_lock:
            now = time.monotonic()
            if not force_refresh and self._token_cache and now < self._token_cache[1]:
                return self._token_cache[0]
            try:
                if self.settings.google_drive_auth_mode == "service_account":
                    account = self._load_service_account()
                    issued_at = int(time.time())
                    assertion = pyjwt.encode(
                        {
                            "iss": account["client_email"],
                            "scope": self.settings.google_drive_scope,
                            "aud": _TOKEN_URL,
                            "iat": issued_at,
                            "exp": issued_at + 3600,
                        },
                        account["private_key"],
                        algorithm="RS256",
                    )
                    data = {
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": assertion,
                    }
                else:
                    data = {
                        "client_id": self.settings.google_drive_client_id,
                        "client_secret": self.settings.google_drive_client_secret,
                        "refresh_token": self.settings.google_drive_refresh_token,
                        "grant_type": "refresh_token",
                    }
            except (OSError, ValueError, TypeError) as exc:
                raise StoragePermanentError("Não foi possível preparar a autenticação remota.") from exc
            auth_retries = max(2, min(self.settings.remote_upload_max_retries, 4))
            response: httpx.Response | None = None
            last_auth_error: Exception | None = None
            for attempt in range(auth_retries + 1):
                try:
                    response = await self._client.post(_TOKEN_URL, data=data)
                except httpx.HTTPError as exc:
                    last_auth_error = exc
                else:
                    if response.status_code == 200:
                        break
                    if response.status_code not in _TEMPORARY_STATUS:
                        try:
                            error_code = str(response.json().get("error") or "")
                        except (ValueError, AttributeError):
                            error_code = ""
                        if error_code == "invalid_grant":
                            raise StorageAuthorizationError(
                                "Autorização do Google Drive expirada ou revogada; reconecte a integração."
                            )
                        if error_code == "invalid_client":
                            raise StorageAuthorizationError("Cliente OAuth do Google Drive recusado; reconecte a integração.")
                        raise StoragePermanentError("Autenticação do Google Drive recusada.")
                    last_auth_error = StorageTemporaryError(
                        "Autenticação remota temporariamente indisponível."
                    )
                if attempt >= auth_retries:
                    raise StorageTemporaryError(
                        "Autenticação remota temporariamente indisponível."
                    ) from last_auth_error
                await anyio.sleep(min(2 ** attempt, 4) + self._random.uniform(0, 0.25))
            if response is None or response.status_code != 200:
                raise StorageTemporaryError("Autenticação remota temporariamente indisponível.")
            try:
                payload = response.json()
                token = str(payload["access_token"])
                expires_in = max(60, int(payload.get("expires_in") or 3600))
            except (ValueError, KeyError, TypeError) as exc:
                raise StoragePermanentError("Resposta de autenticação remota inválida.") from exc
            self._token_cache = (token, time.monotonic() + max(30, expires_in - 60))
            self._store_process_token(self._token_cache)
            return token

    @staticmethod
    def _error_reason(response: httpx.Response) -> str:
        try:
            error = response.json().get("error", {})
            reasons = error.get("errors") or []
            if reasons and isinstance(reasons[0], dict):
                return str(reasons[0].get("reason") or "")
            return str(error.get("status") or "")
        except (ValueError, AttributeError):
            return ""

    def _raise_response(self, response: httpx.Response) -> None:
        if response.status_code == 404:
            raise StorageNotFoundError("Arquivo ou pasta remota não encontrada.")
        if response.status_code == 416:
            raise StorageRangeNotSatisfiableError("Intervalo de download não satisfazível.")
        if response.status_code == 409:
            raise StorageTemporaryError("Objeto remoto reservado ainda está em criação.")
        reason = self._error_reason(response)
        if response.status_code in _TEMPORARY_STATUS or reason in _TEMPORARY_REASONS:
            raise StorageTemporaryError("Google Drive temporariamente indisponível.")
        if response.status_code == 403 and reason in {"storageQuotaExceeded", "dailyLimitExceeded"}:
            raise StorageTemporaryError("Quota do Google Drive temporariamente indisponível.")
        if response.status_code in {401, 403}:
            raise StoragePermanentError("Acesso ao Google Drive recusado.")
        raise StoragePermanentError("Resposta inválida do Google Drive.")

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        base_headers = dict(kwargs.pop("headers", {}))
        retries = int(kwargs.pop("_max_retries", self.settings.remote_upload_max_retries))
        force_refresh = False
        token_refreshed = False
        last_error: Exception | None = None
        temporary_attempts = 0
        while True:
            token = await self._access_token(force_refresh=force_refresh)
            force_refresh = False
            headers = dict(base_headers)
            headers["Authorization"] = f"Bearer {token}"
            try:
                response = await self._client.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                last_error = StorageTemporaryError("Falha temporária de comunicação com o Google Drive.")
            else:
                if response.status_code == 401 and not token_refreshed:
                    self._token_cache = None
                    self._store_process_token(None)
                    force_refresh = True
                    token_refreshed = True
                    last_error = StoragePermanentError("Token remoto recusado.")
                    continue
                elif response.status_code < 400 or response.status_code == 308:
                    return response
                else:
                    try:
                        self._raise_response(response)
                    except StorageTemporaryError as exc:
                        last_error = exc
                    except StoragePermanentError:
                        raise
            if temporary_attempts >= retries:
                break
            delay = min(2 ** temporary_attempts, 30) + self._random.uniform(0, 0.5)
            temporary_attempts += 1
            await anyio.sleep(delay)
        if isinstance(last_error, StorageTemporaryError):
            raise last_error
        if isinstance(last_error, StoragePermanentError):
            raise last_error
        raise StorageTemporaryError("Google Drive temporariamente indisponível.") from last_error

    async def _ensure_system_folder(self, folder_name: str, configured_id: str = "") -> str:
        cached = self._system_folders.get(folder_name)
        if cached:
            return cached
        async with self._folder_lock:
            cached = self._system_folders.get(folder_name)
            if cached:
                return cached
            root_id = self.settings.google_drive_root_folder_id
            await self._validate_parent(root_id)
            if configured_id:
                metadata = await self._file_metadata(configured_id)
                if root_id not in (metadata.get("parents") or []):
                    raise StorageSecurityError("Pasta de tipo fora da raiz administrada.")
                await self._validate_parent(configured_id)
                self._system_folders[folder_name] = configured_id
                return configured_id

            escaped = folder_name.replace("'", "\\'")
            query = (
                f"'{root_id}' in parents and trashed = false and "
                f"mimeType = '{_FOLDER_MIME}' and "
                f"appProperties has {{ key='xardSystemFolder' and value='{escaped}' }}"
            )
            response = await self._request(
                "GET",
                f"{_API}/files",
                params={
                    "q": query,
                    "spaces": "drive",
                    "pageSize": "10",
                    "fields": "files(id,mimeType,parents,trashed,appProperties)",
                    "includeItemsFromAllDrives": "true",
                    "supportsAllDrives": "true",
                    **({
                        "corpora": "drive",
                        "driveId": self.settings.google_drive_shared_drive_id,
                    } if self.settings.google_drive_shared_drive_id else {}),
                },
            )
            try:
                folders = list(response.json().get("files") or [])
            except ValueError as exc:
                raise StoragePermanentError("Busca de pasta privada inválida.") from exc
            if not folders:
                created = await self._request(
                    "POST",
                    f"{_API}/files",
                    params={
                        "supportsAllDrives": "true",
                        "fields": "id,mimeType,parents,trashed,appProperties",
                    },
                    json={
                        "name": folder_name,
                        "mimeType": _FOLDER_MIME,
                        "parents": [root_id],
                        "appProperties": {
                            "xardManaged": "1",
                            "xardSystemFolder": folder_name,
                        },
                    },
                )
                try:
                    folders = [created.json()]
                except ValueError as exc:
                    raise StoragePermanentError("Criação de pasta privada inválida.") from exc
            folders.sort(key=lambda item: str(item.get("id") or ""))
            selected_id = str(folders[0].get("id") or "")
            if len(folders) > 1:
                logger.warning("google_drive_duplicate_system_folder kind=%s count=%s", folder_name, len(folders))
            if not _DRIVE_ID_RE.fullmatch(selected_id):
                raise StoragePermanentError("Identificador de pasta privada inválido.")
            await self._validate_parent(selected_id)
            self._system_folders[folder_name] = selected_id
            return selected_id

    async def _base_parent_for(self, kind: str) -> str:
        if kind == "gif":
            return await self._ensure_system_folder(
                "gifs", self.settings.google_drive_gifs_folder_id,
            )
        if kind in {"video", "audio", "photo"}:
            return await self._ensure_system_folder(
                "videos", self.settings.google_drive_videos_folder_id,
            )
        raise StoragePermanentError("Tipo de arquivo remoto inválido.")

    async def _find_partition_folder(self, parent_id: str, partition_key: str) -> list[dict]:
        escaped = partition_key.replace("'", "\\'")
        query = (
            f"'{parent_id}' in parents and trashed = false and "
            "mimeType = 'application/vnd.google-apps.folder' and "
            f"appProperties has {{ key='xardPartition' and value='{escaped}' }}"
        )
        response = await self._request(
            "GET",
            f"{_API}/files",
            params={
                "q": query,
                "spaces": "drive",
                "pageSize": "10",
                "fields": "files(id,mimeType,parents,createdTime,trashed,appProperties)",
                "includeItemsFromAllDrives": "true",
                "supportsAllDrives": "true",
                **({
                    "corpora": "drive",
                    "driveId": self.settings.google_drive_shared_drive_id,
                } if self.settings.google_drive_shared_drive_id else {}),
            },
        )
        try:
            return list(response.json().get("files") or [])
        except ValueError as exc:
            raise StoragePermanentError("Busca de partição remota inválida.") from exc

    async def _partition_parent(self, kind: str) -> str:
        root_id = self.settings.google_drive_root_folder_id
        base_id = await self._base_parent_for(kind)
        await self._validate_parent(root_id)
        await self._validate_parent(base_id)
        base_metadata = await self._file_metadata(base_id)
        if base_id != root_id and root_id not in (base_metadata.get("parents") or []):
            raise StorageSecurityError("Pasta de tipo fora da raiz administrada.")

        now = datetime.now(timezone.utc)
        parent_id = base_id
        partition_parts = (f"{now.year:04d}", f"{now.month:02d}", f"{now.day:02d}")
        accumulated: list[str] = []
        for segment in partition_parts:
            accumulated.append(segment)
            partition_key = f"{kind}:{'/'.join(accumulated)}"
            folders = await self._find_partition_folder(parent_id, partition_key)
            if not folders:
                created = await self._request(
                    "POST",
                    f"{_API}/files",
                    params={
                        "supportsAllDrives": "true",
                        "fields": "id,mimeType,parents,createdTime,trashed,appProperties",
                    },
                    json={
                        "name": segment,
                        "mimeType": "application/vnd.google-apps.folder",
                        "parents": [parent_id],
                        "appProperties": {
                            "xardManaged": "1",
                            "xardPartition": partition_key,
                            "xardKind": kind,
                        },
                    },
                )
                try:
                    created_folder = created.json()
                except ValueError as exc:
                    raise StoragePermanentError("Criação de partição remota inválida.") from exc
                folders = await self._find_partition_folder(parent_id, partition_key)
                if not folders:
                    folders = [created_folder]
            folders.sort(key=lambda item: str(item.get("id") or ""))
            selected = folders[0]
            parent_id = str(selected.get("id") or "")
            await self._validate_parent(parent_id)
        return parent_id

    async def _file_metadata(self, key: str) -> dict:
        if not _DRIVE_ID_RE.fullmatch(key):
            raise StoragePermanentError("Identificador remoto inválido.")
        response = await self._request(
            "GET",
            f"{_API}/files/{quote(key, safe='')}",
            params={
                "supportsAllDrives": "true",
                "fields": "id,size,md5Checksum,mimeType,parents,createdTime,trashed,appProperties",
            },
        )
        try:
            return response.json()
        except ValueError as exc:
            raise StoragePermanentError("Metadados remotos inválidos.") from exc

    async def _assert_private(self, key: str) -> None:
        page_token = None
        for _ in range(10):
            params = {
                "supportsAllDrives": "true",
                "fields": "nextPageToken,permissions(id,type,role,permissionDetails)",
                "pageSize": "100",
            }
            if page_token:
                params["pageToken"] = page_token
            response = await self._request(
                "GET", f"{_API}/files/{quote(key, safe='')}/permissions", params=params,
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise StoragePermanentError("Permissões remotas inválidas.") from exc
            for permission in payload.get("permissions") or []:
                if permission.get("type") in {"anyone", "domain"}:
                    raise StorageSecurityError("Permissão pública detectada no armazenamento remoto.")
            page_token = payload.get("nextPageToken")
            if not page_token:
                return
        raise StoragePermanentError("Listagem de permissões remotas excedeu o limite seguro.")

    async def _validate_parent(self, parent_id: str) -> None:
        if time.monotonic() < self._validated_parents.get(parent_id, 0):
            return
        metadata = await self._file_metadata(parent_id)
        if metadata.get("trashed") or metadata.get("mimeType") != "application/vnd.google-apps.folder":
            raise StoragePermanentError("Pasta remota inválida ou removida.")
        await self._assert_private(parent_id)
        if len(self._validated_parents) >= 128:
            self._validated_parents.pop(next(iter(self._validated_parents)))
        self._validated_parents[parent_id] = time.monotonic() + 300

    async def _find_existing(self, parent_id: str, idempotency_key: str) -> dict | None:
        if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise StoragePermanentError("Chave idempotente inválida.")
        escaped = idempotency_key.replace("'", "\\'")
        query = (
            f"'{parent_id}' in parents and trashed = false and "
            f"appProperties has {{ key='xardKey' and value='{escaped}' }}"
        )
        params = {
            "q": query,
            "spaces": "drive",
            "pageSize": "10",
            "fields": "files(id,size,md5Checksum,mimeType,parents,createdTime,trashed,appProperties)",
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
        }
        if self.settings.google_drive_shared_drive_id:
            params.update({
                "corpora": "drive",
                "driveId": self.settings.google_drive_shared_drive_id,
            })
        response = await self._request("GET", f"{_API}/files", params=params)
        try:
            files = response.json().get("files") or []
        except ValueError as exc:
            raise StoragePermanentError("Busca idempotente remota inválida.") from exc
        if len(files) > 1:
            files.sort(key=lambda item: str(item.get("id") or ""))
            fingerprints = {
                (
                    str(item.get("size") or ""),
                    str(item.get("md5Checksum") or ""),
                    str(item.get("mimeType") or ""),
                    tuple(sorted(str(parent) for parent in (item.get("parents") or []))),
                )
                for item in files
            }
            if len(fingerprints) != 1 or not files[0].get("md5Checksum"):
                raise StorageSecurityError("Objetos idempotentes remotos divergentes.")
        return files[0] if files else None

    async def _local_md5(self, path: Path) -> str:
        digest = hashlib.md5(usedforsecurity=False)
        async with await anyio.open_file(path, "rb") as source:
            while True:
                block = await source.read(1024 * 1024)
                if not block:
                    return digest.hexdigest()
                digest.update(block)

    async def storage_status(self, *, force_refresh: bool = False) -> dict[str, int | float | None]:
        now = time.monotonic()
        if not force_refresh and self._quota_cache and now < self._quota_cache[1]:
            return dict(self._quota_cache[0])
        response = await self._request(
            "GET",
            f"{_API}/about",
            params={"fields": "storageQuota(limit,usage,usageInDrive,usageInDriveTrash),maxUploadSize"},
        )
        try:
            payload = response.json()
            quota = payload.get("storageQuota") or {}
            limit = int(quota["limit"]) if quota.get("limit") is not None else None
            usage = int(quota.get("usage") or 0)
            usage_in_drive = int(quota.get("usageInDrive") or 0)
            usage_in_trash = int(quota.get("usageInDriveTrash") or 0)
            max_upload_size = int(payload.get("maxUploadSize") or 0) or None
        except (ValueError, TypeError, KeyError) as exc:
            raise StoragePermanentError("Informação de quota do Google Drive inválida.") from exc
        free = max(0, limit - usage) if limit is not None else None
        usage_percent = round((usage / limit) * 100, 2) if limit else None
        status: dict[str, int | float | None] = {
            "limit_bytes": limit,
            "usage_bytes": usage,
            "usage_in_drive_bytes": usage_in_drive,
            "trash_bytes": usage_in_trash,
            "free_bytes": free,
            "usage_percent": usage_percent,
            "max_upload_size_bytes": max_upload_size,
        }
        self._quota_cache = (status, time.monotonic() + 60)
        if usage_percent is not None and usage_percent >= self.settings.google_drive_storage_alert_percent:
            logger.warning("google_drive_storage_threshold usage_percent=%s", usage_percent)
        return dict(status)

    async def _ensure_upload_capacity(self, size: int) -> None:
        configured_limit = self.settings.google_drive_max_file_size_mb * 1024 * 1024
        if size > configured_limit:
            raise StoragePermanentError("Arquivo excede o limite configurado do Google Drive.")
        status = await self.storage_status()
        remote_limit = status.get("max_upload_size_bytes")
        if isinstance(remote_limit, int) and size > remote_limit:
            raise StoragePermanentError("Arquivo excede o limite aceito pelo Google Drive.")
        free = status.get("free_bytes")
        minimum_after_upload = self.settings.google_drive_min_free_space_mb * 1024 * 1024
        if isinstance(free, int) and free - size < minimum_after_upload:
            raise StorageTemporaryError("Espaço livre mínimo do Google Drive seria ultrapassado.")

    async def _existing_reserved_file(
        self,
        reserved_key: str,
        *,
        idempotency_key: str,
        size: int,
        local_md5: str,
    ) -> dict | None:
        try:
            existing = await self._file_metadata(reserved_key)
        except StorageNotFoundError:
            return None
        properties = existing.get("appProperties") or {}
        if (
            properties.get("xardManaged") != "1"
            or properties.get("xardKey") != idempotency_key
        ):
            raise StorageSecurityError("Identificador reservado pertence a outro objeto.")
        if int(existing.get("size") or -1) != size or existing.get("md5Checksum") != local_md5:
            raise StorageSecurityError("Objeto remoto reservado diverge do arquivo local.")
        parents = [str(parent) for parent in (existing.get("parents") or [])]
        if not parents or not any([await self._is_managed_parent(parent) for parent in parents]):
            raise StorageSecurityError("Objeto remoto reservado está fora da árvore administrada.")
        await self._assert_private(reserved_key)
        return existing

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
        try:
            async with asyncio.timeout(self.settings.remote_upload_timeout_seconds):
                return await self._upload_file_impl(
                    local_path,
                    kind=kind,
                    idempotency_key=idempotency_key,
                    mime_type=mime_type,
                    reserved_key=reserved_key,
                    cancel_requested=cancel_requested,
                )
        except TimeoutError as exc:
            raise StorageTemporaryError("Upload remoto excedeu o tempo limite.") from exc

    async def _upload_file_impl(
        self,
        local_path: Path,
        *,
        kind: str,
        idempotency_key: str,
        mime_type: str,
        reserved_key: str | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> StorageMetadata:
        try:
            size = local_path.stat().st_size
        except OSError as exc:
            raise StoragePermanentError("Arquivo local para upload não está disponível.") from exc
        parent_id = await self._partition_parent(kind)
        if reserved_key and not _DRIVE_ID_RE.fullmatch(reserved_key):
            raise StoragePermanentError("Identificador remoto reservado inválido.")
        async with self._upload_slots:
            await self._ensure_upload_capacity(size)
            local_md5 = await self._local_md5(local_path)
            if reserved_key:
                reserved_existing = await self._existing_reserved_file(
                    reserved_key,
                    idempotency_key=idempotency_key,
                    size=size,
                    local_md5=local_md5,
                )
                if reserved_existing:
                    return self._metadata_from_payload(reserved_existing)
            existing = await self._find_existing(parent_id, idempotency_key)
            if existing:
                if int(existing.get("size") or -1) != size or existing.get("md5Checksum") != local_md5:
                    raise StorageSecurityError("Objeto idempotente remoto não corresponde ao arquivo local.")
                try:
                    await self._assert_private(str(existing["id"]))
                except StorageSecurityError:
                    await self.delete_file(str(existing["id"]))
                    raise
                return self._metadata_from_payload(existing, parent_id)

            suffix = local_path.suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
                suffix = ""
            metadata = {
                "name": f"{uuid.uuid4().hex}{suffix}",
                "parents": [parent_id],
                "appProperties": {"xardKey": idempotency_key, "xardManaged": "1", "xardKind": kind},
            }
            if reserved_key:
                metadata["id"] = reserved_key
            try:
                response = await self._request(
                    "POST",
                    f"{_UPLOAD_API}/files",
                    params={
                        "uploadType": "resumable",
                        "supportsAllDrives": "true",
                        "fields": "id,size,md5Checksum,mimeType,parents,createdTime,trashed,appProperties",
                    },
                    headers={
                        "X-Upload-Content-Type": mime_type,
                        "X-Upload-Content-Length": str(size),
                        "Content-Type": "application/json; charset=UTF-8",
                    },
                    json=metadata,
                )
            except StorageTemporaryError:
                if reserved_key:
                    reserved_existing = await self._existing_reserved_file(
                        reserved_key,
                        idempotency_key=idempotency_key,
                        size=size,
                        local_md5=local_md5,
                    )
                    if reserved_existing:
                        return self._metadata_from_payload(reserved_existing)
                raise
            session_uri = response.headers.get("Location")
            if not session_uri or not session_uri.startswith("https://www.googleapis.com/"):
                raise StoragePermanentError("Sessão de upload remoto inválida.")

            chunk_size = self.settings.google_drive_upload_chunk_size_mb * 1024 * 1024
            offset = 0
            result: dict | None = None
            async with await anyio.open_file(local_path, "rb") as source:
                while offset < size:
                    if cancel_requested and cancel_requested():
                        raise StoragePermanentError("Upload remoto cancelado.")
                    await source.seek(offset)
                    chunk = await source.read(min(chunk_size, size - offset))
                    if not chunk:
                        raise StoragePermanentError("Leitura local terminou antes do esperado.")
                    end = offset + len(chunk) - 1
                    try:
                        upload_response = await self._request(
                            "PUT",
                            session_uri,
                            headers={
                                "Content-Length": str(len(chunk)),
                                "Content-Range": f"bytes {offset}-{end}/{size}",
                                "Content-Type": mime_type,
                            },
                            content=chunk,
                            _max_retries=0,
                        )
                    except StorageTemporaryError:
                        # Depois de timeout/5xx, nunca presuma quantos bytes o
                        # Drive recebeu: consulte a sessão retomável primeiro.
                        upload_response = await self._request(
                            "PUT",
                            session_uri,
                            headers={
                                "Content-Length": "0",
                                "Content-Range": f"bytes */{size}",
                            },
                            content=b"",
                        )
                    if upload_response.status_code in {200, 201}:
                        try:
                            result = upload_response.json()
                        except ValueError as exc:
                            raise StoragePermanentError("Confirmação de upload remoto inválida.") from exc
                        offset = size
                    elif upload_response.status_code == 308:
                        acknowledged = upload_response.headers.get("Range", "")
                        match = re.fullmatch(r"bytes=0-(\d+)", acknowledged)
                        offset = int(match.group(1)) + 1 if match else 0
                    else:
                        self._raise_response(upload_response)

            if not result or not result.get("id"):
                recovered = await self._find_existing(parent_id, idempotency_key)
                if not recovered:
                    raise StoragePermanentError("Upload remoto terminou sem confirmação.")
                result = recovered
            if int(result.get("size") or -1) != size or result.get("md5Checksum") != local_md5:
                raise StoragePermanentError("Tamanho ou checksum remoto divergente.")
            if parent_id not in (result.get("parents") or []):
                raise StorageSecurityError("Arquivo remoto criado fora da pasta administrada.")
            canonical = await self._find_existing(parent_id, idempotency_key)
            if canonical:
                if (
                    int(canonical.get("size") or -1) != size
                    or canonical.get("md5Checksum") != local_md5
                ):
                    raise StorageSecurityError("Objeto idempotente remoto não corresponde ao arquivo local.")
                result = canonical
            try:
                await self._assert_private(str(result["id"]))
            except StorageSecurityError:
                await self.delete_file(str(result["id"]))
                raise
            return self._metadata_from_payload(result, parent_id)

    async def reserve_file_id(self) -> str:
        response = await self._request(
            "GET",
            f"{_API}/files/generateIds",
            params={"count": "1", "space": "drive", "type": "files"},
        )
        try:
            reserved_key = str((response.json().get("ids") or [""])[0])
        except (ValueError, IndexError, TypeError) as exc:
            raise StoragePermanentError("Reserva de identificador remoto inválida.") from exc
        if not _DRIVE_ID_RE.fullmatch(reserved_key):
            raise StoragePermanentError("Reserva de identificador remoto inválida.")
        return reserved_key

    def _metadata_from_payload(self, payload: dict, parent_id: str | None = None) -> StorageMetadata:
        try:
            uploaded_at = datetime.fromisoformat(
                str(payload.get("createdTime") or "").replace("Z", "+00:00")
            )
        except ValueError:
            uploaded_at = datetime.now(timezone.utc)
        return StorageMetadata(
            backend=self.name,
            key=str(payload["id"]),
            size=int(payload.get("size") or 0),
            checksum=(f"md5:{payload['md5Checksum']}" if payload.get("md5Checksum") else None),
            mime_type=str(payload.get("mimeType") or "application/octet-stream"),
            parent_id=parent_id or next(iter(payload.get("parents") or []), None),
            uploaded_at=uploaded_at,
        )

    async def open_download_stream(
        self,
        key: str,
        *,
        byte_range: str | None = None,
    ) -> StorageDownload:
        if not _DRIVE_ID_RE.fullmatch(key):
            raise StoragePermanentError("Identificador remoto inválido.")
        if byte_range and not re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", byte_range):
            raise StorageRangeNotSatisfiableError("Intervalo de download inválido.")
        try:
            await asyncio.wait_for(
                self._download_slots.acquire(),
                timeout=min(30, self.settings.remote_download_timeout_seconds),
            )
        except TimeoutError as exc:
            raise StorageTemporaryError("Limite de downloads remotos atingido.") from exc
        response: httpx.Response | None = None
        released = False

        async def close() -> None:
            nonlocal released
            if response is not None:
                await response.aclose()
            if not released:
                released = True
                self._download_slots.release()

        try:
            token_refreshed = False
            force_refresh = False
            temporary_attempts = 0
            while True:
                token = await self._access_token(force_refresh=force_refresh)
                force_refresh = False
                headers = {"Authorization": f"Bearer {token}"}
                if byte_range:
                    headers["Range"] = byte_range
                request = self._client.build_request(
                    "GET",
                    f"{_API}/files/{quote(key, safe='')}",
                    params={"alt": "media", "supportsAllDrives": "true"},
                    headers=headers,
                )
                try:
                    response = await self._client.send(request, stream=True)
                except httpx.HTTPError as exc:
                    if temporary_attempts >= self.settings.remote_upload_max_retries:
                        raise StorageTemporaryError(
                            "Download remoto temporariamente indisponível."
                        ) from exc
                    await anyio.sleep(
                        min(2 ** temporary_attempts, 30) + self._random.uniform(0, 0.5)
                    )
                    temporary_attempts += 1
                    continue
                if response.status_code == 401 and not token_refreshed:
                    await response.aclose()
                    response = None
                    self._token_cache = None
                    self._store_process_token(None)
                    token_refreshed = True
                    force_refresh = True
                    continue
                # Respostas de sucesso permanecem em streaming e ainda não têm
                # corpo materializado; JSON de erro só pode ser lido em falhas.
                reason = self._error_reason(response) if response.status_code >= 400 else ""
                if (
                    response.status_code in _TEMPORARY_STATUS
                    or reason in _TEMPORARY_REASONS
                ) and temporary_attempts < self.settings.remote_upload_max_retries:
                    await response.aclose()
                    response = None
                    await anyio.sleep(
                        min(2 ** temporary_attempts, 30) + self._random.uniform(0, 0.5)
                    )
                    temporary_attempts += 1
                    continue
                break
            if response is None:
                raise StorageTemporaryError("Download remoto temporariamente indisponível.")
            if response.status_code not in {200, 206}:
                await response.aread()
                self._raise_response(response)
            if byte_range and response.status_code != 206:
                raise StoragePermanentError("O backend remoto não confirmou o download parcial.")
            if response.status_code == 206 and not response.headers.get("Content-Range"):
                raise StoragePermanentError("Resposta parcial remota inválida.")
            try:
                length = int(response.headers["Content-Length"])
            except (KeyError, ValueError):
                length = None
            return StorageDownload(
                status_code=response.status_code,
                chunks=response.aiter_bytes(
                    chunk_size=self.settings.remote_download_chunk_size_mb * 1024 * 1024,
                ),
                content_length=length,
                content_range=response.headers.get("Content-Range"),
                content_type=response.headers.get("Content-Type", "application/octet-stream"),
                accept_ranges=response.headers.get("Accept-Ranges", "bytes"),
                _close=close,
            )
        except httpx.HTTPError as exc:
            await close()
            raise StorageTemporaryError("Download remoto temporariamente indisponível.") from exc
        except Exception:
            await close()
            raise

    def _managed_parents(self) -> set[str]:
        return {
            self.settings.google_drive_root_folder_id,
            self.settings.google_drive_videos_folder_id,
            self.settings.google_drive_gifs_folder_id,
            *self._system_folders.values(),
        } - {""}

    async def _is_managed_parent(self, parent_id: str, depth: int = 0) -> bool:
        if parent_id in self._managed_parents():
            return True
        cached = self._managed_parent_cache.get(parent_id)
        if cached and time.monotonic() < cached[1]:
            return cached[0]
        if depth >= 4:
            return False
        try:
            metadata = await self._file_metadata(parent_id)
        except StorageNotFoundError:
            managed = False
            self._cache_managed_parent(parent_id, managed)
            return managed
        if (metadata.get("appProperties") or {}).get("xardManaged") != "1":
            managed = False
            self._cache_managed_parent(parent_id, managed)
            return managed
        parents = metadata.get("parents") or []
        for parent in parents:
            if await self._is_managed_parent(str(parent), depth + 1):
                self._cache_managed_parent(parent_id, True)
                return True
        self._cache_managed_parent(parent_id, False)
        return False

    def _cache_managed_parent(self, parent_id: str, managed: bool) -> None:
        if len(self._managed_parent_cache) >= 256:
            self._managed_parent_cache.pop(next(iter(self._managed_parent_cache)))
        self._managed_parent_cache[parent_id] = (managed, time.monotonic() + 300)

    async def _has_managed_parent(self, parents: list[str]) -> bool:
        for parent in parents:
            if await self._is_managed_parent(str(parent)):
                return True
        return False

    async def delete_file(self, key: str) -> None:
        try:
            metadata = await self._file_metadata(key)
        except StorageNotFoundError:
            return
        parents = metadata.get("parents") or []
        managed = (
            (metadata.get("appProperties") or {}).get("xardManaged") == "1"
            and await self._has_managed_parent(parents)
        )
        if not managed:
            raise StorageSecurityError("Recusa ao excluir arquivo fora das pastas administradas.")
        try:
            if self.settings.google_drive_permanent_delete_expired:
                await self._request(
                    "DELETE",
                    f"{_API}/files/{quote(key, safe='')}",
                    params={"supportsAllDrives": "true"},
                    _max_retries=self.settings.remote_delete_max_retries,
                )
            else:
                await self._request(
                    "PATCH",
                    f"{_API}/files/{quote(key, safe='')}",
                    params={"supportsAllDrives": "true", "fields": "id,trashed"},
                    json={"trashed": True},
                    _max_retries=self.settings.remote_delete_max_retries,
                )
        except StorageNotFoundError:
            return

    async def file_exists(self, key: str) -> bool:
        try:
            metadata = await self._file_metadata(key)
            return not bool(metadata.get("trashed"))
        except StorageNotFoundError:
            return False

    async def get_metadata(self, key: str) -> StorageMetadata:
        payload = await self._file_metadata(key)
        if payload.get("trashed"):
            raise StorageNotFoundError("Arquivo remoto não encontrado.")
        return self._metadata_from_payload(payload)

    async def audit_private_file(self, key: str) -> None:
        payload = await self._file_metadata(key)
        parents = payload.get("parents") or []
        if (
            (payload.get("appProperties") or {}).get("xardManaged") != "1"
            or not await self._has_managed_parent(parents)
        ):
            raise StorageSecurityError("Arquivo remoto fora das pastas administradas.")
        await self._assert_private(key)

    async def list_managed_files(
        self,
        *,
        page_token: str | None = None,
        page_size: int = 100,
    ) -> tuple[list[dict], str | None]:
        """Lista somente blobs marcados pelo sistema, com paginação limitada."""
        params = {
            "q": (
                "trashed = false and mimeType != 'application/vnd.google-apps.folder' and "
                "appProperties has { key='xardManaged' and value='1' }"
            ),
            "spaces": "drive",
            "pageSize": str(min(max(page_size, 1), 200)),
            "fields": (
                "nextPageToken,files(id,size,mimeType,parents,createdTime,trashed,appProperties)"
            ),
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        if self.settings.google_drive_shared_drive_id:
            params.update({
                "corpora": "drive",
                "driveId": self.settings.google_drive_shared_drive_id,
            })
        response = await self._request("GET", f"{_API}/files", params=params)
        try:
            payload = response.json()
            return list(payload.get("files") or []), payload.get("nextPageToken")
        except ValueError as exc:
            raise StoragePermanentError("Listagem de arquivos gerenciados inválida.") from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
