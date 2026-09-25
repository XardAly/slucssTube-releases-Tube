"""Schemas dos endpoints de download."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator
from backend.security.url_validation import UnsafeUrl, validate_public_http_url

ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "instagram.com",
    "www.instagram.com",
    "pinterest.com",
    "www.pinterest.com",
    "pin.it",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
    "x.com",
    "www.x.com",
    "mobile.x.com",
}

# Pinterest usa subdominios de idioma/regiao (br.pinterest.com, fr.pinterest.com,
# etc.) alem do www — todos apontam pro mesmo site, entao aceitamos qualquer
# subdominio de pinterest.com em vez de listar cada pais manualmente.
_PINTEREST_SUFFIX = ".pinterest.com"


def _host_allowed(host: str) -> bool:
    return host in ALLOWED_HOSTS or host.endswith(_PINTEREST_SUFFIX)


class VideoInfoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: HttpUrl

    @field_validator("url")
    @classmethod
    def host_allowed(cls, v: HttpUrl) -> HttpUrl:
        if not _host_allowed((v.host or "").lower()):
            raise ValueError(
                "Link nao suportado. Envie um link do YouTube, TikTok, "
                "Instagram, Pinterest ou X (Twitter)."
            )
        try:
            validate_public_http_url(str(v))
        except UnsafeUrl as exc:
            raise ValueError(str(exc)) from exc
        return v


class DownloadCreateRequest(VideoInfoRequest):
    # O cliente escolhe apenas modo/qualidade; o format_spec do yt-dlp
    # é montado exclusivamente no servidor.
    # "p" = foto (pins de imagem do Pinterest etc., sem stream de video/audio)
    mode: Literal["va", "v", "a", "p"] = "va"
    height: int | None = Field(default=None, ge=144, le=4320)
    format_id: str | None = Field(default=None, max_length=32)
    video_format: Literal["mp4", "mkv", "webm"] = "mp4"
    audio_format: Literal["mp3", "m4a", "opus", "wav", "flac"] = "mp3"
    # Challenge-response obrigatório para criar downloads
    challenge: str = Field(max_length=128)
    challenge_signature: str = Field(max_length=512)

    @field_validator("format_id")
    @classmethod
    def format_id_safe(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"[a-zA-Z0-9_-]+", v):
            raise ValueError("Formato inválido.")
        return v


class GifOptions(BaseModel):
    """Opções fechadas aceitas pelo pipeline seguro de GIF."""

    model_config = ConfigDict(extra="forbid")

    start: float = Field(default=0, ge=0, le=7200)
    end: float = Field(default=6, gt=0, le=7200)
    resolution: Literal["original", "360p", "480p", "720p", "custom"] = "480p"
    custom_width: int = Field(default=640, ge=160, le=1280)
    custom_height: int = Field(default=480, ge=120, le=720)
    fps: Literal[5, 10, 15, 20, 24, 30] = 15
    colors: Literal[32, 64, 128, 256] = 256
    loop: Literal["infinite", "once", "custom"] = "infinite"
    loop_count: int = Field(default=1, ge=1, le=20)
    speed: Literal[0.5, 0.75, 1.0, 1.25, 1.5, 2.0] = 1.0
    enhancement: Literal["off", "light", "medium", "strong"] = "light"
    antialias: bool = True
    brightness: int = Field(default=0, ge=-100, le=100)
    contrast: int = Field(default=0, ge=-100, le=100)
    saturation: int = Field(default=0, ge=-100, le=100)
    gamma: float = Field(default=1.0, ge=0.5, le=2.0)
    sharpen: int = Field(default=0, ge=0, le=100)
    denoise: Literal["off", "light", "medium", "strong"] = "off"
    dither: Literal["auto", "none", "bayer", "sierra", "floyd_steinberg"] = "auto"
    max_size_mb: int | None = Field(default=None, ge=1, le=25)

    @model_validator(mode="after")
    def selected_range_is_safe(self) -> "GifOptions":
        if self.end <= self.start:
            raise ValueError("O final do trecho deve ser maior que o início.")
        if self.end - self.start > 30:
            raise ValueError("O trecho do GIF pode ter no máximo 30 segundos.")
        return self


class UpscaleOptions(BaseModel):
    """Opções mínimas autorizadas para o Upscale executado no cliente."""

    model_config = ConfigDict(extra="forbid")

    media_kind: Literal["image", "video"]
    model: Literal["realesr_animevideov3", "realcugan"]
    scale: Literal[2, 3, 4]
    output_format: Literal["auto", "mp4", "hevc", "prores"]
    model_version: str | None = Field(default=None, min_length=1, max_length=64)
    engine_version: str | None = Field(default=None, min_length=1, max_length=64)


class ConversionCreateRequest(VideoInfoRequest):
    options: GifOptions = Field(default_factory=GifOptions)
    challenge: str = Field(max_length=128)
    challenge_signature: str = Field(max_length=512)


class LocalOperationAuthorizeRequest(BaseModel):
    """Pedido fechado para uma operação executada por um cliente oficial."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["download", "gif", "compatibility", "upscale"]
    url: HttpUrl | None = None
    mode: Literal["va", "v", "a", "p"] | None = None
    height: int | None = Field(default=None, ge=144, le=4320)
    format_id: str | None = Field(default=None, max_length=32)
    video_format: Literal["mp4", "mkv", "webm"] = "mp4"
    audio_format: Literal["mp3", "m4a", "opus", "wav", "flac"] = "mp3"
    gif_options: GifOptions | None = None
    upscale_options: UpscaleOptions | None = None
    challenge: str = Field(max_length=128)
    challenge_signature: str = Field(max_length=512)

    @field_validator("url")
    @classmethod
    def local_url_allowed(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is None:
            return value
        if not _host_allowed((value.host or "").lower()):
            raise ValueError("Link não suportado.")
        try:
            validate_public_http_url(str(value))
        except UnsafeUrl as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("format_id")
    @classmethod
    def local_format_id_safe(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[a-zA-Z0-9_-]+", value):
            raise ValueError("Formato inválido.")
        return value

    @model_validator(mode="after")
    def operation_fields_match(self) -> "LocalOperationAuthorizeRequest":
        if self.operation == "download":
            if self.url is None or self.mode is None:
                raise ValueError("URL e modo são obrigatórios para download.")
            if self.gif_options is not None:
                raise ValueError("Opções de GIF não pertencem a um download.")
            if self.upscale_options is not None:
                raise ValueError("Opções de Upscale não pertencem a um download.")
        elif self.operation == "gif":
            if self.url is not None or self.mode is not None:
                raise ValueError("GIF local não aceita URL ou modo de download.")
            if self.gif_options is None:
                raise ValueError("As opções do GIF são obrigatórias.")
            if self.upscale_options is not None:
                raise ValueError("Opções de Upscale não pertencem a um GIF.")
        elif self.operation == "upscale":
            if any((self.url, self.mode, self.height, self.format_id, self.gif_options)):
                raise ValueError("O Upscale não aceita campos de download ou GIF.")
            if self.upscale_options is None:
                raise ValueError("As opções do Upscale são obrigatórias.")
        elif any((
            self.url, self.mode, self.height, self.format_id,
            self.gif_options, self.upscale_options,
        )):
            raise ValueError("A conversão de compatibilidade não aceita esses campos.")
        return self


class LocalOperationProgressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["running", "completed", "failed", "cancelled"]
    stage: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_]+$")
    progress: float = Field(ge=0, le=100)
    message: str | None = Field(default=None, max_length=160)
