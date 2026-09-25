from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from backend.api.config import Settings
from backend.database.models import Download, DownloadStatus, StorageState
from backend.downloads import router as downloads_router
from backend.storage import service as storage_service
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
from backend.storage.google_drive import GoogleDriveStorageBackend
from backend.storage.local import LocalStorageBackend


ROOT_ID = "rootfolder123"
VIDEOS_ID = "videosfolder123"
GIFS_ID = "gifsfolder123"
REMOTE_ID = "remotefile123"


def drive_settings(**overrides) -> Settings:
    values = {
        "jwt_secret": "x" * 64,
        "environment": "development",
        "storage_backend": "google_drive",
        "google_drive_auth_mode": "oauth",
        "google_drive_client_id": "client",
        "google_drive_client_secret": "secret",
        "google_drive_refresh_token": "refresh",
        "google_drive_root_folder_id": ROOT_ID,
        "google_drive_videos_folder_id": VIDEOS_ID,
        "google_drive_gifs_folder_id": GIFS_ID,
        "google_drive_upload_chunk_size_mb": 1,
        "remote_upload_max_retries": 0,
    }
    values.update(overrides)
    return Settings(**values)


async def configured_backend(monkeypatch, handler, **settings_overrides):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = GoogleDriveStorageBackend(drive_settings(**settings_overrides), client=client)

    async def token(*, force_refresh=False):
        del force_refresh
        return "access-token"

    async def partition(_kind):
        return GIFS_ID

    async def private(_key):
        return None

    async def capacity(_size):
        return None

    monkeypatch.setattr(backend, "_access_token", token)
    monkeypatch.setattr(backend, "_partition_parent", partition)
    monkeypatch.setattr(backend, "_assert_private", private)
    monkeypatch.setattr(backend, "_ensure_upload_capacity", capacity)
    return backend, client


def test_upload_retomavel_em_blocos_e_confirma_checksum(tmp_path, monkeypatch):
    content = b"a" * (2 * 1024 * 1024 + 17)
    source = tmp_path / "result.gif"
    source.write_bytes(content)
    md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
    chunks: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        if request.method == "POST":
            return httpx.Response(
                200, headers={"Location": "https://www.googleapis.com/upload/session123"},
            )
        chunks.append(request.content)
        return httpx.Response(
            200 if sum(map(len, chunks)) == len(content) else 308,
            headers={} if sum(map(len, chunks)) == len(content) else {
                "Range": f"bytes=0-{sum(map(len, chunks)) - 1}",
            },
            json={
                "id": REMOTE_ID,
                "size": str(len(content)),
                "md5Checksum": md5,
                "mimeType": "image/gif",
                "parents": [GIFS_ID],
                "createdTime": "2026-07-15T12:00:00Z",
            } if sum(map(len, chunks)) == len(content) else None,
        )

    async def run():
        backend, client = await configured_backend(monkeypatch, handler)
        try:
            return await backend.upload_file(
                source, kind="gif", idempotency_key=f"{uuid.uuid4()}:gif:1",
                mime_type="image/gif",
            )
        finally:
            await client.aclose()

    metadata = asyncio.run(run())
    assert [len(chunk) for chunk in chunks] == [1024 * 1024, 1024 * 1024, 17]
    assert metadata.key == REMOTE_ID
    assert metadata.size == len(content)
    assert metadata.checksum == f"md5:{md5}"


def test_retry_idempotente_reutiliza_objeto_existente(tmp_path, monkeypatch):
    source = tmp_path / "result.mp4"
    source.write_bytes(b"video")
    md5 = hashlib.md5(b"video", usedforsecurity=False).hexdigest()
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, json={"files": [{
            "id": REMOTE_ID, "size": "5", "md5Checksum": md5,
            "mimeType": "video/mp4", "parents": [GIFS_ID],
            "createdTime": "2026-07-15T12:00:00Z",
        }]})

    async def run():
        backend, client = await configured_backend(monkeypatch, handler)
        try:
            return await backend.upload_file(
                source, kind="video", idempotency_key=f"{uuid.uuid4()}:video:3",
                mime_type="video/mp4",
            )
        finally:
            await client.aclose()

    assert asyncio.run(run()).key == REMOTE_ID
    assert methods == ["GET"]


def test_objeto_idempotente_divergente_e_bloqueado(tmp_path, monkeypatch):
    source = tmp_path / "result.gif"
    source.write_bytes(b"correct")

    async def handler(_request):
        return httpx.Response(200, json={"files": [{
            "id": REMOTE_ID, "size": "999", "md5Checksum": "bad",
            "mimeType": "image/gif", "parents": [GIFS_ID],
        }]})

    async def run():
        backend, client = await configured_backend(monkeypatch, handler)
        try:
            with pytest.raises(StorageSecurityError):
                await backend.upload_file(
                    source, kind="gif", idempotency_key=f"{uuid.uuid4()}:gif:1",
                    mime_type="image/gif",
                )
        finally:
            await client.aclose()

    asyncio.run(run())


def test_duplicatas_idempotentes_iguais_convergem_sem_exclusao(monkeypatch):
    md5 = hashlib.md5(b"same", usedforsecurity=False).hexdigest()

    async def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"files": [
            {"id": "remotefile999", "size": "4", "md5Checksum": md5,
             "mimeType": "video/mp4", "parents": [GIFS_ID]},
            {"id": "remotefile111", "size": "4", "md5Checksum": md5,
             "mimeType": "video/mp4", "parents": [GIFS_ID]},
        ]})

    async def run():
        backend, client = await configured_backend(monkeypatch, handler)
        try:
            return await backend._find_existing(GIFS_ID, f"{uuid.uuid4()}:video:1")
        finally:
            await client.aclose()

    assert asyncio.run(run())["id"] == "remotefile111"


def test_id_drive_reservado_e_enviado_na_criacao(tmp_path, monkeypatch):
    source = tmp_path / "result.gif"
    source.write_bytes(b"gif")
    md5 = hashlib.md5(b"gif", usedforsecurity=False).hexdigest()
    reserved = "reservedfile123"

    async def handler(request):
        if request.method == "GET":
            if request.url.path.endswith(f"/{reserved}"):
                return httpx.Response(404)
            return httpx.Response(200, json={"files": []})
        if request.method == "POST":
            assert json.loads(request.content)["id"] == reserved
            return httpx.Response(
                200, headers={"Location": "https://www.googleapis.com/upload/session123"},
            )
        return httpx.Response(200, json={
            "id": reserved, "size": "3", "md5Checksum": md5,
            "mimeType": "image/gif", "parents": [GIFS_ID],
        })

    async def run():
        backend, client = await configured_backend(monkeypatch, handler)
        try:
            return await backend.upload_file(
                source, kind="gif", idempotency_key=f"{uuid.uuid4()}:gif:1",
                mime_type="image/gif", reserved_key=reserved,
            )
        finally:
            await client.aclose()

    assert asyncio.run(run()).key == reserved


def test_upload_cancelado_nao_envia_blocos(tmp_path, monkeypatch):
    source = tmp_path / "result.gif"
    source.write_bytes(b"data")
    puts = 0

    async def handler(request):
        nonlocal puts
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        if request.method == "POST":
            return httpx.Response(200, headers={
                "Location": "https://www.googleapis.com/upload/session123",
            })
        puts += 1
        return httpx.Response(500)

    async def run():
        backend, client = await configured_backend(monkeypatch, handler)
        try:
            with pytest.raises(StoragePermanentError, match="cancelado"):
                await backend.upload_file(
                    source, kind="gif", idempotency_key=f"{uuid.uuid4()}:gif:1",
                    mime_type="image/gif", cancel_requested=lambda: True,
                )
        finally:
            await client.aclose()

    asyncio.run(run())
    assert puts == 0


def test_timeout_consulta_sessao_e_retoma_do_byte_confirmado(tmp_path, monkeypatch):
    source = tmp_path / "result.gif"
    source.write_bytes(b"abcd")
    md5 = hashlib.md5(b"abcd", usedforsecurity=False).hexdigest()
    ranges: list[str] = []
    first_data_request = True

    async def handler(request):
        nonlocal first_data_request
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        if request.method == "POST":
            return httpx.Response(200, headers={
                "Location": "https://www.googleapis.com/upload/session123",
            })
        ranges.append(request.headers.get("Content-Range", ""))
        if request.content and first_data_request:
            first_data_request = False
            raise httpx.ReadTimeout("lost response", request=request)
        if not request.content:
            return httpx.Response(308, headers={"Range": "bytes=0-1"})
        return httpx.Response(200, json={
            "id": REMOTE_ID, "size": "4", "md5Checksum": md5,
            "mimeType": "image/gif", "parents": [GIFS_ID],
            "createdTime": "2026-07-15T12:00:00Z",
        })

    async def run():
        backend, client = await configured_backend(monkeypatch, handler)
        try:
            return await backend.upload_file(
                source, kind="gif", idempotency_key=f"{uuid.uuid4()}:gif:1",
                mime_type="image/gif",
            )
        finally:
            await client.aclose()

    assert asyncio.run(run()).size == 4
    assert ranges == ["bytes 0-3/4", "bytes */4", "bytes 2-3/4"]


@pytest.mark.parametrize(
    ("status_code", "payload", "error_type"),
    [
        (429, {}, StorageTemporaryError),
        (503, {}, StorageTemporaryError),
        (403, {"error": {"errors": [{"reason": "storageQuotaExceeded"}]}}, StorageTemporaryError),
        (401, {}, StoragePermanentError),
    ],
)
def test_classificacao_de_falhas_remotas(monkeypatch, status_code, payload, error_type):
    async def handler(_request):
        return httpx.Response(status_code, json=payload)

    async def run():
        backend, client = await configured_backend(monkeypatch, handler)
        try:
            with pytest.raises(error_type):
                await backend._request("GET", "https://www.googleapis.com/drive/v3/files")
        finally:
            await client.aclose()

    asyncio.run(run())


def test_timeout_de_conexao_e_temporario(monkeypatch):
    async def handler(request):
        raise httpx.ConnectTimeout("timeout", request=request)

    async def run():
        backend, client = await configured_backend(monkeypatch, handler)
        try:
            with pytest.raises(StorageTemporaryError):
                await backend._request("GET", "https://www.googleapis.com/drive/v3/files")
        finally:
            await client.aclose()

    asyncio.run(run())


def test_oauth_503_e_falha_temporaria():
    async def handler(_request):
        return httpx.Response(503)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        backend = GoogleDriveStorageBackend(drive_settings(), client=client)
        try:
            with pytest.raises(StorageTemporaryError):
                await backend._access_token(force_refresh=True)
        finally:
            await client.aclose()

    asyncio.run(run())


def test_oauth_revogado_pede_reconexao_sem_expor_resposta():
    async def handler(_request):
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "secret"})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        backend = GoogleDriveStorageBackend(drive_settings(), client=client)
        try:
            with pytest.raises(StoragePermanentError, match="reconecte") as error:
                await backend._access_token(force_refresh=True)
            assert "secret" not in str(error.value)
        finally:
            await client.aclose()

    asyncio.run(run())


def test_permissao_publica_e_tratada_como_falha_de_seguranca(monkeypatch):
    async def handler(_request):
        return httpx.Response(200, json={
            "permissions": [{"id": "permission1", "type": "anyone", "role": "reader"}],
        })

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        backend = GoogleDriveStorageBackend(drive_settings(), client=client)

        async def token(*, force_refresh=False):
            del force_refresh
            return "token"

        monkeypatch.setattr(backend, "_access_token", token)
        try:
            with pytest.raises(StorageSecurityError):
                await backend._assert_private(REMOTE_ID)
        finally:
            await client.aclose()

    asyncio.run(run())


def test_download_range_e_streaming(monkeypatch):
    async def handler(request):
        assert request.headers["Range"] == "bytes=2-4"
        return httpx.Response(
            206,
            content=b"cde",
            headers={
                "Content-Range": "bytes 2-4/10",
                "Content-Length": "3",
                "Content-Type": "application/octet-stream",
            },
        )

    async def run():
        backend, client = await configured_backend(monkeypatch, handler)
        try:
            stream = await backend.open_download_stream(REMOTE_ID, byte_range="bytes=2-4")
            body = b"".join([chunk async for chunk in stream.chunks])
            await stream.aclose()
            return stream, body
        finally:
            await client.aclose()

    stream, body = asyncio.run(run())
    assert stream.status_code == 206
    assert stream.content_range == "bytes 2-4/10"
    assert body == b"cde"


def test_download_200_nao_tenta_ler_json_antes_do_stream(monkeypatch):
    class LiveStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"live"

    async def handler(_request):
        return httpx.Response(
            200,
            stream=LiveStream(),
            headers={"Content-Length": "4", "Content-Type": "application/octet-stream"},
        )

    async def run():
        backend, client = await configured_backend(monkeypatch, handler)
        try:
            stream = await backend.open_download_stream(REMOTE_ID)
            try:
                return b"".join([chunk async for chunk in stream.chunks])
            finally:
                await stream.aclose()
        finally:
            await client.aclose()

    assert asyncio.run(run()) == b"live"


def test_download_401_renova_token_antes_do_stream(monkeypatch):
    requests = 0
    refresh_flags: list[bool] = []

    async def handler(_request):
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(401)
        return httpx.Response(200, content=b"ok", headers={"Content-Length": "2"})

    async def run():
        backend, client = await configured_backend(monkeypatch, handler)

        async def token(*, force_refresh=False):
            refresh_flags.append(force_refresh)
            return "refreshed" if force_refresh else "old"

        monkeypatch.setattr(backend, "_access_token", token)
        try:
            stream = await backend.open_download_stream(REMOTE_ID)
            body = b"".join([chunk async for chunk in stream.chunks])
            await stream.aclose()
            return body
        finally:
            await client.aclose()

    assert asyncio.run(run()) == b"ok"
    assert refresh_flags == [False, True]


def test_range_ignorado_pelo_remoto_nao_finge_retomada(monkeypatch):
    async def handler(_request):
        return httpx.Response(200, content=b"all")

    async def run():
        backend, client = await configured_backend(monkeypatch, handler)
        try:
            with pytest.raises(StoragePermanentError, match="parcial"):
                await backend.open_download_stream(REMOTE_ID, byte_range="bytes=2-")
            assert backend._download_slots._value == backend.settings.google_drive_max_concurrent_downloads
        finally:
            await client.aclose()

    asyncio.run(run())


def test_download_aceita_suffix_range(monkeypatch):
    async def handler(request):
        assert request.headers["Range"] == "bytes=-3"
        return httpx.Response(
            206,
            content=b"hij",
            headers={"Content-Range": "bytes 7-9/10", "Content-Length": "3"},
        )

    async def run():
        backend, client = await configured_backend(monkeypatch, handler)
        try:
            stream = await backend.open_download_stream(REMOTE_ID, byte_range="bytes=-3")
            body = b"".join([chunk async for chunk in stream.chunks])
            await stream.aclose()
            return body
        finally:
            await client.aclose()

    assert asyncio.run(run()) == b"hij"


def test_range_invalido_e_416_normalizado(monkeypatch):
    async def handler(_request):
        return httpx.Response(416)

    async def run():
        backend, client = await configured_backend(monkeypatch, handler)
        try:
            with pytest.raises(StorageRangeNotSatisfiableError):
                await backend.open_download_stream(REMOTE_ID, byte_range="bytes=20-")
            with pytest.raises(StorageRangeNotSatisfiableError):
                await backend.open_download_stream(REMOTE_ID, byte_range="bytes=0-1,4-5")
        finally:
            await client.aclose()

    asyncio.run(run())


def test_delete_ausente_e_idempotente(monkeypatch):
    async def handler(_request):
        raise AssertionError("não deveria chamar HTTP")

    async def run():
        backend, client = await configured_backend(monkeypatch, handler)

        async def missing(_key):
            raise StorageNotFoundError("ausente")

        monkeypatch.setattr(backend, "_file_metadata", missing)
        try:
            await backend.delete_file(REMOTE_ID)
        finally:
            await client.aclose()

    asyncio.run(run())


def test_delete_fora_das_pastas_gerenciadas_e_bloqueado(monkeypatch):
    async def handler(_request):
        raise AssertionError("não deveria excluir")

    async def run():
        backend, client = await configured_backend(monkeypatch, handler)

        async def metadata(_key):
            return {"id": REMOTE_ID, "parents": ["externalparent"], "appProperties": {}}

        monkeypatch.setattr(backend, "_file_metadata", metadata)
        try:
            with pytest.raises(StorageSecurityError):
                await backend.delete_file(REMOTE_ID)
        finally:
            await client.aclose()

    asyncio.run(run())


def test_configuracao_drive_incompleta_falha_fechado():
    with pytest.raises(ValidationError):
        drive_settings(google_drive_root_folder_id="")


def test_subpastas_drive_podem_ser_criadas_automaticamente():
    settings = drive_settings(
        google_drive_videos_folder_id="",
        google_drive_gifs_folder_id="",
    )
    assert settings.google_drive_root_folder_id == ROOT_ID


def test_criacao_automatica_da_pasta_de_gifs(monkeypatch):
    requests: list[str] = []

    async def handler(request):
        requests.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        return httpx.Response(200, json={
            "id": GIFS_ID,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [ROOT_ID],
            "trashed": False,
            "appProperties": {"xardManaged": "1", "xardSystemFolder": "gifs"},
        })

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        backend = GoogleDriveStorageBackend(
            drive_settings(google_drive_gifs_folder_id=""), client=client,
        )

        async def valid(_key):
            return None

        async def token(*, force_refresh=False):
            del force_refresh
            return "token"

        monkeypatch.setattr(backend, "_validate_parent", valid)
        monkeypatch.setattr(backend, "_access_token", token)
        try:
            return await backend._base_parent_for("gif")
        finally:
            await client.aclose()

    assert asyncio.run(run()) == GIFS_ID
    assert requests == ["GET", "POST"]


def test_pasta_de_gifs_configurada_e_reutilizada(monkeypatch):
    async def handler(_request):
        raise AssertionError("não deveria pesquisar nem criar pasta")

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        backend = GoogleDriveStorageBackend(drive_settings(), client=client)

        async def valid(_key):
            return None

        async def metadata(_key):
            return {"id": GIFS_ID, "parents": [ROOT_ID]}

        monkeypatch.setattr(backend, "_validate_parent", valid)
        monkeypatch.setattr(backend, "_file_metadata", metadata)
        try:
            return await backend._base_parent_for("gif")
        finally:
            await client.aclose()

    assert asyncio.run(run()) == GIFS_ID


def test_quota_impede_upload_que_viola_espaco_minimo(monkeypatch):
    async def handler(request):
        assert request.url.path.endswith("/about")
        return httpx.Response(200, json={
            "storageQuota": {
                "limit": str(10 * 1024 * 1024),
                "usage": str(8 * 1024 * 1024),
                "usageInDrive": str(8 * 1024 * 1024),
                "usageInDriveTrash": "0",
            },
            "maxUploadSize": str(100 * 1024 * 1024),
        })

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        backend = GoogleDriveStorageBackend(
            drive_settings(google_drive_min_free_space_mb=1), client=client,
        )

        async def token(*, force_refresh=False):
            del force_refresh
            return "token"

        monkeypatch.setattr(backend, "_access_token", token)
        try:
            with pytest.raises(StorageTemporaryError, match="Espaço livre"):
                await backend._ensure_upload_capacity(2 * 1024 * 1024)
        finally:
            await client.aclose()

    asyncio.run(run())


def test_exclusao_configurada_para_lixeira_nao_usa_delete(monkeypatch):
    calls: list[tuple[str, dict | None]] = []

    async def handler(_request):
        raise AssertionError("request direto foi substituído")

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        backend = GoogleDriveStorageBackend(
            drive_settings(google_drive_permanent_delete_expired=False), client=client,
        )

        async def metadata(_key):
            return {
                "id": REMOTE_ID,
                "parents": [GIFS_ID],
                "mimeType": "image/gif",
                "appProperties": {"xardManaged": "1"},
            }

        async def request(method, _url, **kwargs):
            calls.append((method, kwargs.get("json")))
            return httpx.Response(200, json={"id": REMOTE_ID, "trashed": True})

        monkeypatch.setattr(backend, "_file_metadata", metadata)
        monkeypatch.setattr(backend, "_request", request)
        try:
            await backend.delete_file(REMOTE_ID)
        finally:
            await client.aclose()

    asyncio.run(run())
    assert calls == [("PATCH", {"trashed": True})]


def test_segredos_nao_aparecem_no_repr_da_configuracao():
    settings = drive_settings(
        google_drive_client_secret="client-secret-value",
        google_drive_refresh_token="refresh-secret-value",
    )
    rendered = repr(settings)
    assert "client-secret-value" not in rendered
    assert "refresh-secret-value" not in rendered


def test_backend_local_streama_range_sem_sair_da_raiz(tmp_path):
    root = tmp_path / "storage"
    root.mkdir()
    path = root / "file.bin"
    path.write_bytes(b"0123456789")

    async def run():
        backend = LocalStorageBackend(root, chunk_size=2)
        stream = await backend.open_download_stream(str(path), byte_range="bytes=3-6")
        return stream, b"".join([chunk async for chunk in stream.chunks])

    stream, body = asyncio.run(run())
    assert stream.status_code == 206
    assert stream.content_range == "bytes 3-6/10"
    assert body == b"3456"


def test_backend_local_rejeita_arquivo_externo(tmp_path):
    root = tmp_path / "storage"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret")
    backend = LocalStorageBackend(root)
    with pytest.raises(StoragePermanentError):
        asyncio.run(backend.open_download_stream(str(outside)))


def test_backend_local_aceita_suffix_range(tmp_path):
    root = tmp_path / "storage"
    root.mkdir()
    path = root / "file.bin"
    path.write_bytes(b"0123456789")

    async def run():
        stream = await LocalStorageBackend(root).open_download_stream(
            str(path), byte_range="bytes=-3",
        )
        return stream, b"".join([chunk async for chunk in stream.chunks])

    stream, body = asyncio.run(run())
    assert stream.content_range == "bytes 7-9/10"
    assert body == b"789"


def test_desconexao_fecha_stream_remoto(monkeypatch):
    closed = False

    async def chunks():
        yield b"first"
        yield b"second"

    async def close():
        nonlocal closed
        closed = True

    class Backend:
        async def open_download_stream(self, _key, *, byte_range=None):
            del byte_range
            return StorageDownload(
                status_code=200, chunks=chunks(), content_length=11, _close=close,
            )

    monkeypatch.setattr(downloads_router, "get_api_storage_backend", lambda _name: Backend())
    record = download_record()
    record.storage_backend = "google_drive"
    record.storage_key = REMOTE_ID
    record.remote_mime_type = "application/octet-stream"
    record.title = "arquivo"

    async def run():
        response = await downloads_router._remote_delivery_response(record, None)
        iterator = response.body_iterator
        assert await anext(iterator) == b"first"
        await iterator.aclose()

    asyncio.run(run())
    assert closed


def test_range_multiplo_retorna_416_sem_chamar_drive(monkeypatch):
    monkeypatch.setattr(
        downloads_router, "get_api_storage_backend",
        lambda _name: (_ for _ in ()).throw(AssertionError("não deveria abrir backend")),
    )
    record = download_record()
    record.storage_backend = "google_drive"
    record.storage_key = REMOTE_ID
    record.remote_size = 10

    async def run():
        with pytest.raises(Exception) as caught:
            await downloads_router._remote_delivery_response(record, "bytes=0-1,4-5")
        return caught.value

    error = asyncio.run(run())
    assert error.status_code == 416
    assert error.headers["Content-Range"] == "bytes */10"


class FakeDb:
    def __init__(self, *, fail_commit: int | None = None):
        self.commits: list[tuple[str | None, str | None, bool | None]] = []
        self.fail_commit = fail_commit
        self.download: Download | None = None

    def commit(self):
        if self.download is not None:
            self.commits.append((
                self.download.storage_backend,
                self.download.storage_state,
                self.download.local_delete_pending,
            ))
        if self.fail_commit == len(self.commits):
            raise RuntimeError("database unavailable")

    def execute(self, _statement):
        return SimpleNamespace(
            rowcount=1,
            scalar_one=lambda: 0,
            scalar_one_or_none=lambda: "windows",
        )


def test_slot_global_usa_lock_de_sessao_e_libera():
    calls: list[str] = []

    class Connection:
        def execution_options(self, **_kwargs):
            return self

        def execute(self, statement, _params):
            calls.append(str(statement))
            return SimpleNamespace(scalar_one=lambda: True)

        def close(self):
            calls.append("close")

    class Engine:
        dialect = SimpleNamespace(name="postgresql")

        def connect(self):
            return Connection()

    class Db:
        def get_bind(self):
            return Engine()

    with storage_service._upload_slot(Db(), uuid.uuid4(), 2):
        calls.append("upload")

    assert "SELECT pg_try_advisory_lock" in calls[0]
    assert calls[-2:] == ["SELECT pg_advisory_unlock(:lock_key)", "close"]


class FakeBackend:
    name = "google_drive"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.upload_path_exists = False
        self.deleted: list[str] = []

    async def upload_file(self, local_path, **_kwargs):
        self.upload_path_exists = local_path.is_file()
        if self.error:
            raise self.error
        return self.result

    async def reserve_file_id(self):
        return "reservedfile123"

    async def delete_file(self, key):
        self.deleted.append(key)

    async def close(self):
        return None


def storage_settings(**overrides):
    values = {
        "storage_backend": "google_drive",
        "download_file_ttl_hours": 6,
        "storage_allow_local_fallback": True,
        "storage_local_fallback_max_size_mb": 1024,
        "storage_recovery_ttl_hours": 24,
        "storage_keep_generated_gifs": True,
        "storage_keep_downloaded_videos": False,
        "storage_keep_source_videos": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def download_record() -> Download:
    return Download(
        id=uuid.uuid4(), device_id=uuid.uuid4(), url="https://example.test/video",
        mode="gif", output_format="gif", revision=1,
        status=DownloadStatus.processing, storage_backend="local",
        storage_state=StorageState.local_ready.value,
    )


def test_banco_confirma_remoto_antes_de_excluir_local(tmp_path, monkeypatch):
    path = tmp_path / "output.gif"
    path.write_bytes(b"gif")
    metadata = StorageMetadata(
        backend="google_drive", key=REMOTE_ID, size=3, checksum="md5:x",
        mime_type="image/gif", parent_id=GIFS_ID, uploaded_at=datetime.now(timezone.utc),
    )
    backend = FakeBackend(result=metadata)
    db = FakeDb()
    record = download_record()
    db.download = record
    monkeypatch.setattr(storage_service, "get_settings", lambda: storage_settings())
    monkeypatch.setattr(storage_service, "create_storage_backend", lambda **_kwargs: backend)

    storage_service.persist_completed_output(db, record, path)

    assert backend.upload_path_exists
    assert not path.exists()
    assert [state for _, state, _ in db.commits] == [
        StorageState.remote_uploading.value,
        StorageState.remote_ready.value,
        StorageState.completed.value,
    ]
    assert record.file_path is None
    assert record.storage_key == REMOTE_ID


def test_falha_do_banco_apos_upload_preserva_arquivo_local(tmp_path, monkeypatch):
    path = tmp_path / "output.gif"
    path.write_bytes(b"gif")
    metadata = StorageMetadata(
        backend="google_drive", key=REMOTE_ID, size=3, checksum=None,
        mime_type="image/gif", parent_id=GIFS_ID, uploaded_at=datetime.now(timezone.utc),
    )
    backend = FakeBackend(result=metadata)
    db = FakeDb(fail_commit=2)
    record = download_record()
    db.download = record
    monkeypatch.setattr(storage_service, "get_settings", lambda: storage_settings())
    monkeypatch.setattr(storage_service, "create_storage_backend", lambda **_kwargs: backend)

    with pytest.raises(RuntimeError, match="database"):
        storage_service.persist_completed_output(db, record, path)

    assert path.exists()
    assert record.local_delete_pending is True


def test_recovery_conclui_estado_remoto_sem_arquivo_local(monkeypatch):
    from backend.workers import tasks as worker_tasks

    record = download_record()
    record.storage_backend = "google_drive"
    record.storage_state = StorageState.completed.value
    record.storage_key = REMOTE_ID
    record.remote_size = 3
    record.file_path = None
    record.status = DownloadStatus.processing
    db = FakeDb()
    db.download = record
    monkeypatch.setattr(worker_tasks, "publish_download_event", lambda *_args: None)
    monkeypatch.setattr(worker_tasks, "_clear_progress", lambda *_args: None)

    assert worker_tasks._resume_pending_storage(db, record, str(record.id))
    assert record.status == DownloadStatus.completed
    assert record.storage_key == REMOTE_ID


def test_fallback_local_limitado_preserva_arquivo(tmp_path, monkeypatch):
    path = tmp_path / "output.gif"
    path.write_bytes(b"gif")
    backend = FakeBackend(error=StorageTemporaryError("unavailable"))
    db = FakeDb()
    record = download_record()
    db.download = record
    monkeypatch.setattr(storage_service, "get_settings", lambda: storage_settings())
    monkeypatch.setattr(storage_service, "create_storage_backend", lambda **_kwargs: backend)

    metadata = storage_service.persist_completed_output(db, record, path)

    assert metadata.backend == "local"
    assert path.exists()
    assert record.storage_state == StorageState.local_fallback.value
    assert record.storage_error == "StorageTemporaryError"


def test_oauth_revogado_preserva_fallback_e_suspende_retry_automatico(tmp_path, monkeypatch):
    path = tmp_path / "output.gif"
    path.write_bytes(b"gif")
    backend = FakeBackend(error=StorageAuthorizationError("reconecte"))
    db = FakeDb()
    record = download_record()
    db.download = record
    monkeypatch.setattr(storage_service, "get_settings", lambda: storage_settings())
    monkeypatch.setattr(storage_service, "create_storage_backend", lambda **_kwargs: backend)

    with pytest.raises(StorageAuthorizationError):
        storage_service.persist_completed_output(db, record, path)

    assert path.is_file()
    assert record.storage_backend == "local"
    assert record.storage_state == StorageState.local_fallback.value
    assert record.storage_error == "StorageAuthorizationError"
    assert record.expires_at is not None


def test_fallback_acima_do_limite_nao_marca_sucesso(tmp_path, monkeypatch):
    path = tmp_path / "output.gif"
    path.write_bytes(b"gif")
    backend = FakeBackend(error=StorageTemporaryError("unavailable"))
    db = FakeDb()
    db.execute = lambda _statement: SimpleNamespace(scalar_one=lambda: 2 * 1024 * 1024)
    record = download_record()
    db.download = record
    monkeypatch.setattr(
        storage_service, "get_settings",
        lambda: storage_settings(storage_local_fallback_max_size_mb=1),
    )
    monkeypatch.setattr(storage_service, "create_storage_backend", lambda **_kwargs: backend)

    with pytest.raises(StorageTemporaryError):
        storage_service.persist_completed_output(db, record, path)
    assert path.exists()
    assert record.storage_state == StorageState.remote_uploading.value


def test_politica_pode_manter_gif_final_no_backend_local(tmp_path, monkeypatch):
    path = tmp_path / "output.gif"
    path.write_bytes(b"gif")
    db = FakeDb()
    record = download_record()
    db.download = record
    monkeypatch.setattr(
        storage_service, "get_settings",
        lambda: storage_settings(storage_keep_generated_gifs=False),
    )

    metadata = storage_service.persist_completed_output(db, record, path)

    assert metadata.backend == "local"
    assert path.exists()
    assert record.storage_backend == "local"


def test_politica_de_fonte_enviada_preserva_metadados_privados(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    metadata = StorageMetadata(
        backend="google_drive", key=REMOTE_ID, size=5, checksum="md5:x",
        mime_type="video/mp4", parent_id=VIDEOS_ID, uploaded_at=datetime.now(timezone.utc),
    )
    backend = FakeBackend(result=metadata)
    db = FakeDb()
    record = download_record()
    db.download = record
    monkeypatch.setattr(
        storage_service, "get_settings",
        lambda: storage_settings(storage_keep_source_videos=True),
    )
    monkeypatch.setattr(storage_service, "create_storage_backend", lambda **_kwargs: backend)

    result = storage_service.persist_source_video(
        db, record, source, downloaded_source=False,
    )

    assert result is metadata
    assert source.exists()
    assert record.source_storage_key == REMOTE_ID
    assert record.source_remote_size == 5


@pytest.mark.skipif(
    os.environ.get("GOOGLE_DRIVE_INTEGRATION_TEST") != "1",
    reason="requer credenciais e pastas de teste explicitamente configuradas",
)
def test_integracao_opcional_valida_upload_download_e_exclusao(tmp_path):
    source = tmp_path / "integration.gif"
    content = b"xard-google-drive-private-integration"
    source.write_bytes(content)

    async def run():
        backend = GoogleDriveStorageBackend(Settings())
        remote_key = None
        try:
            await backend._validate_parent(backend.settings.google_drive_root_folder_id)
            await backend.storage_status(force_refresh=True)
            reserved_key = await backend.reserve_file_id()
            metadata = await backend.upload_file(
                source,
                kind="gif",
                idempotency_key=f"{uuid.uuid4()}:integration:1",
                mime_type="image/gif",
                reserved_key=reserved_key,
            )
            remote_key = metadata.key
            assert metadata.size == len(content)
            await backend.audit_private_file(remote_key)
            stream = await backend.open_download_stream(remote_key)
            try:
                downloaded = b"".join([chunk async for chunk in stream.chunks])
            finally:
                await stream.aclose()
            assert downloaded == content
            await backend.delete_file(remote_key)
            assert not await backend.file_exists(remote_key)
            remote_key = None
        finally:
            if remote_key:
                await backend.delete_file(remote_key)
            await backend.close()

    asyncio.run(run())
