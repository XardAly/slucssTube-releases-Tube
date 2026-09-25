"""
Assinatura assimétrica (Ed25519).

Modelo de chaves:

1. CHAVE DO DISPOSITIVO — gerada NO dispositivo na instalação.
   - Privada: nunca sai do dispositivo (DPAPI no Windows / Keystore no Android).
   - Pública: enviada ao servidor no registro do dispositivo.
   - Usada para assinar TODAS as requisições protegidas e os challenges.
   - Engenharia reversa do app não compromete outros usuários: cada
     instalação tem sua própria chave, revogável individualmente.

2. CHAVE DO SERVIDOR — carregada uma vez no boot, nunca regenerada sozinha
   em produção (regenerar invalidaria assinaturas e o manifesto já confiado
   pelos apps instalados).
   - Privada: nunca sai do servidor.
   - Pública: embutida no app (pode ser pública) para o cliente verificar
     manifestos de atualização e respostas críticas.

   Fontes aceitas, nesta ordem de prioridade (a primeira não vazia decide):
     1. SERVER_SIGNING_PRIVATE_KEY_PATH — arquivo PEM montado por secret mount.
     2. SERVER_SIGNING_PRIVATE_KEY_B64  — PEM em Base64 (hospedagens sem
        suporte a secret mount, ex.: Square Cloud).
     3. SERVER_SIGNING_PRIVATE_KEY_PEM  — PEM literal na variável de ambiente.
     4. SERVER_KEYS_DIR (legado)        — par de arquivos
        server_ed25519_{private,public}.pem num diretório; fora de produção,
        gerado uma vez e persistido em disco para os próximos boots.
   Em produção, alguma das quatro é obrigatória; nenhuma chave é gerada
   automaticamente. Veja backend/SERVER_SIGNING_KEY.md.

String canônica assinada pelo cliente em cada requisição:

    METHOD \n PATH \n X-Timestamp \n X-Request-ID \n sha256(body)
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import stat
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from backend.api.config import Settings, get_settings

SERVER_PRIVATE_KEY_FILE = "server_ed25519_private.pem"
SERVER_PUBLIC_KEY_FILE = "server_ed25519_public.pem"


class ServerSigningKeyError(RuntimeError):
    """Configuração da chave de assinatura do servidor ausente ou inválida.

    A mensagem nunca inclui o conteúdo da chave, só qual fonte foi rejeitada.
    """


def canonical_string(method: str, path: str, timestamp: str, request_id: str, body: bytes) -> bytes:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp}\n{request_id}\n{body_hash}".encode()


def canonical_string_from_hash(
    method: str, path: str, timestamp: str, request_id: str, body_hash: str
) -> bytes:
    return f"{method.upper()}\n{path}\n{timestamp}\n{request_id}\n{body_hash}".encode()


def verify_device_signature(
    public_key_pem: str,
    signature_b64: str,
    method: str,
    path: str,
    timestamp: str,
    request_id: str,
    body: bytes,
) -> bool:
    """Valida a assinatura Ed25519 enviada em X-Signature."""
    try:
        key = serialization.load_pem_public_key(public_key_pem.encode())
        if not isinstance(key, Ed25519PublicKey):
            return False
        key.verify(
            base64.b64decode(signature_b64),
            canonical_string(method, path, timestamp, request_id, body),
        )
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def verify_device_signature_hash(
    public_key_pem: str,
    signature_b64: str,
    method: str,
    path: str,
    timestamp: str,
    request_id: str,
    body_hash: str,
) -> bool:
    """Valida upload em streaming sem carregar o corpo inteiro na memoria."""
    normalized = body_hash.lower()
    if len(normalized) != 64 or any(c not in "0123456789abcdef" for c in normalized):
        return False
    try:
        key = serialization.load_pem_public_key(public_key_pem.encode())
        if not isinstance(key, Ed25519PublicKey):
            return False
        key.verify(
            base64.b64decode(signature_b64),
            canonical_string_from_hash(
                method, path, timestamp, request_id, normalized
            ),
        )
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def verify_raw_signature(public_key_pem: str, signature_b64: str, message: bytes) -> bool:
    """Valida assinatura sobre uma mensagem arbitrária (challenges)."""
    try:
        key = serialization.load_pem_public_key(public_key_pem.encode())
        if not isinstance(key, Ed25519PublicKey):
            return False
        key.verify(base64.b64decode(signature_b64), message)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def _reject_insecure_permissions(path: Path, source: str) -> None:
    """Recusa arquivo de chave legível/gravável por outros usuários (best-effort; POSIX apenas)."""
    if os.name != "posix":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if mode & (stat.S_IROTH | stat.S_IWOTH | stat.S_IRGRP | stat.S_IWGRP):
        raise ServerSigningKeyError(
            f"{source} tem permissões inadequadas (deveria ser legível somente pelo dono, ex.: 600)."
        )


def _parse_private_key(pem_bytes: bytes, source: str) -> Ed25519PrivateKey:
    if not pem_bytes.strip():
        raise ServerSigningKeyError(f"{source} está vazio.")
    try:
        key = serialization.load_pem_private_key(pem_bytes, password=None)
    except (ValueError, TypeError) as exc:
        raise ServerSigningKeyError(f"{source} não contém uma chave PEM válida.") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ServerSigningKeyError(f"{source} não é uma chave Ed25519.")
    return key


def _key_from_path(path_value: str) -> Ed25519PrivateKey:
    source = "SERVER_SIGNING_PRIVATE_KEY_PATH"
    path = Path(path_value)
    if path.is_symlink():
        raise ServerSigningKeyError(f"{source} não pode apontar para um symlink.")
    if not path.is_file():
        raise ServerSigningKeyError(f"{source} aponta para um arquivo inexistente.")
    _reject_insecure_permissions(path, source)
    return _parse_private_key(path.read_bytes(), source)


def _key_from_base64(value: str) -> Ed25519PrivateKey:
    source = "SERVER_SIGNING_PRIVATE_KEY_B64"
    try:
        pem_bytes = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ServerSigningKeyError(f"{source} não é Base64 válido.") from exc
    return _parse_private_key(pem_bytes, source)


def _key_from_pem_literal(value: str) -> Ed25519PrivateKey:
    # Painéis de hospedagem costumam não aceitar quebras de linha reais em
    # variáveis de ambiente; aceita tanto "\n" escapado quanto literal.
    normalized = value.strip().replace("\\n", "\n")
    return _parse_private_key(normalized.encode(), "SERVER_SIGNING_PRIVATE_KEY_PEM")


def _legacy_dir_key(settings: Settings) -> Ed25519PrivateKey:
    """Compatibilidade com o formato antigo: par de arquivos em SERVER_KEYS_DIR."""
    keys_dir = Path(settings.server_keys_dir)
    keys_dir.mkdir(parents=True, exist_ok=True)
    private_path = keys_dir / SERVER_PRIVATE_KEY_FILE
    public_path = keys_dir / SERVER_PUBLIC_KEY_FILE

    if private_path.is_symlink() or public_path.is_symlink():
        raise ServerSigningKeyError("Secret mount de assinatura (SERVER_KEYS_DIR) não pode usar symlink.")

    if not private_path.exists():
        if settings.environment == "production":
            raise ServerSigningKeyError(
                "Nenhuma chave de assinatura válida foi encontrada. Configure "
                "SERVER_SIGNING_PRIVATE_KEY_PATH, SERVER_SIGNING_PRIVATE_KEY_B64 "
                "ou SERVER_SIGNING_PRIVATE_KEY_PEM."
            )
        # Fora de produção: gera uma vez e persiste em disco para os próximos boots.
        private_key = Ed25519PrivateKey.generate()
        private_path.write_bytes(
            private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        public_path.write_bytes(
            private_key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        private_path.chmod(0o600)
        public_path.chmod(0o644)
        return private_key

    if not public_path.exists():
        raise ServerSigningKeyError("Chave pública correspondente ausente no secret mount (SERVER_KEYS_DIR).")

    _reject_insecure_permissions(private_path, "SERVER_KEYS_DIR (chave privada)")
    private_key = _parse_private_key(private_path.read_bytes(), "SERVER_KEYS_DIR (chave privada)")
    try:
        public_key = serialization.load_pem_public_key(public_path.read_bytes())
    except (ValueError, TypeError) as exc:
        raise ServerSigningKeyError("Chave pública do secret mount (SERVER_KEYS_DIR) está malformada.") from exc
    if not isinstance(public_key, Ed25519PublicKey):
        raise ServerSigningKeyError("Chave pública do secret mount (SERVER_KEYS_DIR) não é Ed25519.")

    expected_public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    mounted_public = public_key.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if expected_public != mounted_public:
        raise ServerSigningKeyError(
            "Chaves privada e pública do secret mount (SERVER_KEYS_DIR) não correspondem."
        )
    return private_key


def load_server_signing_key(settings: Settings | None = None) -> Ed25519PrivateKey:
    """Carrega a chave Ed25519 do servidor pela primeira fonte configurada (veja ordem no topo do módulo)."""
    settings = settings or get_settings()

    path_value = settings.server_signing_private_key_path.strip()
    if path_value:
        return _key_from_path(path_value)

    b64_value = settings.server_signing_private_key_b64.strip()
    if b64_value:
        return _key_from_base64(b64_value)

    pem_value = settings.server_signing_private_key_pem.strip()
    if pem_value:
        return _key_from_pem_literal(pem_value)

    return _legacy_dir_key(settings)


def ensure_server_keys(settings: Settings | None = None) -> Ed25519PrivateKey:
    """Garante que a chave de assinatura do servidor está disponível e válida; retorna a chave privada."""
    try:
        return load_server_signing_key(settings)
    except ServerSigningKeyError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise ServerSigningKeyError("Falha ao carregar a chave de assinatura do servidor.") from exc


def server_sign(message: bytes, settings: Settings | None = None) -> str:
    """Assina uma mensagem com a chave privada do servidor (base64)."""
    private_key = ensure_server_keys(settings)
    return base64.b64encode(private_key.sign(message)).decode()


def server_public_key_pem(settings: Settings | None = None) -> str:
    private_key = ensure_server_keys(settings)
    return private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
