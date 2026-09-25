"""Transient Android display-name events. Never attach names to database records."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from backend.api.deps import AuthContext, get_auth_context
from backend.api.config import get_settings
from backend.notifications.discord import _enqueue, _safe_text


class EventFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EventUser(EventFields):
    name: str = Field(min_length=1, max_length=60)


class EventDevice(EventFields):
    manufacturer: str = Field(default="Unknown Device", max_length=100)
    model: str = Field(default="Unknown Device", max_length=100)
    android_version: str = Field(min_length=1, max_length=40)


class EventApp(EventFields):
    version: str = Field(min_length=1, max_length=40)


class EventDownload(EventFields):
    title: str = Field(min_length=1, max_length=200)
    format: str = Field(min_length=1, max_length=16)
    quality: str = Field(min_length=1, max_length=60)
    status: Literal["started", "completed", "failed"]
    file_name: str | None = Field(default=None, max_length=240)
    error: str | None = Field(default=None, max_length=300)


class AndroidDownloadEvent(EventFields):
    event: Literal["download_started", "download_completed", "download_failed"]
    user: EventUser
    device: EventDevice
    app: EventApp
    download: EventDownload
    timestamp: AwareDatetime

    @model_validator(mode="after")
    def matching_status(self):
        if self.event != f"download_{self.download.status}":
            raise ValueError("Event and status must match")
        if self.download.status == "failed" and not self.download.error:
            raise ValueError("Failed events need an error reason")
        return self


def discord_payload(event: AndroidDownloadEvent) -> dict:
    title, color = {
        "started": ("📥 Download iniciado", 0xCF243B),
        "completed": ("✅ Download concluído", 0x38B87C),
        "failed": ("⚠️ Download falhou", 0xE45454),
    }[event.download.status]
    device = event.device
    model = device.model or "Unknown Device"
    manufacturer = device.manufacturer
    label = model if model.lower().startswith(manufacturer.lower()) else f"{manufacturer} {model}".strip()
    values = [
        ("Usuário", event.user.name, False),
        ("Dispositivo", label, False),
        ("Android", device.android_version, True),
        ("App", event.app.version, True),
        ("Arquivo", event.download.file_name or event.download.title, False),
        ("Qualidade", event.download.quality, True),
        ("Formato", event.download.format, True),
        # The Android timestamp already contains the user's local UTC offset.
        ("Horário", event.timestamp.strftime("%d/%m/%Y %H:%M"), False),
    ]
    if event.download.error:
        values.append(("Motivo", event.download.error, False))
    return {
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": title, "color": color, "timestamp": event.timestamp.isoformat(),
            "fields": [{"name": name, "value": _safe_text(value, 400), "inline": inline}
                       for name, value, inline in values],
        }],
    }


router = APIRouter(tags=["android"])


@router.post("/app/android/download-events", status_code=202)
def receive_event(event: AndroidDownloadEvent, _auth: AuthContext = Depends(get_auth_context)):
    # Existing signed request protection applies, but no name is persisted by this route.
    # Queue is bounded and memory-only; Discord downtime cannot delay media processing.
    url = get_settings().discord_download_webhook_url.strip()
    if url:
        try:
            _enqueue(url, discord_payload(event), 5.0)
        except Exception:
            pass  # Deliberately do not log event bodies or the display name.
    return {"accepted": True}
