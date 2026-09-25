"""Contratos do cache persistente de mídia e da proteção de recursos."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.database.models import (
    Device,
    Download,
    DownloadStatus,
    MediaCache,
    StorageState,
)
from backend.database.session import Base
from backend.downloads import cache
from backend.storage import service as storage_service
from backend.storage.base import StorageMetadata, StorageNotFoundError
from backend.workers import tasks


def _settings(**overrides):
    values = {
        "media_cache_enabled": True,
        "storage_backend": "google_drive",
        "storage_keep_generated_gifs": True,
        "download_file_ttl_hours": 6,
        "media_cache_processing_stale_seconds": 3600,
        "task_max_retries": 3,
        "temp_storage_max_mb": 64,
        "temp_storage_min_free_mb": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _device(db, platform="windows"):
    row = Device(
        id=uuid.uuid4(), device_id=uuid.uuid4().hex,
        installation_id=uuid.uuid4().hex, public_key="test", platform=platform,
        app_version="1.0.0",
    )
    db.add(row)
    db.commit()
    return row


def _download(device, **overrides):
    values = {
        "id": uuid.uuid4(),
        "device_id": device.id,
        "url": "https://www.youtube.com/watch?v=AbCdEf12345",
        "format_spec": "(bv[vcodec^=avc1][height=1080])+(ba[ext=m4a]/ba)",
        "mode": "va",
        "output_format": "mp4",
        "operation_type": "download",
        "status": DownloadStatus.queued,
        "stage": "queued",
        "progress": 0.0,
    }
    values.update(overrides)
    return Download(**values)


def _metadata(key="drive-file", size=1234):
    return StorageMetadata(
        backend="google_drive", key=key, size=size, checksum="md5:test",
        mime_type="video/mp4", parent_id="private-folder",
        uploaded_at=datetime.now(timezone.utc),
    )


def test_identidade_youtube_usa_id_real_em_aliases():
    urls = (
        "https://youtube.com/watch?v=AbCdEf12345&utm_source=x",
        "https://youtu.be/AbCdEf12345?si=share",
        "https://youtube.com/shorts/AbCdEf12345",
    )
    identities = {cache.identify_media(url) for url in urls}
    assert identities == {cache.MediaIdentity("youtube", "AbCdEf12345")}


def test_identidade_x_usa_id_do_status_em_aliases():
    urls = (
        "https://x.com/usuario/status/1234567890?s=20",
        "https://twitter.com/usuario/status/1234567890",
        "https://mobile.twitter.com/usuario/status/1234567890",
    )
    identities = {cache.identify_media(url) for url in urls}
    assert identities == {cache.MediaIdentity("twitter", "1234567890")}


def test_identidade_nao_confunde_host_parecido_com_x():
    # "netflix.com" termina em "x.com" — a checagem não pode ser um simples
    # endswith("x.com"), senão qualquer domínio terminado em "x" cairia aqui.
    identity = cache.identify_media("https://netflix.com/status/1234567890")
    assert identity.platform != "twitter"


def test_cache_key_inclui_todos_os_parametros_de_saida():
    device = SimpleNamespace(id=uuid.uuid4())
    base = _download(device)
    _, _, first, canonical = cache.build_cache_identity(base)

    assert first == cache.build_cache_identity(_download(device))[2]
    assert first != cache.build_cache_identity(_download(device, mode="v"))[2]
    assert first != cache.build_cache_identity(_download(device, output_format="mkv"))[2]
    assert first != cache.build_cache_identity(
        _download(device, format_spec="(bv[vcodec^=avc1][height=720])+(ba[ext=m4a]/ba)"),
    )[2]
    assert first != cache.build_cache_identity(
        _download(device, mode="a", output_format="mp3", format_spec="bestaudio/best"),
    )[2]
    gif_a = _download(
        device, mode="gif", output_format="gif", operation_type="gif_url",
        conversion_options='{"start":0,"end":5,"fps":15}',
    )
    gif_b = _download(
        device, mode="gif", output_format="gif", operation_type="gif_url",
        conversion_options='{"start":0,"end":6,"fps":15}',
    )
    assert cache.build_cache_identity(gif_a)[2] != cache.build_cache_identity(gif_b)[2]
    assert '"profile":"h264-aac-yuv420p-crf18-veryfast-v2"' in canonical


def test_miss_concorrente_reutiliza_um_job_e_hit_nao_processa(db, monkeypatch):
    monkeypatch.setattr(cache, "get_settings", lambda: _settings())
    monkeypatch.setattr(storage_service, "get_remote_metadata", lambda _key: _metadata())
    owner_device = _device(db)
    waiter_device = _device(db)
    hit_device = _device(db)

    owner = _download(owner_device)
    assert cache.reserve_request(db, owner).kind == "owner"
    db.add(owner)
    db.commit()

    waiter = _download(waiter_device)
    assert cache.reserve_request(db, waiter).kind == "wait"
    db.add(waiter)
    db.commit()
    assert waiter.status == DownloadStatus.processing
    assert waiter.cache_role == cache.CACHE_ROLE_WAITER

    owner.status = DownloadStatus.completed
    owner.title = "Vídeo"
    owner.file_size = 1234
    owner.storage_backend = "google_drive"
    owner.storage_state = StorageState.completed.value
    owner.storage_key = "drive-file"
    owner.remote_size = 1234
    owner.remote_mime_type = "video/mp4"
    followers = cache.complete_owner(db, owner)
    db.commit()

    assert [row.id for row in followers] == [waiter.id]
    assert waiter.status == DownloadStatus.completed
    assert waiter.storage_key == "drive-file"
    assert waiter.cache_role == cache.CACHE_ROLE_REUSED

    hit = _download(hit_device)
    assert cache.reserve_request(db, hit).kind == "hit"
    db.add(hit)
    db.commit()
    assert hit.status == DownloadStatus.completed
    assert hit.storage_key == "drive-file"
    entry = db.execute(select(MediaCache)).scalar_one()
    assert entry.external_downloads == 1
    assert entry.hit_count == 2
    assert entry.request_count == 3
    assert entry.bytes_reused == 2468
    assert entry.bytes_downloaded_external == 1234


def test_cache_novo_e_gravado_antes_do_download_que_o_referencia(db, monkeypatch):
    """
    Regressão: `/download/create` morria com ForeignKeyViolation.

    Não há relationship() entre Download e MediaCache, então o unit of work
    ordena os INSERTs por tabela e `downloads` sai antes de `media_cache`. Sem a
    gravação antecipada, o download referencia uma linha que ainda não existe.
    """
    monkeypatch.setattr(cache, "get_settings", lambda: _settings())
    device = _device(db)
    download = _download(device)

    decisao = cache.reserve_request(db, download)

    assert decisao.kind == "owner"
    assert download.media_cache_id is not None
    # Visível na transação antes mesmo de o download entrar na sessão.
    gravado = db.execute(
        select(MediaCache).where(MediaCache.id == download.media_cache_id),
    ).scalar_one_or_none()
    assert gravado is not None
    assert gravado.state == "processing"


def test_arquivo_ausente_invalida_hit_e_cria_novo_dono(db, monkeypatch):
    monkeypatch.setattr(cache, "get_settings", lambda: _settings())
    device = _device(db)
    first = _download(device)
    cache.reserve_request(db, first)
    db.add(first)
    db.commit()
    first.status = DownloadStatus.completed
    first.storage_backend = "google_drive"
    first.storage_state = StorageState.completed.value
    first.storage_key = "missing"
    first.file_size = 50
    cache.complete_owner(db, first)
    db.commit()

    def missing(_key):
        raise StorageNotFoundError("sumiu")

    monkeypatch.setattr(storage_service, "get_remote_metadata", missing)
    replacement = _download(_device(db))
    assert cache.reserve_request(db, replacement).kind == "owner"
    assert replacement.cache_role == cache.CACHE_ROLE_OWNER
    entry = db.get(MediaCache, replacement.media_cache_id)
    assert entry.state == "processing"
    assert entry.storage_key is None


def test_falha_do_dono_finaliza_todos_os_seguidores(db, monkeypatch):
    monkeypatch.setattr(cache, "get_settings", lambda: _settings())
    owner = _download(_device(db))
    cache.reserve_request(db, owner)
    db.add(owner)
    db.commit()
    waiter = _download(_device(db))
    cache.reserve_request(db, waiter)
    db.add(waiter)
    db.commit()

    owner.status = DownloadStatus.failed
    followers = cache.fail_owner(db, owner, RuntimeError("falha externa"))
    db.commit()
    assert len(followers) == 1
    assert waiter.status == DownloadStatus.failed
    assert db.get(MediaCache, owner.media_cache_id).state == "failed"


def test_vinte_pedidos_compartilham_um_download_e_um_upload(db, monkeypatch):
    monkeypatch.setattr(cache, "get_settings", lambda: _settings())
    owner = _download(_device(db))
    assert cache.reserve_request(db, owner).kind == "owner"
    db.add(owner)
    db.commit()
    waiters = []
    for _ in range(19):
        follower = _download(_device(db))
        assert cache.reserve_request(db, follower).kind == "wait"
        db.add(follower)
        db.commit()
        waiters.append(follower)

    owner.status = DownloadStatus.completed
    owner.storage_backend = "google_drive"
    owner.storage_state = StorageState.completed.value
    owner.storage_key = "single-drive-file"
    owner.file_size = 2048
    cache.complete_owner(db, owner)
    db.commit()

    assert db.execute(select(MediaCache)).scalar_one().external_downloads == 1
    assert all(follower.status == DownloadStatus.completed for follower in waiters)
    assert {follower.storage_key for follower in waiters} == {"single-drive-file"}


def test_job_processing_abandonado_volta_para_fila(db, monkeypatch):
    from backend.database import session as session_module
    from backend.tasks import manager as manager_module

    monkeypatch.setattr(cache, "get_settings", lambda: _settings())
    owner = _download(_device(db))
    cache.reserve_request(db, owner)
    db.add(owner)
    db.commit()
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    owner.status = DownloadStatus.processing
    owner.heartbeat_at = stale
    owner.attempts = 1
    entry = db.get(MediaCache, owner.media_cache_id)
    entry.heartbeat_at = stale
    entry.updated_at = stale
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(session_module, "SessionLocal", factory)
    monkeypatch.setattr(manager_module, "notify_task_manager", lambda: None)

    assert cache.recover_stale_entries() == 1
    check = factory()
    try:
        recovered = check.get(Download, owner.id)
        assert recovered.status == DownloadStatus.queued
        assert recovered.stage == "retrying"
    finally:
        check.close()


def test_limpeza_de_historico_nao_apaga_objeto_compartilhado(db, monkeypatch):
    device = _device(db)
    entry = MediaCache(
        id=uuid.uuid4(), cache_key="a" * 64, platform="youtube", media_id="id",
        operation_type="download", request_fingerprint="b" * 64,
        request_parameters="{}", state="ready", mode="va", output_format="mp4",
        storage_backend="google_drive", storage_key="shared-drive-file", file_size=10,
    )
    db.add(entry)
    download = _download(
        device, media_cache_id=entry.id, cache_role=cache.CACHE_ROLE_HIT,
        status=DownloadStatus.completed, storage_backend="google_drive",
        storage_state=StorageState.completed.value, storage_key="shared-drive-file",
    )
    db.add(download)
    db.commit()
    remote_calls = []
    monkeypatch.setattr(
        storage_service, "_operate_remote_keys",
        lambda keys, audit=False: remote_calls.append((keys, audit)) or {},
    )

    storage_service.delete_download_file(db, download)

    assert remote_calls == []
    assert download.storage_key is None
    assert db.get(MediaCache, entry.id).storage_key == "shared-drive-file"


def test_limite_de_disco_temporario_falha_antes_do_worker(tmp_path, monkeypatch):
    payload = tmp_path / "partial.bin"
    payload.write_bytes(b"x" * 1024)
    monkeypatch.setattr(tasks.settings, "downloads_dir", str(tmp_path))
    monkeypatch.setattr(tasks.settings, "temp_storage_max_mb", 1)
    monkeypatch.setattr(tasks.settings, "temp_storage_min_free_mb", 1)
    monkeypatch.setattr(tasks, "_temporary_storage_bytes", lambda _root: 2 * 1024 * 1024)

    with pytest.raises(Exception, match="temporário ocupado"):
        tasks._ensure_temporary_storage_capacity()
