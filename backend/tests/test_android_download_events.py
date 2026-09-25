from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.api.deps import get_auth_context
from backend.notifications import android_events as events


def payload(status="completed"):
    return {
        "event": f"download_{status}", "user": {"name": "Lucas"},
        "device": {"manufacturer": "Samsung", "model": "Galaxy S24", "android_version": "16"},
        "app": {"version": "1.2.1"},
        "download": {"title": "Vídeo", "format": "MP4", "quality": "1080p", "status": status,
                     **({"error": "Sem conexão"} if status == "failed" else {})},
        "timestamp": "2026-09-09T09:50:00-03:00",
    }


@pytest.mark.parametrize("status", ["started", "completed", "failed"])
def test_embeds_include_name_device_media_time_without_sensitive_identifiers(status):
    message = events.discord_payload(events.AndroidDownloadEvent.model_validate(payload(status)))
    fields = {f["name"]: f["value"] for f in message["embeds"][0]["fields"]}
    assert fields["Usuário"] == "Lucas"
    assert fields["Dispositivo"] == "Samsung Galaxy S24"
    assert fields["Horário"] == "09/09/2026 09:50"
    assert fields["Formato"] == "MP4"
    assert message["allowed_mentions"] == {"parse": []}
    if status == "failed":
        assert fields["Motivo"] == "Sem conexão"


def test_payload_rejects_identifiers_mismatched_status_and_naive_dates():
    for mutate in (
        lambda p: p["device"].update(imei="123"),
        lambda p: p["user"].update(name="   "),
        lambda p: p.update(event="download_failed"),
        lambda p: p.update(timestamp="2026-09-09T09:50:00"),
    ):
        value = deepcopy(payload())
        mutate(value)
        with pytest.raises(ValidationError):
            events.AndroidDownloadEvent.model_validate(value)


@pytest.mark.parametrize("offline", [False, True])
def test_route_is_best_effort_and_has_no_database_dependency(monkeypatch, offline):
    api = FastAPI()
    api.include_router(events.router)
    api.dependency_overrides[get_auth_context] = lambda: SimpleNamespace()
    delivered = []
    monkeypatch.setattr(events, "get_settings", lambda: SimpleNamespace(discord_download_webhook_url="https://discord.com/api/webhooks/1/test"))
    def enqueue(*args):
        if offline:
            raise OSError("offline")
        delivered.append(args)
    monkeypatch.setattr(events, "_enqueue", enqueue)
    with TestClient(api) as client:
        response = client.post("/app/android/download-events", json=payload())
    assert response.status_code == 202
    assert bool(delivered) is not offline
