"""Schemas Pydantic das sessões temporárias."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DeviceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    device_id: str = Field(min_length=8, max_length=64)
    installation_id: str = Field(min_length=8, max_length=64)
    hardware_hash: str | None = Field(default=None, min_length=16, max_length=128)
    public_key: str = Field(max_length=4096, description="Chave pública Ed25519 (PEM) gerada no dispositivo")
    platform: str = Field(pattern="^(windows|android|web)$")
    app_version: str = Field(max_length=16)
    # Attestation conforme a plataforma (web não possui)
    app_hash: str | None = Field(default=None, max_length=64)
    play_integrity_token: str | None = Field(default=None, max_length=16384)


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device: DeviceInfo


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str = Field(max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
