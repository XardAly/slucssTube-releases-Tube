from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.downloads import router
from backend.database.models import DeviceStatus
from backend.api.deps import get_auth_context
from backend.database.session import get_db
from backend.notifications import discord
from backend.downloads.schemas import (
    LocalOperationAuthorizeRequest,
    LocalOperationProgressRequest,
)


class _Db:
    def __init__(self, record=None):
        self.added = None
        self.record = record
        self.commits = 0

    def add(self, value):
        self.added = value

    def commit(self):
        self.commits += 1

    def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self.record)


def _context():
    return SimpleNamespace(
        device=SimpleNamespace(
            id=uuid4(), device_id="desktop-device", platform="windows",
            public_key="public-key",
        ),
        ip="203.0.113.10",
    )


def test_autorizacao_local_cria_registro_separado_e_permit_assinado(monkeypatch):
    db = _Db()
    ctx = _context()
    monkeypatch.setattr(router, "consume_challenge", lambda *_args: True)
    monkeypatch.setattr(router, "server_sign", lambda canonical: hashlib.sha256(canonical).hexdigest())
    data = LocalOperationAuthorizeRequest(
        operation="download",
        url="https://www.youtube.com/watch?v=abcdefghijk",
        mode="va",
        height=1080,
        format_id="137",
        video_format="mp4",
        audio_format="mp3",
        challenge="challenge",
        challenge_signature="signature",
    )

    response = router.authorize_local_operation(data, ctx, db)

    permit = response["permit"]
    assert permit["device_id"] == "desktop-device"
    assert permit["operation"] == "download"
    assert permit["parameters"]["format_id"] == "137"
    assert 0 < permit["expires_at"] - permit["issued_at"] <= 15 * 60
    assert db.added.operation == "download"
    assert db.added.status == "authorized"
    assert db.added.permit_nonce_hash == hashlib.sha256(permit["nonce"].encode()).hexdigest()
    assert db.commits == 1
    assert response["signature"] == hashlib.sha256(
        router._canonical_local_permit(permit)
    ).hexdigest()


def test_schema_rejeita_operacao_local_com_campos_cruzados():
    with pytest.raises(ValidationError):
        LocalOperationAuthorizeRequest(
            operation="gif",
            url="https://youtu.be/abcdefghijk",
            gif_options={},
            challenge="c",
            challenge_signature="s",
        )


def test_autorizacao_upscale_retorna_permit_fechado_sem_dados_do_arquivo(monkeypatch):
    db = _Db()
    ctx = _context()
    ctx.device.status = DeviceStatus.authorized
    ctx.device.app_version = "1.6.0"
    monkeypatch.setattr(
        router, "get_settings",
        lambda: SimpleNamespace(upscale_enabled=True, upscale_minimum_version="1.6.0"),
    )
    monkeypatch.setattr(router, "consume_challenge", lambda *_args: True)
    monkeypatch.setattr(router, "server_sign", lambda canonical: hashlib.sha256(canonical).hexdigest())
    data = LocalOperationAuthorizeRequest(
        operation="upscale",
        upscale_options={
            "media_kind": "video",
            "model": "realesr_animevideov3",
            "scale": 4,
            "output_format": "hevc",
        },
        challenge="challenge",
        challenge_signature="signature",
    )

    response = router.authorize_local_operation(data, ctx, db)

    parameters = response["permit"]["parameters"]
    assert parameters == {
        "media_kind": "video",
        "model": "realesr_animevideov3",
        "scale": 4,
        "output_format": "hevc",
    }
    assert response["permit"]["url"] == ""
    assert db.added.operation == "upscale"
    assert "path" not in db.added.parameters


def test_autorizacao_upscale_android_valida_engine_e_pesos_sem_receber_midia(monkeypatch):
    db = _Db()
    ctx = _context()
    ctx.device.platform = "android"
    ctx.device.device_id = "android-device"
    ctx.device.status = DeviceStatus.authorized
    ctx.device.app_version = "1.2.0"
    monkeypatch.setattr(
        router,
        "get_settings",
        lambda: SimpleNamespace(
            android_upscale_enabled=True,
            android_upscale_minimum_version="1.2.0",
            android_upscale_engine_version="ncnn-20260526-v1",
            android_animevideov3_model_version="animevideov3-ncnn-v1",
            android_realcugan_model_version="realcugan-se-conservative-v1",
        ),
    )
    monkeypatch.setattr(router, "consume_challenge", lambda *_args: True)
    monkeypatch.setattr(router, "server_sign", lambda _canonical: "assinatura")
    data = LocalOperationAuthorizeRequest(
        operation="upscale",
        upscale_options={
            "media_kind": "video",
            "model": "realesr_animevideov3",
            "scale": 2,
            "output_format": "auto",
            "model_version": "animevideov3-ncnn-v1",
            "engine_version": "ncnn-20260526-v1",
        },
        challenge="challenge",
        challenge_signature="signature",
    )

    response = router.authorize_local_operation(data, ctx, db)

    assert response["permit"]["device_id"] == "android-device"
    assert response["permit"]["parameters"] == data.upscale_options.model_dump(mode="json")
    assert set(data.upscale_options.model_dump()) == {
        "media_kind", "model", "scale", "output_format", "model_version", "engine_version",
    }


def test_autorizacao_upscale_android_rejeita_pesos_incompativeis(monkeypatch):
    ctx = _context()
    ctx.device.platform = "android"
    ctx.device.status = DeviceStatus.authorized
    ctx.device.app_version = "1.2.0"
    monkeypatch.setattr(
        router,
        "get_settings",
        lambda: SimpleNamespace(
            android_upscale_enabled=True,
            android_upscale_minimum_version="1.2.0",
            android_upscale_engine_version="ncnn-20260526-v1",
            android_animevideov3_model_version="animevideov3-ncnn-v2",
            android_realcugan_model_version="realcugan-se-conservative-v1",
        ),
    )
    data = LocalOperationAuthorizeRequest(
        operation="upscale",
        upscale_options={
            "media_kind": "video",
            "model": "realesr_animevideov3",
            "scale": 2,
            "output_format": "auto",
            "model_version": "animevideov3-ncnn-v1",
            "engine_version": "ncnn-20260526-v1",
        },
        challenge="challenge",
        challenge_signature="signature",
    )

    with pytest.raises(HTTPException) as error:
        router.authorize_local_operation(data, ctx, _Db())

    assert error.value.status_code == 426


@pytest.mark.parametrize(
    ("enabled", "app_version", "expected_status"),
    [(False, "1.6.0", 403), (True, "1.5.9", 426)],
)
def test_upscale_falha_fechado_quando_desabilitado_ou_desatualizado(
    monkeypatch, enabled, app_version, expected_status,
):
    ctx = _context()
    ctx.device.status = DeviceStatus.authorized
    ctx.device.app_version = app_version
    challenge_consumed = []
    monkeypatch.setattr(
        router, "get_settings",
        lambda: SimpleNamespace(
            upscale_enabled=enabled, upscale_minimum_version="1.6.0",
        ),
    )
    monkeypatch.setattr(
        router, "consume_challenge", lambda *_args: challenge_consumed.append(True),
    )
    data = LocalOperationAuthorizeRequest(
        operation="upscale",
        upscale_options={
            "media_kind": "image", "model": "realcugan",
            "scale": 2, "output_format": "auto",
        },
        challenge="challenge",
        challenge_signature="signature",
    )

    with pytest.raises(HTTPException) as error:
        router.authorize_local_operation(data, ctx, _Db())

    assert error.value.status_code == expected_status
    assert challenge_consumed == []


def test_schema_upscale_rejeita_modelo_escala_formato_e_campos_extras():
    base = {
        "operation": "upscale",
        "upscale_options": {
            "media_kind": "video", "model": "realesr_animevideov3",
            "scale": 2, "output_format": "auto",
        },
        "challenge": "c",
        "challenge_signature": "s",
    }
    for field, invalid in (
        ("model", "anime4k"), ("scale", 8), ("output_format", "mkv"),
    ):
        payload = {**base, "upscale_options": {**base["upscale_options"], field: invalid}}
        with pytest.raises(ValidationError):
            LocalOperationAuthorizeRequest(**payload)
    with pytest.raises(ValidationError):
        LocalOperationAuthorizeRequest(**{
            **base,
            "upscale_options": {**base["upscale_options"], "filepath": "C:/video.mov"},
        })


def test_endpoint_http_upscale_integra_schema_autorizacao_e_persistencia(monkeypatch):
    db = _Db()
    ctx = _context()
    ctx.device.status = DeviceStatus.authorized
    ctx.device.app_version = "1.6.0"
    monkeypatch.setattr(
        router, "get_settings",
        lambda: SimpleNamespace(upscale_enabled=True, upscale_minimum_version="1.6.0"),
    )
    monkeypatch.setattr(router, "consume_challenge", lambda *_args: True)
    monkeypatch.setattr(router, "server_sign", lambda _canonical: "signed-permit")
    api = FastAPI()
    api.include_router(router.router)
    api.dependency_overrides[get_auth_context] = lambda: ctx
    api.dependency_overrides[get_db] = lambda: db

    with TestClient(api) as client:
        response = client.post("/local/authorize", json={
            "operation": "upscale",
            "upscale_options": {
                "media_kind": "video",
                "model": "realcugan",
                "scale": 3,
                "output_format": "prores",
            },
            "challenge": "challenge",
            "challenge_signature": "signature",
        })

    assert response.status_code == 201
    payload = response.json()
    assert payload["signature"] == "signed-permit"
    assert payload["permit"]["parameters"]["model"] == "realcugan"
    assert db.added.operation == "upscale"
    assert db.commits == 1


def test_progresso_local_terminal_e_idempotente(monkeypatch):
    avisos = []
    monkeypatch.setattr(
        router, "notify_local_operation_completed",
        lambda db, record: avisos.append(record),
    )
    now = datetime.now(timezone.utc)
    record = SimpleNamespace(
        id=uuid4(), device_id=uuid4(), operation="gif", status="running",
        stage="processing", progress=50.0, progress_message=None,
        expires_at=now + timedelta(minutes=5), started_at=now,
        completed_at=None,
    )
    db = _Db(record)
    ctx = SimpleNamespace(device=SimpleNamespace(id=record.device_id))
    data = LocalOperationProgressRequest(
        status="completed", stage="completed", progress=99, message="Concluído.",
    )

    response = router.update_local_operation(record.id, data, ctx, db)

    assert response["status"] == "completed"
    assert record.progress == 100.0
    assert record.completed_at is not None
    assert db.commits == 1
    # Processamento local não cria linha em `downloads`: o aviso do Discord
    # precisa sair daqui, senão nada chega ao webhook.
    assert avisos == [record]


class TestCookiesCompartilhados:
    """A sessão do servidor só sai daqui sob condições estritas."""

    def _ctx(self, platform="android"):
        return SimpleNamespace(device=SimpleNamespace(id=uuid4(), platform=platform))

    def test_desligado_por_padrao_nao_entrega(self, monkeypatch):
        monkeypatch.setattr(
            router, "get_settings",
            lambda: SimpleNamespace(cookies_sharing_enabled=False),
        )
        with pytest.raises(HTTPException) as erro:
            router.video_cookies(self._ctx())
        assert erro.value.status_code == 404

    def test_apenas_apps_oficiais_recebem(self, monkeypatch):
        monkeypatch.setattr(
            router, "get_settings",
            lambda: SimpleNamespace(cookies_sharing_enabled=True),
        )
        for plataforma in ("web", "ios"):
            with pytest.raises(HTTPException) as erro:
                router.video_cookies(self._ctx(plataforma))
            assert erro.value.status_code == 403

    def test_sem_arquivo_configurado_responde_indisponivel(self, monkeypatch):
        monkeypatch.setattr(
            router, "get_settings",
            lambda: SimpleNamespace(cookies_sharing_enabled=True),
        )
        monkeypatch.setattr(router, "resolve_cookiefile", lambda: None)
        with pytest.raises(HTTPException) as erro:
            router.video_cookies(self._ctx())
        assert erro.value.status_code == 503

    @pytest.mark.parametrize("plataforma", ["android", "windows"])
    def test_entrega_o_conteudo_aos_apps_oficiais(self, monkeypatch, tmp_path, plataforma):
        arquivo = tmp_path / "cookies.txt"
        arquivo.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\n", encoding="utf-8")
        monkeypatch.setattr(
            router, "get_settings",
            lambda: SimpleNamespace(cookies_sharing_enabled=True),
        )
        monkeypatch.setattr(router, "resolve_cookiefile", lambda: str(arquivo))

        resposta = router.video_cookies(self._ctx(plataforma))

        assert resposta["cookies"].startswith("# Netscape HTTP Cookie File")


def test_conclusao_local_avisa_discord_com_dados_da_tarefa(monkeypatch):
    enviados = []
    monkeypatch.setattr(
        discord, "_enqueue",
        lambda url, payload, timeout: enviados.append(payload) or True,
    )
    monkeypatch.setattr(
        discord, "get_settings",
        lambda: SimpleNamespace(
            discord_download_webhook_url="https://discord.com/api/webhooks/1/token",
        ),
    )
    operation = SimpleNamespace(
        id=uuid4(), device_id=uuid4(), operation="download",
        parameters='{"mode": "va", "height": 1080}',
        url="https://www.youtube.com/watch?v=abc", client_ip="177.54.23.9",
    )
    db = SimpleNamespace(get=lambda _model, _id: SimpleNamespace(platform="android"))

    discord.notify_local_operation_completed(db, operation)

    campos = {f["name"]: f["value"] for f in enviados[0]["embeds"][0]["fields"]}
    assert campos["Tarefa"] == "Download"
    assert campos["Qualidade"] == "1080p"
    assert campos["Origem"] == "android"
    assert campos["IP"] == "177.54.23.xxx"
    assert "youtube.com" in campos["Link"]


def test_sem_webhook_configurado_nada_e_enviado(monkeypatch):
    monkeypatch.setattr(
        discord, "get_settings",
        lambda: SimpleNamespace(discord_download_webhook_url="  "),
    )
    monkeypatch.setattr(
        discord, "_enqueue",
        lambda *_args, **_kwargs: pytest.fail("nao deveria enviar sem webhook"),
    )
    operation = SimpleNamespace(
        id=uuid4(), device_id=uuid4(), operation="gif",
        parameters="{}", url=None, client_ip=None,
    )

    discord.notify_local_operation_completed(SimpleNamespace(), operation)


@pytest.mark.parametrize(
    ("plataforma", "rotulo"),
    [("windows", "EXE (Windows)"), ("android", "APK (Android)")],
)
def test_atualizacao_do_app_avisa_discord(monkeypatch, plataforma, rotulo):
    enviados = []
    monkeypatch.setattr(
        discord, "_enqueue",
        lambda url, payload, timeout: enviados.append((url, payload, timeout)) or True,
    )
    monkeypatch.setattr(
        discord, "get_settings",
        lambda: SimpleNamespace(
            discord_download_webhook_url="https://discord.com/api/webhooks/1/token",
        ),
    )
    device = SimpleNamespace(platform=plataforma, app_version="1.5.6")

    discord.notify_app_updated(device, "1.5.5")

    _, payload, timeout = enviados[0]
    embed = payload["embeds"][0]
    campos = {field["name"]: field["value"] for field in embed["fields"]}
    assert embed["title"] == "1 usuario atualizou"
    assert campos == {
        "Aplicativo": rotulo,
        "Versao anterior": "1.5.5",
        "Nova versao": "1.5.6",
    }
    assert timeout == 5.0


def test_atualizacao_sem_webhook_configurado_nao_enfileira(monkeypatch):
    monkeypatch.setattr(
        discord, "get_settings",
        lambda: SimpleNamespace(discord_download_webhook_url="  "),
    )
    monkeypatch.setattr(
        discord, "_enqueue",
        lambda *_args, **_kwargs: pytest.fail("nao deveria enviar sem webhook"),
    )

    discord.notify_app_updated(
        SimpleNamespace(platform="windows", app_version="1.5.6"),
        "1.5.5",
    )
