"""Autoriza uma conta Google pessoal e prepara o armazenamento privado do XARD."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx


AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
LEGACY_AUTH_URL = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
FOLDER_MIME = "application/vnd.google-apps.folder"
DEFAULT_OUTPUT = Path(".devkeys/google-drive-personal.env")


class SetupError(RuntimeError):
    pass


def _load_client(path: Path) -> tuple[str, str]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 128 * 1024:
        raise SetupError("O JSON OAuth informado não é um arquivo válido.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        installed = payload["installed"]
        client_id = str(installed["client_id"])
        client_secret = str(installed["client_secret"])
        if (
            installed.get("auth_uri") not in {AUTH_URL, LEGACY_AUTH_URL}
            or installed.get("token_uri") != TOKEN_URL
        ):
            raise KeyError
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise SetupError("Use o JSON de um OAuth Client do tipo Aplicativo para computador.") from exc
    if any(char in client_id + client_secret for char in "\r\n"):
        raise SetupError("O JSON OAuth contém valores inválidos.")
    return client_id, client_secret


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _receive_code(client_id: str, *, timeout_seconds: int = 300) -> tuple[str, str, str]:
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce()
    result: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - nome exigido por BaseHTTPRequestHandler
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if parsed.path != "/oauth2/callback":
                self.send_response(404)
                self.end_headers()
                return
            if params.get("state", [""])[0] != state:
                result["error"] = "state_invalido"
            elif params.get("error"):
                result["error"] = params["error"][0]
            elif params.get("code"):
                result["code"] = params["code"][0]
            else:
                result["error"] = "codigo_ausente"
            body = (
                b"<!doctype html><meta charset=utf-8><title>XARD OAuth</title>"
                b"<p>Autorizacao recebida. Voce pode fechar esta janela e voltar ao terminal.</p>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    with HTTPServer(("127.0.0.1", 0), CallbackHandler) as server:
        server.timeout = 1
        redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth2/callback"
        authorization_params = {
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'scope': DRIVE_FILE_SCOPE,
            'access_type': 'offline',
            'prompt': 'consent',
            'include_granted_scopes': 'false',
            'state': state,
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
        }
        authorization_url = f"{AUTH_URL}?{urlencode(authorization_params)}"
        print("Abrindo a autorização oficial do Google no navegador...")
        print(f"AUTHORIZATION_URL={authorization_url}", flush=True)
        if not webbrowser.open(authorization_url, new=1):
            print("O navegador não abriu. Acesse esta URL temporária:")
            print(authorization_url)
        deadline = time.monotonic() + timeout_seconds
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if result.get("error"):
        raise SetupError(f"Autorização não concluída ({result['error']}).")
    if not result.get("code"):
        raise SetupError("Tempo de autorização esgotado; execute novamente.")
    return result["code"], verifier, redirect_uri


def _exchange_code(
    client: httpx.Client,
    *,
    code: str,
    verifier: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
) -> tuple[str, str]:
    response = client.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
    )
    if response.status_code != 200:
        raise SetupError("O Google recusou a troca do código OAuth; nenhum token foi salvo.")
    try:
        payload = response.json()
        access_token = str(payload["access_token"])
        refresh_token = str(payload["refresh_token"])
        scopes = set(str(payload.get("scope") or "").split())
    except (ValueError, KeyError, TypeError) as exc:
        raise SetupError("O Google não retornou um refresh token válido.") from exc
    if DRIVE_FILE_SCOPE not in scopes:
        raise SetupError("O escopo drive.file não foi autorizado.")
    return access_token, refresh_token


def _drive_request(
    client: httpx.Client,
    access_token: str,
    method: str,
    path: str,
    **kwargs: object,
) -> httpx.Response:
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["Authorization"] = f"Bearer {access_token}"
    response: httpx.Response | None = None
    for attempt in range(5):
        try:
            response = client.request(method, f"{DRIVE_API}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            if attempt == 4:
                raise SetupError("Falha de rede ao preparar o Google Drive.") from exc
        else:
            if response.status_code < 400:
                return response
            if response.status_code not in {408, 429, 500, 502, 503, 504}:
                raise SetupError("O Google Drive recusou a preparação da pasta privada.")
        time.sleep(min(2**attempt, 8) + secrets.randbelow(250) / 1000)
    raise SetupError("O Google Drive permaneceu indisponível durante a preparação.")


def _validate_private_folder(client: httpx.Client, access_token: str, folder_id: str) -> None:
    metadata = _drive_request(
        client,
        access_token,
        "GET",
        f"/files/{folder_id}",
        params={"fields": "id,mimeType,trashed,capabilities(canAddChildren)"},
    ).json()
    if (
        metadata.get("mimeType") != FOLDER_MIME
        or metadata.get("trashed")
        or not (metadata.get("capabilities") or {}).get("canAddChildren")
    ):
        raise SetupError("A pasta configurada não é uma pasta privada gravável.")
    permissions = _drive_request(
        client,
        access_token,
        "GET",
        f"/files/{folder_id}/permissions",
        params={"fields": "permissions(type,role)", "pageSize": "100"},
    ).json()
    if any(item.get("type") in {"anyone", "domain"} for item in permissions.get("permissions") or []):
        raise SetupError("A pasta possui permissão pública ou de domínio; remova-a antes de continuar.")


def _find_folder(
    client: httpx.Client,
    access_token: str,
    *,
    parent_id: str,
    property_name: str,
    property_value: str,
) -> str | None:
    escaped = property_value.replace("'", "\\'")
    query = (
        f"'{parent_id}' in parents and trashed = false and mimeType = '{FOLDER_MIME}' and "
        f"appProperties has {{ key='{property_name}' and value='{escaped}' }}"
    )
    payload = _drive_request(
        client,
        access_token,
        "GET",
        "/files",
        params={"q": query, "spaces": "drive", "pageSize": "10", "fields": "files(id)"},
    ).json()
    ids = sorted(str(item.get("id") or "") for item in payload.get("files") or [] if item.get("id"))
    return ids[0] if ids else None


def _create_folder(
    client: httpx.Client,
    access_token: str,
    *,
    name: str,
    parent_id: str,
    app_properties: dict[str, str],
) -> str:
    payload = _drive_request(
        client,
        access_token,
        "POST",
        "/files",
        params={"fields": "id"},
        json={
            "name": name,
            "mimeType": FOLDER_MIME,
            "parents": [parent_id],
            "appProperties": app_properties,
        },
    ).json()
    folder_id = str(payload.get("id") or "")
    if not folder_id:
        raise SetupError("O Google Drive não confirmou a criação da pasta.")
    return folder_id


def _prepare_folders(
    client: httpx.Client,
    access_token: str,
    *,
    configured_root: str | None,
) -> dict[str, str]:
    root_id = configured_root
    if root_id:
        _validate_private_folder(client, access_token, root_id)
    else:
        root_id = _find_folder(
            client,
            access_token,
            parent_id="root",
            property_name="xardSystemRoot",
            property_value="1",
        )
        if not root_id:
            root_id = _create_folder(
                client,
                access_token,
                name="XARD Private Storage",
                parent_id="root",
                app_properties={"xardManaged": "1", "xardSystemRoot": "1"},
            )
    _validate_private_folder(client, access_token, root_id)
    folders = {"root": root_id}
    for name in ("videos", "gifs", "temporary", "recovery"):
        folder_id = _find_folder(
            client,
            access_token,
            parent_id=root_id,
            property_name="xardSystemFolder",
            property_value=name,
        )
        if not folder_id:
            folder_id = _create_folder(
                client,
                access_token,
                name=name,
                parent_id=root_id,
                app_properties={"xardManaged": "1", "xardSystemFolder": name},
            )
        _validate_private_folder(client, access_token, folder_id)
        folders[name] = folder_id
    return folders


def _dotenv_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _write_env(
    path: Path,
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    folders: dict[str, str],
    overwrite: bool,
) -> None:
    if path.exists() and not overwrite:
        raise SetupError(f"O arquivo {path} já existe; use --overwrite para substituí-lo.")
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "STORAGE_BACKEND": "google_drive",
        "GOOGLE_DRIVE_AUTH_MODE": "oauth",
        "GOOGLE_DRIVE_CLIENT_ID": client_id,
        "GOOGLE_DRIVE_CLIENT_SECRET": client_secret,
        "GOOGLE_DRIVE_REFRESH_TOKEN": refresh_token,
        "GOOGLE_DRIVE_SCOPE": DRIVE_FILE_SCOPE,
        "GOOGLE_DRIVE_ROOT_FOLDER_ID": folders["root"],
        "GOOGLE_DRIVE_VIDEOS_FOLDER_ID": folders["videos"],
        "GOOGLE_DRIVE_GIFS_FOLDER_ID": folders["gifs"],
    }
    content = "".join(f"{key}={_dotenv_value(value)}\n" for key, value in values.items())
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-json", type=Path, required=True, help="JSON OAuth Desktop baixado do Google")
    parser.add_argument("--root-folder-id", help="Raiz previamente criada por este mesmo cliente OAuth")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Arquivo secreto de saída")
    parser.add_argument("--overwrite", action="store_true", help="Substitui o arquivo de saída existente")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client_id, client_secret = _load_client(args.client_json)
        code, verifier, redirect_uri = _receive_code(client_id)
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            access_token, refresh_token = _exchange_code(
                client,
                code=code,
                verifier=verifier,
                redirect_uri=redirect_uri,
                client_id=client_id,
                client_secret=client_secret,
            )
            folders = _prepare_folders(
                client,
                access_token,
                configured_root=args.root_folder_id,
            )
        _write_env(
            args.output,
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            folders=folders,
            overwrite=args.overwrite,
        )
    except (SetupError, OSError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    print(f"Autorização concluída. Segredos salvos somente em: {args.output}")
    print("Nenhum token foi exibido. Copie essas variáveis para o secret manager do servidor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
