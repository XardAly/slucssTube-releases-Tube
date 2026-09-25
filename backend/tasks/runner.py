"""Ponto de entrada isolado de cada processo pesado."""

from __future__ import annotations

import os


def run_task_process(download_id: str, operation_type: str) -> None:
    """Executa uma tarefa em processo dedicado, fora do event loop da API."""
    if os.name == "posix":
        os.setsid()
    os.environ["XARD_SERVICE"] = "task"

    from backend.observability import setup_logging

    setup_logging("task")

    # O processo usa conexões próprias. Isso também torna o runner seguro em
    # plataformas que escolham fork em vez de spawn no futuro.
    from backend.database.session import engine

    engine.dispose(close=False)
    try:
        from backend.workers.tasks import run_operation

        run_operation(download_id, operation_type)
    finally:
        engine.dispose()
