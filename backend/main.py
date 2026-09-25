"""Entrada única de produção da API e do gerenciador interno de tarefas."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent


def _register_alias(base: Path) -> None:
    """Faz imports `backend.*` funcionarem também no deploy plano."""
    package = types.ModuleType("backend")
    package.__path__ = [str(base)]
    sys.modules["backend"] = package
    sys.path.insert(0, str(base))


def _setup_import_path() -> None:
    for base in (SCRIPT_DIR.parent, SCRIPT_DIR):
        if (base / "backend" / "api" / "main.py").is_file():
            sys.path.insert(0, str(base))
            return
    bases = [SCRIPT_DIR] + sorted(
        directory for directory in SCRIPT_DIR.iterdir() if directory.is_dir()
    )
    for base in bases:
        if (base / "api" / "main.py").is_file() and (base / "workers").is_dir():
            _register_alias(base)
            return
    raise SystemExit("[main] Estrutura de deploy do backend não reconhecida.")


_setup_import_path()


def _load_env() -> None:
    """Carrega o primeiro .env sem sobrepor o painel da hospedagem."""
    candidates = (
        Path.cwd() / ".env",
        SCRIPT_DIR / ".env",
        SCRIPT_DIR.parent / ".env",
    )
    for env_path in candidates:
        if not env_path.is_file():
            continue
        # Accept quoted values (including JSON changelogs) without treating
        # their quotes as part of integers, paths or URLs. Keep panel overrides.
        load_dotenv(env_path, override=False, interpolate=False, encoding="utf-8-sig")
        return


def _ensure_dirs() -> None:
    for variable in ("SERVER_KEYS_DIR", "DOWNLOADS_DIR"):
        path = os.environ.get(variable, "")
        if path:
            Path(path).mkdir(parents=True, exist_ok=True)


def _run_api() -> None:
    import uvicorn

    from backend.api.config import get_settings
    from backend.api.main import app

    settings = get_settings()
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "80")),
        log_config=None,
        ws="wsproto",
        ws_max_size=settings.websocket_max_message_bytes,
        limit_concurrency=settings.api_limit_concurrency,
        backlog=settings.api_backlog,
        timeout_graceful_shutdown=settings.api_graceful_shutdown_seconds,
    )


def main() -> None:
    _load_env()
    _ensure_dirs()
    _run_api()


if __name__ == "__main__":
    main()
