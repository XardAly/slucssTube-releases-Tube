"""Testes unitários das estatísticas persistentes de download."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from backend.downloads.service import public_download_stats, record_download_completion


class _QueueResult:
    def one(self):
        return 4, 3


class _PublicStatsSession:
    def execute(self, _statement):
        return _QueueResult()

    def get(self, _model, _key):
        return SimpleNamespace(value=42)


def test_public_download_stats_returns_only_aggregates():
    result = public_download_stats(_PublicStatsSession())

    assert result["users_in_queue"] == 3
    assert result["downloads_in_queue"] == 4
    assert result["site_downloads_completed"] == 42
    assert result["updated_at"].tzinfo is not None


class _ClaimResult:
    rowcount = 1


class _PlatformResult:
    def scalar_one_or_none(self):
        return "web"


class _CompletionSession:
    def __init__(self):
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        if len(self.statements) == 1:
            return _ClaimResult()
        if len(self.statements) == 2:
            return _PlatformResult()
        return SimpleNamespace()


def test_web_completion_is_counted_once_per_download():
    session = _CompletionSession()
    download = SimpleNamespace(
        id=uuid.uuid4(),
        device_id=uuid.uuid4(),
        statistics_counted_at=None,
    )

    assert record_download_completion(session, download) is True
    assert download.statistics_counted_at is not None
    assert len(session.statements) == 3

    assert record_download_completion(session, download) is False
    assert len(session.statements) == 3
