"""Testes unitários das peças de segurança puras."""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from datetime import datetime, timedelta, timezone
import stat
import zipfile
from types import SimpleNamespace

import jwt
import pytest
from pydantic import ValidationError

from backend.api.config import get_settings
from backend.api.deps import _signed_request_body, client_ip, get_auth_context
from backend.auth.schemas import RefreshRequest

from backend.auth.tokens import (
    create_access_token,
    decode_access_token,
    hash_refresh_token,
    new_refresh_token,
)
from backend.downloads.service import make_delivery_token, verify_delivery_token
from backend.downloads import service as download_service
from backend.downloads.router import _video_signature_matches
from backend.notifications.discord import _mask_ip, _safe_text, _valid_webhook_url
from backend.security import challenge as challenge_security
from backend.security import replay as replay_security
from backend.security.replay import timestamp_valid
from backend.security.signing import canonical_string
from backend.security.url_validation import UnsafeUrl, validate_public_http_url
from backend.workers import binary_install, deno_utils, ffmpeg_utils


class TestAccessToken:
    def test_quando_token_valido_retorna_device_id(self):
        device_id = uuid.uuid4()
        token = create_access_token(device_id)
        payload = decode_access_token(token)
        assert payload is not None
        assert payload["dev"] == str(device_id)

    def test_quando_token_adulterado_retorna_none(self):
        token = create_access_token(uuid.uuid4())
        assert decode_access_token(token + "x") is None

    def test_token_nao_carrega_dados_sensiveis(self):
        payload = decode_access_token(create_access_token(uuid.uuid4()))
        assert set(payload.keys()) == {
            "dev", "sub", "iss", "aud", "purpose", "nbf", "iat", "exp", "jti"
        }
        assert payload["purpose"] == "access"

    def test_token_com_audiencia_errada_e_rejeitado(self):
        settings = get_settings()
        now = int(time.time())
        token = jwt.encode(
            {
                "dev": str(uuid.uuid4()), "sub": str(uuid.uuid4()),
                "iss": settings.jwt_issuer, "aud": "outra-api", "purpose": "access",
                "iat": now, "nbf": now, "exp": now + 60, "jti": "x",
            },
            settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
        assert decode_access_token(token) is None


class TestRefreshToken:
    def test_banco_recebe_apenas_hash(self):
        token, token_hash, _ = new_refresh_token()
        assert token != token_hash
        assert hash_refresh_token(token) == token_hash
        assert len(token_hash) == 64  # sha256 hex

    def test_tokens_sao_unicos(self):
        assert new_refresh_token()[0] != new_refresh_token()[0]


class TestReplayTimestamp:
    def test_agora_e_valido(self):
        assert timestamp_valid(str(int(time.time())))

    def test_fora_da_janela_e_invalido(self):
        assert not timestamp_valid(str(int(time.time()) - 3600))

    def test_lixo_e_invalido(self):
        assert not timestamp_valid("abc")
        assert not timestamp_valid("")


class TestAuthExecutionModel:
    def test_banco_da_autenticacao_roda_fora_do_event_loop(self):
        assert not inspect.iscoroutinefunction(get_auth_context)

    def test_upload_streaming_nao_e_carregado_na_memoria(self):
        class UploadRequest:
            url = SimpleNamespace(path="/conversion/create-upload")

            async def body(self):
                raise AssertionError("o corpo grande do upload não deve ser lido")

        assert asyncio.run(_signed_request_body(UploadRequest())) == b""

    def test_requisicao_comum_preserva_corpo_exato_assinado(self):
        class JsonRequest:
            url = SimpleNamespace(path="/local/authorize")

            async def body(self):
                return b'{"challenge":"abc"}'

        assert asyncio.run(_signed_request_body(JsonRequest())) == b'{"challenge":"abc"}'


class TestAuthTransactionCount:
    class _Nested:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Db:
        def __init__(self, stored=None):
            self.stored = stored
            self.commits = 0
            self.flushes = 0
            self.added = []

        def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: self.stored)

        def begin_nested(self):
            return TestAuthTransactionCount._Nested()

        def add(self, value):
            self.added.append(value)

        def flush(self):
            self.flushes += 1

        def commit(self):
            self.commits += 1

    def test_antireplay_faz_flush_e_deixa_commit_para_auditoria(self, monkeypatch):
        db = self._Db()
        monkeypatch.setattr(replay_security, "_last_nonce_cleanup", time.monotonic())

        assert replay_security.request_id_fresh(db, "request-unico")
        assert db.flushes == 1
        assert db.commits == 0
        assert db.added[0].id == "request-unico"

    def test_challenge_valido_pode_confirmar_junto_da_operacao(self, monkeypatch):
        db = self._Db(stored="public-key")
        monkeypatch.setattr(challenge_security, "verify_raw_signature", lambda *_args: True)

        assert challenge_security.consume_challenge(
            db, "nonce", "public-key", "signature", False,
        )
        assert db.commits == 0

    def test_challenge_invalido_continua_consumido_imediatamente(self, monkeypatch):
        db = self._Db(stored="public-key")
        monkeypatch.setattr(challenge_security, "verify_raw_signature", lambda *_args: False)

        assert not challenge_security.consume_challenge(
            db, "nonce", "public-key", "signature", False,
        )
        assert db.commits == 1


class TestDeliveryToken:
    def test_token_valido_para_o_mesmo_download(self):
        download_id = uuid.uuid4()
        device_id = uuid.uuid4()
        token = make_delivery_token(download_id, device_id, 3)
        assert verify_delivery_token(download_id, device_id, 3, token)

    def test_token_nao_vale_para_outro_download(self):
        download_id = uuid.uuid4()
        device_id = uuid.uuid4()
        token = make_delivery_token(download_id, device_id, 1)
        assert not verify_delivery_token(uuid.uuid4(), device_id, 1, token)
        assert not verify_delivery_token(download_id, uuid.uuid4(), 1, token)
        assert not verify_delivery_token(download_id, device_id, 2, token)

    def test_token_malformado_e_rejeitado(self):
        download_id = uuid.uuid4()
        device_id = uuid.uuid4()
        assert not verify_delivery_token(download_id, device_id, 1, "lixo")
        assert not verify_delivery_token(download_id, device_id, 1, "123.abc")

    def test_token_respeita_expiracao_real_do_arquivo(self):
        download_id = uuid.uuid4()
        device_id = uuid.uuid4()
        token = make_delivery_token(
            download_id, device_id, 1,
            datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        assert not verify_delivery_token(download_id, device_id, 1, token)


class TestCanonicalString:
    def test_inclui_todos_os_campos_assinados(self):
        s = canonical_string("post", "/download/create", "111", "req-1", b"{}")
        parts = s.decode().split("\n")
        assert parts[0] == "POST"
        assert parts[1] == "/download/create"
        assert parts[2] == "111"
        assert parts[3] == "req-1"
        assert len(parts[4]) == 64  # sha256 do body

    def test_body_diferente_muda_a_string(self):
        a = canonical_string("POST", "/x", "1", "r", b"a")
        b = canonical_string("POST", "/x", "1", "r", b"b")
        assert a != b


class TestStrictInput:
    def test_refresh_rejeita_mass_assignment(self):
        with pytest.raises(ValidationError):
            RefreshRequest(refresh_token="token", is_admin=True)


class TestForwardedIp:
    @staticmethod
    def _request(peer: str, headers: dict[str, str]):
        return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)

    def test_header_falso_de_cliente_direto_e_ignorado(self, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "")
        monkeypatch.setattr(settings, "internal_proxy_secret", "segredo-interno")
        request = self._request("203.0.113.8", {"x-forwarded-for": "10.0.0.1"})
        assert client_ip(request) == "203.0.113.8"

    def test_proxy_autenticado_pode_encaminhar_ip(self, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "")
        monkeypatch.setattr(settings, "internal_proxy_secret", "segredo-interno")
        request = self._request(
            "203.0.113.8",
            {"x-internal-proxy-key": "segredo-interno", "x-forwarded-for": "2001:db8::1"},
        )
        assert client_ip(request) == "2001:db8::1"


class TestFilesAndAlerts:
    def test_magic_bytes_nao_confiam_so_na_extensao(self):
        assert _video_signature_matches(".mp4", b"\x00\x00\x00\x18ftypisom")
        assert not _video_signature_matches(".mp4", b"MZ" + b"\x00" * 14)

    def test_storage_rejeita_arquivo_fora_da_raiz(self, tmp_path, monkeypatch):
        root = tmp_path / "storage"
        root.mkdir()
        outside = tmp_path / "secret.txt"
        outside.write_text("x")
        monkeypatch.setattr(get_settings(), "downloads_dir", str(root))
        assert download_service.safe_storage_file(str(outside)) is None

    def test_webhook_e_dados_sensiveis_sao_sanitizados(self):
        assert _valid_webhook_url("https://discord.com/api/webhooks/1/token")
        assert not _valid_webhook_url("https://example.com/api/webhooks/1/token")
        assert _mask_ip("177.54.23.9") == "177.54.23.xxx"
        assert "segredo" not in _safe_text("Bearer segredo")


class TestSsrfAndWorkerBootstrap:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/x",
            "http://[::1]/x",
            "http://10.0.0.1/x",
            "http://169.254.169.254/latest/meta-data",
            "http://2130706433/x",
            "http://0x7f000001/x",
            "https://user:password@example.com/x",
            "https://example.com:6379/x",
        ],
    )
    def test_destinos_ssrf_sao_rejeitados_antes_do_worker(self, url):
        with pytest.raises(UnsafeUrl):
            validate_public_http_url(url)

    def test_ffmpeg_rejeita_traversal_de_arquivo(self):
        assert not ffmpeg_utils._safe_archive_name("../../ffmpeg.exe")
        assert not ffmpeg_utils._safe_archive_name("/tmp/ffmpeg")

    def test_deno_rejeita_symlink_no_zip(self):
        item = zipfile.ZipInfo("deno")
        item.external_attr = (stat.S_IFLNK | 0o777) << 16
        assert not deno_utils._safe_zip_entry(item)

    def test_ffmpeg_falha_antes_da_rede_quando_hash_ausente(self):
        with pytest.raises(RuntimeError, match="não configurado"):
            with binary_install.verified_archive(
                "https://github.com/example.zip",
                "",
                max_bytes=1024,
                allowed_hosts={"github.com"},
                prefix="test-",
            ):
                pass
