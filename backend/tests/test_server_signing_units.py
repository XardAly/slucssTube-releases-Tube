"""Carregamento da chave de assinatura do servidor (Ed25519): arquivo, Base64,
PEM literal, legado (SERVER_KEYS_DIR), prioridade entre fontes e validações."""

from __future__ import annotations

import base64
import os
import stat

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

from backend.api.config import Settings
from backend.security import signing
from backend.security.signing import ServerSigningKeyError

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="permissões POSIX")


def _pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


# As três fontes ficam explicitamente vazias por padrão para isolar os testes
# do backend/.env real (Settings lê env_file=".env" mesmo em construção
# direta; sem isto, uma chave de produção já configurada mascararia os
# cenários "ausente"/"inválido" abaixo).
_NO_SIGNING_SOURCES = {
    "server_signing_private_key_path": "",
    "server_signing_private_key_b64": "",
    "server_signing_private_key_pem": "",
}


def _settings(**overrides) -> Settings:
    values = {"jwt_secret": "x" * 64, "environment": "development", **_NO_SIGNING_SOURCES}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _production_settings(**overrides) -> Settings:
    values = {
        "jwt_secret": "x" * 64,
        "environment": "production",
        **_NO_SIGNING_SOURCES,
        "database_url": "postgresql+psycopg2://u:p@host:5432/db?sslmode=require",
        "web_origin": "https://xard.example",
        "android_download_url": "",
        "android_download_sha256": "",
        "android_download_url_arm64": "",
        "android_download_sha256_arm64": "",
        "android_download_url_arm32": "",
        "android_download_sha256_arm32": "",
        "internal_proxy_secret": "y" * 32,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_carrega_chave_por_arquivo_path(tmp_path):
    key = Ed25519PrivateKey.generate()
    private_path = tmp_path / "server_private.pem"
    private_path.write_bytes(_pem(key))

    settings = _settings(server_signing_private_key_path=str(private_path))
    loaded = signing.load_server_signing_key(settings)

    assert loaded.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    ) == key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )


def test_carrega_chave_por_base64():
    key = Ed25519PrivateKey.generate()
    b64 = base64.b64encode(_pem(key)).decode()

    settings = _settings(server_signing_private_key_b64=b64)
    loaded = signing.load_server_signing_key(settings)

    assert loaded.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ) == key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def test_carrega_chave_por_pem_literal():
    key = Ed25519PrivateKey.generate()
    pem_text = _pem(key).decode()

    settings = _settings(server_signing_private_key_pem=pem_text)
    loaded = signing.load_server_signing_key(settings)
    assert isinstance(loaded, Ed25519PrivateKey)

    # Paineis que não aceitam múltiplas linhas: "\n" escapado também funciona.
    escaped = pem_text.replace("\n", "\\n")
    settings_escaped = _settings(server_signing_private_key_pem=escaped)
    loaded_escaped = signing.load_server_signing_key(settings_escaped)
    assert loaded_escaped.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ) == key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def test_prioridade_entre_fontes(tmp_path):
    key_path = Ed25519PrivateKey.generate()
    key_b64 = Ed25519PrivateKey.generate()
    key_pem = Ed25519PrivateKey.generate()

    private_path = tmp_path / "private.pem"
    private_path.write_bytes(_pem(key_path))

    settings = _settings(
        server_signing_private_key_path=str(private_path),
        server_signing_private_key_b64=base64.b64encode(_pem(key_b64)).decode(),
        server_signing_private_key_pem=_pem(key_pem).decode(),
    )
    loaded = signing.load_server_signing_key(settings)
    assert loaded.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ) == key_path.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    # Sem PATH, B64 tem prioridade sobre PEM.
    settings_no_path = _settings(
        server_signing_private_key_b64=base64.b64encode(_pem(key_b64)).decode(),
        server_signing_private_key_pem=_pem(key_pem).decode(),
    )
    loaded2 = signing.load_server_signing_key(settings_no_path)
    assert loaded2.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ) == key_b64.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def test_configuracao_ausente_em_producao(tmp_path):
    settings = _production_settings(server_keys_dir=str(tmp_path / "keys"))
    with pytest.raises(ServerSigningKeyError) as exc:
        signing.load_server_signing_key(settings)
    message = str(exc.value)
    assert "SERVER_SIGNING_PRIVATE_KEY_PATH" in message
    assert "SERVER_SIGNING_PRIVATE_KEY_B64" in message
    assert "SERVER_SIGNING_PRIVATE_KEY_PEM" in message


def test_producao_com_base64_funciona():
    key = Ed25519PrivateKey.generate()
    settings = _production_settings(
        server_signing_private_key_b64=base64.b64encode(_pem(key)).decode()
    )
    message = b"manifesto assinado"
    signature = signing.server_sign(message, settings)
    public_pem = signing.server_public_key_pem(settings)
    assert signing.verify_raw_signature(public_pem, signature, message)


def test_ambiente_dev_gera_e_persiste_chave(tmp_path):
    keys_dir = tmp_path / "keys"
    settings = _settings(server_keys_dir=str(keys_dir))

    first = signing.load_server_signing_key(settings)
    assert (keys_dir / signing.SERVER_PRIVATE_KEY_FILE).is_file()
    assert (keys_dir / signing.SERVER_PUBLIC_KEY_FILE).is_file()

    # Um segundo boot (novo objeto Settings, mesmo diretório) reaproveita a
    # mesma chave em vez de gerar outra.
    second = signing.load_server_signing_key(_settings(server_keys_dir=str(keys_dir)))
    assert first.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ) == second.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def test_base64_invalido():
    settings = _settings(server_signing_private_key_b64="isto-nao-e-base64-valido!!!")
    with pytest.raises(ServerSigningKeyError, match="SERVER_SIGNING_PRIVATE_KEY_B64"):
        signing.load_server_signing_key(settings)


def test_pem_invalido():
    settings = _settings(server_signing_private_key_pem="isto nao e um PEM")
    with pytest.raises(ServerSigningKeyError, match="SERVER_SIGNING_PRIVATE_KEY_PEM"):
        signing.load_server_signing_key(settings)


def test_arquivo_inexistente(tmp_path):
    settings = _settings(server_signing_private_key_path=str(tmp_path / "nao-existe.pem"))
    with pytest.raises(ServerSigningKeyError, match="inexistente"):
        signing.load_server_signing_key(settings)


def test_arquivo_vazio(tmp_path):
    empty_path = tmp_path / "vazio.pem"
    empty_path.write_bytes(b"")
    settings = _settings(server_signing_private_key_path=str(empty_path))
    with pytest.raises(ServerSigningKeyError, match="vazio"):
        signing.load_server_signing_key(settings)


def test_algoritmo_incorreto():
    rsa_key = generate_private_key(public_exponent=65537, key_size=2048)
    rsa_pem = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    settings = _settings(server_signing_private_key_b64=base64.b64encode(rsa_pem).decode())
    with pytest.raises(ServerSigningKeyError, match="Ed25519"):
        signing.load_server_signing_key(settings)


def test_chave_publica_incompativel_no_legado(tmp_path):
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    key_a = Ed25519PrivateKey.generate()
    key_b = Ed25519PrivateKey.generate()

    (keys_dir / signing.SERVER_PRIVATE_KEY_FILE).write_bytes(_pem(key_a))
    (keys_dir / signing.SERVER_PUBLIC_KEY_FILE).write_bytes(
        key_b.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )

    settings = _settings(server_keys_dir=str(keys_dir))
    with pytest.raises(ServerSigningKeyError, match="não correspondem"):
        signing.load_server_signing_key(settings)


@POSIX_ONLY
def test_permissoes_inadequadas(tmp_path):
    key = Ed25519PrivateKey.generate()
    private_path = tmp_path / "private.pem"
    private_path.write_bytes(_pem(key))
    private_path.chmod(0o644)

    settings = _settings(server_signing_private_key_path=str(private_path))
    with pytest.raises(ServerSigningKeyError, match="permiss"):
        signing.load_server_signing_key(settings)
