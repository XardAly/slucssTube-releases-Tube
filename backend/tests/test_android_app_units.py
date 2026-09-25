from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.api import main
from backend.api.config import Settings
from backend.auth import service
from backend.auth.schemas import DeviceInfo
from backend.downloads import router
from backend.downloads.schemas import LocalOperationAuthorizeRequest


class _Db:
    def __init__(self, count=0):
        self.added = None
        self.count = count

    def add(self, value):
        self.added = value

    def commit(self):
        pass

    def execute(self, _statement):
        return SimpleNamespace(scalar_one=lambda: self.count)


def test_manifesto_android_e_assinado_com_configuracao_remota(monkeypatch):
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(
            android_latest_version="1.2.0",
            android_release_id="android-12",
            android_update_policy="recommended",
            android_latest_version_code=12,
            android_minimum_version_code=10,
            android_download_url="https://api.example/app/android/apk",
            android_download_sha256="a" * 64,
            android_download_url_arm64="",
            android_download_sha256_arm64="",
            android_download_url_arm32="",
            android_download_sha256_arm32="",
            android_changelog_json='["Correção no YouTube"]',
            android_processing_mode="local",
            android_server_fallback_enabled=False,
            android_upscale_enabled=True,
            android_upscale_minimum_version_code=20,
            android_upscale_engine_version="ncnn-20260526-v1",
            android_animevideov3_model_version="animevideov3-ncnn-v1",
            android_realcugan_model_version="realcugan-se-conservative-v1",
        ),
    )
    monkeypatch.setattr(
        main,
        "server_sign",
        lambda payload: hashlib.sha256(payload).hexdigest(),
    )

    response = main.android_app_version()
    signature = response.pop("signature")
    canonical = json.dumps(
        response, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")

    assert response["latest_version_code"] == 12
    assert response["release_id"] == "android-12"
    assert response["update_policy"] == "recommended"
    assert response["processing_mode"] == "local"
    assert response["upscale"] == {
        "enabled": True,
        "minimum_version_code": 20,
        "engine_version": "ncnn-20260526-v1",
        "models": {
            "realesr_animevideov3": "animevideov3-ncnn-v1",
            "realcugan": "realcugan-se-conservative-v1",
        },
    }
    # Sem fatias publicadas o mapa vem vazio e todo aparelho usa o universal.
    assert response["downloads"] == {}
    assert signature == hashlib.sha256(canonical).hexdigest()


def test_manifesto_publica_fatia_por_arquitetura_e_ignora_par_incompleto(monkeypatch):
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(
            android_latest_version="1.0.6",
            android_release_id="android-7",
            android_update_policy="optional",
            android_latest_version_code=7,
            android_minimum_version_code=2,
            android_download_url="https://exemplo/universal.apk",
            android_download_sha256="a" * 64,
            android_download_url_arm64="https://exemplo/arm64.apk",
            android_download_sha256_arm64="B" * 64,
            # Par incompleto: URL sem hash não pode ser publicada, senão o
            # aplicativo baixaria dezenas de MB e recusaria no final.
            android_download_url_arm32="https://exemplo/arm32.apk",
            android_download_sha256_arm32="",
            android_changelog_json="Um",
            android_processing_mode="local",
            android_server_fallback_enabled=False,
            android_upscale_enabled=True,
            android_upscale_minimum_version_code=20,
            android_upscale_engine_version="ncnn-20260526-v1",
            android_animevideov3_model_version="animevideov3-ncnn-v1",
            android_realcugan_model_version="realcugan-se-conservative-v1",
        ),
    )
    monkeypatch.setattr(main, "server_sign", lambda payload: hashlib.sha256(payload).hexdigest())

    response = main.android_app_version()

    assert response["downloads"] == {
        "arm64-v8a": {"url": "https://exemplo/arm64.apk", "sha256": "b" * 64},
    }
    assert response["download_url"] == "https://exemplo/universal.apk"
    # A assinatura precisa cobrir o campo novo, ou o cliente recusa o manifesto.
    signature = response.pop("signature")
    canonical = json.dumps(
        response, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    assert signature == hashlib.sha256(canonical).hexdigest()


def test_changelog_aceita_lista_separada_por_barra_e_descarta_json_quebrado(monkeypatch):
    """O painel da hospedagem não preserva JSON; a lista com `|` precisa valer."""
    def changelog_para(valor):
        monkeypatch.setattr(
            main, "settings", SimpleNamespace(android_changelog_json=valor),
        )
        return main._android_changelog()

    assert changelog_para("Um|Dois") == ["Um", "Dois"]
    assert changelog_para('["Um","Dois"]') == ["Um", "Dois"]
    assert changelog_para("Um\nDois") == ["Um", "Dois"]
    assert changelog_para("  Um  |  | Dois ") == ["Um", "Dois"]
    # JSON truncado não pode virar texto avulso na tela do usuário.
    assert changelog_para('["Um","Do') == []
    assert changelog_para("") == []


def test_configuracao_de_producao_exige_https_no_instalador_android():
    settings = Settings.model_construct(
        environment="production",
        jwt_algorithm="HS256",
        jwt_secret="x" * 32,
        database_url="postgresql://user:pass@db/app?sslmode=require",
        web_origin="https://site.example",
        app_download_url="",
        android_download_url="",
        android_download_sha256="",
        android_installer_url="http://downloads.example/installer.apk",
        android_minimum_version_code=1,
        android_latest_version_code=1,
        discord_security_webhook_enabled=False,
        security_log_hmac_key="",
        internal_proxy_secret="y" * 32,
        worker_binary_downloads_enabled=False,
    )

    with pytest.raises(ValueError, match="ANDROID_INSTALLER_URL deve usar HTTPS"):
        settings.production_must_fail_closed()


def test_configuracao_de_producao_valida_fatias_android_completas_e_https():
    settings = Settings.model_construct(
        environment="production",
        jwt_algorithm="HS256",
        jwt_secret="x" * 32,
        database_url="postgresql://user:pass@db/app?sslmode=require",
        web_origin="https://site.example",
        app_download_url="",
        android_download_url="",
        android_download_sha256="",
        android_installer_url="https://example.com/installer.apk",
        android_download_url_arm64="http://example.com/arm64.apk",
        android_download_sha256_arm64="a" * 64,
        android_download_url_arm32="https://example.com/arm32.apk",
        android_download_sha256_arm32="",
        android_minimum_version_code=1,
        android_latest_version_code=1,
        discord_security_webhook_enabled=False,
        security_log_hmac_key="",
        internal_proxy_secret="y" * 32,
        worker_binary_downloads_enabled=False,
    )

    with pytest.raises(ValueError) as captured:
        settings.production_must_fail_closed()

    message = str(captured.value)
    assert "ANDROID_DOWNLOAD_URL_ARM64 deve usar HTTPS" in message
    assert "ANDROID_DOWNLOAD_URL_ARM32 e seu SHA-256 devem ser configurados juntos" in message


def test_android_sideload_usa_identidade_assinada_sem_exigir_token_play(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: SimpleNamespace(android_sideload_enabled=True),
    )
    device = DeviceInfo(
        device_id="android-device-1",
        installation_id="android-install-1",
        public_key="public-key",
        platform="android",
        app_version="1.0.0",
    )

    asyncio.run(service._verify_client_integrity(device))


def test_android_pode_autorizar_download_local(monkeypatch):
    db = _Db()
    ctx = SimpleNamespace(
        device=SimpleNamespace(
            id=uuid4(), device_id="android-device", platform="android",
            public_key="public-key",
        ),
        ip="203.0.113.20",
    )
    monkeypatch.setattr(router, "consume_challenge", lambda *_args: True)
    monkeypatch.setattr(router, "server_sign", lambda _payload: "signature")
    request = LocalOperationAuthorizeRequest(
        operation="download",
        url="https://www.youtube.com/watch?v=abcdefghijk",
        mode="va",
        video_format="mp4",
        audio_format="mp3",
        challenge="challenge",
        challenge_signature="signature",
    )

    response = router.authorize_local_operation(request, ctx, db)

    assert response["permit"]["device_id"] == "android-device"
    assert db.added.operation == "download"


def test_download_do_apk_redireciona_sem_exigir_arquivo_no_backend(monkeypatch):
    url = (
        "https://github.com/XardAly/slucssTube-releases-Tube/releases/download/"
        "android-v1.0.0/Slucss-System.apk"
    )
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(android_download_url=url, android_apk_file="arquivo-ausente.apk"),
    )
    monkeypatch.setattr(main, "client_ip", lambda _request: "203.0.113.21")
    db = _Db()

    response = main.android_app_apk(object(), db)

    assert response.status_code == 307
    assert response.headers["location"] == url
    assert db.added.event == "android_apk_download"


@pytest.mark.parametrize(
    ("abi", "expected_url"),
    (
        ("arm64", "https://example.com/Slucss-System-arm64-v8a.apk"),
        ("arm32", "https://example.com/Slucss-System-armeabi-v7a.apk"),
    ),
)
def test_download_do_apk_seleciona_fatia_por_abi(monkeypatch, abi, expected_url):
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(
            android_download_url="https://example.com/Slucss-System.apk",
            android_download_sha256="a" * 64,
            android_download_url_arm64="https://example.com/Slucss-System-arm64-v8a.apk",
            android_download_sha256_arm64="b" * 64,
            android_download_url_arm32="https://example.com/Slucss-System-armeabi-v7a.apk",
            android_download_sha256_arm32="c" * 64,
            android_apk_file="arquivo-ausente.apk",
        ),
    )
    monkeypatch.setattr(main, "client_ip", lambda _request: "203.0.113.25")
    db = _Db()

    response = main.android_app_apk(object(), db, abi)

    assert response.status_code == 307
    assert response.headers["location"] == expected_url
    assert db.added.event == "android_apk_download"


def test_download_do_apk_com_fatia_incompleta_cai_no_universal(monkeypatch):
    universal_url = "https://example.com/Slucss-System.apk"
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(
            android_download_url=universal_url,
            android_download_sha256="a" * 64,
            android_download_url_arm64="https://example.com/Slucss-System-arm64-v8a.apk",
            android_download_sha256_arm64="",
            android_download_url_arm32="",
            android_download_sha256_arm32="",
            android_apk_file="arquivo-ausente.apk",
        ),
    )
    monkeypatch.setattr(main, "client_ip", lambda _request: "203.0.113.26")
    db = _Db()

    response = main.android_app_apk(object(), db, "arm64")

    assert response.status_code == 307
    assert response.headers["location"] == universal_url
    assert db.added.event == "android_apk_download"


def test_download_do_apk_com_abi_respeita_o_mesmo_limite_por_ip(monkeypatch):
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(
            android_download_url="https://example.com/Slucss-System.apk",
            android_download_sha256="a" * 64,
            android_download_url_arm64="https://example.com/Slucss-System-arm64-v8a.apk",
            android_download_sha256_arm64="b" * 64,
            android_download_url_arm32="",
            android_download_sha256_arm32="",
            android_apk_file="arquivo-ausente.apk",
        ),
    )
    monkeypatch.setattr(main, "client_ip", lambda _request: "203.0.113.27")
    db = _Db(count=11)

    with pytest.raises(main.HTTPException) as captured:
        main.android_app_apk(object(), db, "arm64")

    assert captured.value.status_code == 429
    assert db.added is None


def test_download_do_instalador_android_redireciona_para_apk_completo(monkeypatch):
    monkeypatch.setattr(main, "client_ip", lambda _request: "203.0.113.22")
    db = _Db()

    response = main.android_app_installer(object(), db)

    assert response.status_code == 307
    assert response.headers["location"] == "/app/android/apk"
    assert db.added.event == "android_installer_download"


def test_download_do_instalador_android_nao_entrega_bootstrap(monkeypatch):
    monkeypatch.setattr(main, "client_ip", lambda _request: "203.0.113.23")
    db = _Db()

    response = main.android_app_installer(object(), db)

    assert response.status_code == 307
    assert response.headers["location"] == "/app/android/apk"
    assert db.added.event == "android_installer_download"


def test_download_do_instalador_android_respeita_limite_por_ip(monkeypatch):
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(
            android_installer_url="https://example.com/installer.apk",
            android_installer_apk_file="arquivo-ausente.apk",
        ),
    )
    monkeypatch.setattr(main, "client_ip", lambda _request: "203.0.113.24")
    db = _Db(count=11)

    with pytest.raises(main.HTTPException) as captured:
        main.android_app_installer(object(), db)

    assert captured.value.status_code == 429
    assert db.added is None
