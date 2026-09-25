from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest

from tools import google_drive_oauth_setup as oauth_setup


def test_pkce_usa_sha256_e_base64url_sem_padding():
    verifier, challenge = oauth_setup._pkce()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert challenge == expected
    assert "=" not in challenge


def test_json_oauth_precisa_ser_cliente_desktop(tmp_path):
    client_json = tmp_path / "client.json"
    client_json.write_text(json.dumps({"web": {"client_id": "x"}}), encoding="utf-8")
    with pytest.raises(oauth_setup.SetupError, match="Aplicativo para computador"):
        oauth_setup._load_client(client_json)


def test_json_desktop_oficial_com_endpoint_legado_e_aceito(tmp_path):
    client_json = tmp_path / "client.json"
    client_json.write_text(json.dumps({
        "installed": {
            "client_id": "client",
            "client_secret": "secret",
            "auth_uri": oauth_setup.LEGACY_AUTH_URL,
            "token_uri": oauth_setup.TOKEN_URL,
        }
    }), encoding="utf-8")
    assert oauth_setup._load_client(client_json) == ("client", "secret")


def test_troca_oauth_exige_refresh_token_e_escopo_minimo():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(oauth_setup.TOKEN_URL)
        body = request.content.decode()
        assert "grant_type=authorization_code" in body
        assert "code_verifier=verifier" in body
        return httpx.Response(200, json={
            "access_token": "access",
            "refresh_token": "refresh",
            "scope": oauth_setup.DRIVE_FILE_SCOPE,
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert oauth_setup._exchange_code(
            client,
            code="code",
            verifier="verifier",
            redirect_uri="http://127.0.0.1:1234/oauth2/callback",
            client_id="client",
            client_secret="secret",
        ) == ("access", "refresh")


def test_arquivo_secreto_e_atomico_e_nao_sobrescreve(tmp_path):
    output = tmp_path / "oauth.env"
    folders = {
        "root": "rootfolder123",
        "videos": "videosfolder123",
        "gifs": "gifsfolder123",
    }
    oauth_setup._write_env(
        output,
        client_id="client",
        client_secret="secret",
        refresh_token="refresh",
        folders=folders,
        overwrite=False,
    )
    content = output.read_text(encoding="utf-8")
    assert 'GOOGLE_DRIVE_REFRESH_TOKEN="refresh"' in content
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(oauth_setup.SetupError, match="--overwrite"):
        oauth_setup._write_env(
            output,
            client_id="other",
            client_secret="other",
            refresh_token="other",
            folders=folders,
            overwrite=False,
        )


def test_fluxo_administrativo_nao_imprime_tokens(tmp_path, monkeypatch, capsys):
    client_json = tmp_path / "client.json"
    client_json.write_text(json.dumps({
        "installed": {
            "client_id": "client-id-secret",
            "client_secret": "client-secret-value",
            "auth_uri": oauth_setup.AUTH_URL,
            "token_uri": oauth_setup.TOKEN_URL,
        }
    }), encoding="utf-8")
    output = tmp_path / "generated.env"

    monkeypatch.setattr(
        oauth_setup,
        "_receive_code",
        lambda _client_id: ("authorization-code-secret", "verifier", "http://127.0.0.1/callback"),
    )
    monkeypatch.setattr(
        oauth_setup,
        "_exchange_code",
        lambda *_args, **_kwargs: ("access-token-secret", "refresh-token-secret"),
    )
    monkeypatch.setattr(
        oauth_setup,
        "_prepare_folders",
        lambda *_args, **_kwargs: {
            "root": "rootfolder123",
            "videos": "videosfolder123",
            "gifs": "gifsfolder123",
            "temporary": "temporaryfolder123",
            "recovery": "recoveryfolder123",
        },
    )

    assert oauth_setup.main([
        "--client-json", str(client_json),
        "--output", str(output),
    ]) == 0
    rendered = capsys.readouterr().out
    assert "access-token-secret" not in rendered
    assert "refresh-token-secret" not in rendered
    assert "client-secret-value" not in rendered
    assert output.is_file()
