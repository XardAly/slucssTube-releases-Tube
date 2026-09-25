"""Gera os ZIPs de deploy do backend e frontend sem PowerShell."""

from __future__ import annotations

import os
import re
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SOURCE = ROOT / "backend"
BACKEND_DEPLOY = ROOT / "deploy" / "backend"
WEB_SOURCE = ROOT / "web"
WEB_DEPLOY = ROOT / "deploy" / "web"

SKIP_PARTS = {
    "__pycache__", ".pytest_cache", "node_modules", "cache",
    ".devdownloads", ".devkeys",
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".tsbuildinfo", ".apk"}


def _allowed(path: Path) -> bool:
    return (
        not any(part in SKIP_PARTS for part in path.parts)
        and path.suffix.lower() not in SKIP_SUFFIXES
    )


def _add_tree(zf: zipfile.ZipFile, source: Path, archive_root: Path | None = None) -> int:
    count = 0
    base = archive_root or source
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.is_symlink() or not _allowed(path.relative_to(source)):
            continue
        arcname = path.relative_to(base).as_posix()
        # Sempre gerado de forma coerente com main.js em package_web().
        if arcname == ".next/.deploy-version":
            continue
        zf.write(path, arcname)
        count += 1
    return count


def _add_file(zf: zipfile.ZipFile, source: Path, arcname: str | None = None) -> int:
    if not source.is_file():
        return 0
    zf.write(source, arcname or source.name)
    return 1


def _atomic_zip(output: Path, writer) -> tuple[int, int]:
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as zf:
            count = writer(zf)
        with zipfile.ZipFile(temporary) as zf:
            bad = zf.testzip()
            if bad:
                raise RuntimeError(f"Arquivo corrompido no ZIP: {bad}")
        os.replace(temporary, output)
        return count, output.stat().st_size
    finally:
        temporary.unlink(missing_ok=True)


def package_backend() -> tuple[int, int]:
    def write(zf: zipfile.ZipFile) -> int:
        count = 0
        for path in sorted(BACKEND_SOURCE.rglob("*")):
            relative = path.relative_to(BACKEND_SOURCE)
            if (
                not path.is_file()
                or path.is_symlink()
                or not _allowed(relative)
                or relative.parts[0] in {"tests", "nginx"}
                # O .env real entra uma única vez logo abaixo. O .env.example é
                # só referência e backend.zip seria um pacote aninhado indevido.
                or relative.name in {".env", ".env.example", "backend.zip"}
            ):
                continue
            lowered = relative.as_posix().lower()
            if path.suffix.lower() == ".json" and any(
                marker in lowered for marker in ("credential", "service-account", "google-drive")
            ):
                raise RuntimeError("Arquivo de credencial não pode entrar no pacote do backend")
            if path.suffix.lower() == ".json" and path.stat().st_size <= 1024 * 1024:
                content = path.read_text(encoding="utf-8", errors="ignore")
                if '"private_key"' in content or '"refresh_token"' in content:
                    raise RuntimeError("JSON com segredo não pode entrar no pacote do backend")
            zf.write(path, relative.as_posix())
            count += 1

        # Este é o pacote PRIVADO de deploy. A hospedagem atual carrega o .env
        # do próprio pacote e o worker precisa do cookie Netscape no caminho
        # /application/cookies.txt. Nunca publique este ZIP como artefato público.
        private_files = (
            (BACKEND_SOURCE / ".env", ".env"),
            (ROOT / "cookies.txt", "cookies.txt"),
        )
        for source, arcname in private_files:
            if not source.is_file():
                raise RuntimeError(
                    f"Arquivo privado obrigatório ausente no deploy: {source.name}"
                )
            count += _add_file(zf, source, arcname)

        for name in ("squarecloud.app",):
            count += _add_file(zf, BACKEND_DEPLOY / name, name)
        count += _add_file(
            zf,
            ROOT / "tools" / "google_drive_oauth_setup.py",
            "tools/google_drive_oauth_setup.py",
        )
        count += _add_file(
            zf,
            ROOT / "tools" / "setup-google-drive-oauth.ps1",
            "tools/setup-google-drive-oauth.ps1",
        )
        return count

    return _atomic_zip(ROOT / "square-backend.zip", write)


def package_web() -> tuple[int, int]:
    def write(zf: zipfile.ZipFile) -> int:
        count = 0
        for directory in (".next", "app", "lib", "public"):
            source = WEB_SOURCE / directory
            if source.is_dir():
                count += _add_tree(zf, source, WEB_SOURCE)
        for name in (
            "main.js", "middleware.ts", "next-env.d.ts", "next.config.mjs", "package-lock.json",
            "package.json", "tsconfig.json",
        ):
            count += _add_file(zf, WEB_SOURCE / name, name)
        main_source = (WEB_SOURCE / "main.js").read_text(encoding="utf-8")
        match = re.search(r'const\s+DEPLOY_VERSION\s*=\s*"([^"]+)"', main_source)
        if not match:
            raise RuntimeError("DEPLOY_VERSION não encontrado em web/main.js")
        zf.writestr(".next/.deploy-version", match.group(1))
        count += 1
        for name in ("squarecloud.app",):
            count += _add_file(zf, WEB_DEPLOY / name, name)
        return count

    return _atomic_zip(ROOT / "square-web.zip", write)


def _verify_env_is_shell_safe(zip_path: Path) -> None:
    """
    A hospedagem carrega o .env como script de shell.

    Valor com espaço e sem aspas quebra a injeção: o shell executa o resto da
    linha como comando ("do: command not found") e a aplicação recebe o valor
    truncado. Falhar aqui é melhor do que descobrir isso no log de produção.
    """
    with zipfile.ZipFile(zip_path) as zf:
        if ".env" not in zf.namelist():
            return
        conteudo = zf.read(".env").decode("utf-8", errors="replace")
    problemas = []
    for numero, linha in enumerate(conteudo.splitlines(), 1):
        if not linha.strip() or linha.lstrip().startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        if " " not in valor:
            continue
        if not ((valor.startswith('"') and valor.endswith('"'))
                or (valor.startswith("'") and valor.endswith("'"))):
            problemas.append(f"linha {numero}: {chave.strip()}")
    if problemas:
        raise RuntimeError(
            f"{zip_path.name}: valor com espaço sem aspas no .env "
            f"(quebra a injeção de variáveis): {', '.join(problemas)}"
        )


def _verify_required(
    zip_path: Path,
    required: set[str],
    *,
    allowed_private_files: set[str] | None = None,
) -> None:
    allowed_private_files = allowed_private_files or set()
    with zipfile.ZipFile(zip_path) as zf:
        entries = zf.namelist()
        names = set(entries)
        missing = required - names
        forbidden = [
            name for name in names
            if name not in allowed_private_files and (
                "__pycache__" in name
                or name.endswith((".pyc", ".pyo", ".pem", ".pfx", ".key"))
                or "node_modules/" in name
                or Path(name).suffix.lower() == ".apk"
                or name in {
                    ".env", ".env.example", ".env.production", "backend.zip",
                    "cookies.txt", "www.youtube.com_cookies.txt",
                }
            )
        ]
        if missing:
            raise RuntimeError(f"{zip_path.name}: arquivos obrigatórios ausentes: {sorted(missing)}")
        if forbidden:
            raise RuntimeError(f"{zip_path.name}: cache/dependência indevida no pacote")
        if len(entries) != len(names):
            raise RuntimeError(f"{zip_path.name}: há arquivos duplicados no pacote")


def _verify_netscape_cookies(zip_path: Path, name: str = "cookies.txt") -> None:
    with zipfile.ZipFile(zip_path) as zf:
        try:
            content = zf.read(name)
        except KeyError as exc:
            raise RuntimeError(f"{zip_path.name}: cookies de deploy ausentes") from exc
    first_line = content.splitlines()[0] if content else b""
    if not first_line.startswith(b"# Netscape HTTP Cookie File"):
        raise RuntimeError(
            f"{zip_path.name}: cookies não estão no formato Netscape esperado"
        )


def main() -> int:
    backend_count, backend_size = package_backend()
    web_count, web_size = package_web()
    _verify_required(
        ROOT / "square-backend.zip",
        {
            "main.py", "requirements.txt", "workers/gif_converter.py", "downloads/router.py",
            "tools/google_drive_oauth_setup.py", ".env", "cookies.txt",
            "tools/setup-google-drive-oauth.ps1",
        },
        allowed_private_files={".env", "cookies.txt"},
    )
    _verify_env_is_shell_safe(ROOT / "square-backend.zip")
    _verify_netscape_cookies(ROOT / "square-backend.zip")
    _verify_required(
        ROOT / "square-web.zip",
        {
            "main.js", "package.json", ".next/BUILD_ID", "app/page.tsx",
            "app/converter/page.tsx", ".next/server/app/converter/page.js",
            ".next/.deploy-version", "middleware.ts",
        },
    )
    print(f"square-backend.zip: {backend_count} arquivos, {backend_size} bytes")
    print(f"square-web.zip: {web_count} arquivos, {web_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
