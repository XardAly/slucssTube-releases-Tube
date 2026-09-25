"""
Modelos do banco de dados (PostgreSQL).

Sistema sem contas de usuário: toda identidade é o DISPOSITIVO.
Tabelas: devices, sessions, downloads, api_requests, security_logs, bans.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.session import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DeviceStatus(str, enum.Enum):
    authorized = "authorized"
    blocked = "blocked"
    suspicious = "suspicious"


class DownloadStatus(str, enum.Enum):
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class StorageState(str, enum.Enum):
    local_ready = "local_ready"
    remote_uploading = "remote_uploading"
    remote_ready = "remote_ready"
    local_delete_pending = "local_delete_pending"
    local_fallback = "local_fallback"
    completed = "completed"
    delete_pending = "delete_pending"
    deleted = "deleted"
    security_blocked = "security_blocked"
    remote_missing = "remote_missing"


class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(120))
    message: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    target: Mapped[str] = mapped_column(String(16), default="all", index=True)  # all | web | desktop
    display_mode: Mapped[str] = mapped_column(String(16), default="popup")  # popup | banner
    cta_label: Mapped[str | None] = mapped_column(String(40))
    cta_url: Mapped[str | None] = mapped_column(Text)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    installation_id: Mapped[str] = mapped_column(String(64), index=True)
    hardware_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    # Chave pública Ed25519 gerada pelo dispositivo na instalação.
    # A privada correspondente nunca sai do dispositivo.
    public_key: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(String(16))  # windows | android | web
    app_version: Mapped[str] = mapped_column(String(16))
    status: Mapped[DeviceStatus] = mapped_column(Enum(DeviceStatus), default=DeviceStatus.authorized)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SessionToken(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"), index=True)
    # Nunca guardamos o refresh token puro — apenas SHA-256.
    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class SystemCounter(Base):
    """Contador persistente que não depende da retenção das tarefas."""

    __tablename__ = "system_counters"
    __table_args__ = (
        CheckConstraint("value >= 0", name="ck_system_counters_value_nonnegative"),
    )

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )


class LocalOperation(Base):
    """Autoriza e acompanha trabalho executado exclusivamente no desktop."""

    __tablename__ = "local_operations"
    __table_args__ = (
        CheckConstraint(
            "progress >= 0 AND progress <= 100",
            name="ck_local_operations_progress_range",
        ),
        Index("ix_local_operations_device_created", "device_id", "created_at"),
        Index("ix_local_operations_status_expires", "status", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"), index=True)
    operation: Mapped[str] = mapped_column(String(24))
    url: Mapped[str | None] = mapped_column(Text)
    parameters: Mapped[str] = mapped_column(Text, default="{}")
    permit_nonce_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="authorized", index=True)
    stage: Mapped[str] = mapped_column(String(32), default="authorized")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    progress_message: Mapped[str | None] = mapped_column(String(160))
    client_ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MediaCache(Base):
    """Saída final compartilhável, persistida no PostgreSQL e no Drive privado."""

    __tablename__ = "media_cache"
    __table_args__ = (
        CheckConstraint("request_count >= 0", name="ck_media_cache_request_count"),
        CheckConstraint("hit_count >= 0", name="ck_media_cache_hit_count"),
        CheckConstraint("external_downloads >= 0", name="ck_media_cache_external_downloads"),
        CheckConstraint("bytes_reused >= 0", name="ck_media_cache_bytes_reused"),
        CheckConstraint("bytes_downloaded_external >= 0", name="ck_media_cache_bytes_external"),
        CheckConstraint("file_size IS NULL OR file_size >= 0", name="ck_media_cache_file_size"),
        Index("ix_media_cache_state_updated", "state", "updated_at"),
        Index("ix_media_cache_owner", "owner_download_id"),
        Index("ix_media_cache_media", "platform", "media_id"),
        Index(
            "ux_media_cache_storage_key",
            "storage_key",
            unique=True,
            postgresql_where=text("storage_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    cache_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    platform: Mapped[str] = mapped_column(String(32))
    media_id: Mapped[str] = mapped_column(String(255))
    operation_type: Mapped[str] = mapped_column(String(32))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    request_parameters: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), default="processing", index=True)
    owner_download_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    stage: Mapped[str | None] = mapped_column(String(24))
    title: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(4))
    output_format: Mapped[str | None] = mapped_column(String(8))
    conversion_options: Mapped[str | None] = mapped_column(Text)
    storage_backend: Mapped[str | None] = mapped_column(String(24))
    storage_key: Mapped[str | None] = mapped_column(Text)
    storage_reserved_key: Mapped[str | None] = mapped_column(Text)
    remote_parent_id: Mapped[str | None] = mapped_column(Text)
    remote_checksum: Mapped[str | None] = mapped_column(String(128))
    remote_mime_type: Mapped[str | None] = mapped_column(String(255))
    remote_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    request_count: Mapped[int] = mapped_column(BigInteger, default=0)
    hit_count: Mapped[int] = mapped_column(BigInteger, default=0)
    external_downloads: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_reused: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_downloaded_external: Mapped[int] = mapped_column(BigInteger, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Download(Base):
    __tablename__ = "downloads"
    __table_args__ = (
        CheckConstraint("progress IS NULL OR (progress >= 0 AND progress <= 100)", name="ck_downloads_progress_range"),
        CheckConstraint("attempts >= 0", name="ck_downloads_attempts_nonnegative"),
        CheckConstraint("revision >= 1", name="ck_downloads_revision_positive"),
        CheckConstraint("file_size IS NULL OR file_size >= 0", name="ck_downloads_file_size_nonnegative"),
        CheckConstraint("remote_size IS NULL OR remote_size >= 0", name="ck_downloads_remote_size_nonnegative"),
        Index("ix_downloads_device_created", "device_id", "created_at"),
        Index(
            "ix_downloads_processing_heartbeat",
            "heartbeat_at",
            postgresql_where=text("status = 'processing'"),
        ),
        Index(
            "ix_downloads_queued_created",
            "queued_at",
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_downloads_operation_status", "operation_type", "status"),
        Index("ix_downloads_media_cache", "media_cache_id", "cache_role"),
        Index(
            "ux_downloads_active_deduplication",
            "device_id",
            "deduplication_key",
            unique=True,
            postgresql_where=text(
                "deduplication_key IS NOT NULL AND status IN ('queued', 'processing')"
            ),
        ),
        Index(
            "ix_downloads_expiration_pending",
            "completed_at",
            postgresql_where=text("file_path IS NOT NULL"),
        ),
        Index(
            "ix_downloads_completed_recent",
            "completed_at",
            postgresql_where=text("status = 'completed'"),
        ),
        Index("ix_downloads_storage_pending", "storage_state", "updated_at"),
        Index("ix_downloads_storage_expires", "expires_at", "storage_state"),
        Index(
            "ux_downloads_storage_idempotency",
            "storage_idempotency_key",
            unique=True,
            postgresql_where=text("storage_idempotency_key IS NOT NULL"),
        ),
        Index(
            "ux_downloads_source_storage_idempotency",
            "source_storage_idempotency_key",
            unique=True,
            postgresql_where=text("source_storage_idempotency_key IS NOT NULL"),
        ),
        Index(
            "ux_downloads_storage_reserved_key",
            "storage_reserved_key",
            unique=True,
            postgresql_where=text("storage_reserved_key IS NOT NULL"),
        ),
        Index(
            "ux_downloads_source_storage_reserved_key",
            "source_storage_reserved_key",
            unique=True,
            postgresql_where=text("source_storage_reserved_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"), index=True)
    url: Mapped[str] = mapped_column(Text)
    format_spec: Mapped[str] = mapped_column(String(128), default="best")
    # "va" = vídeo+áudio | "v" = só vídeo | "a" = só áudio
    mode: Mapped[str] = mapped_column(String(4), default="va")
    # contêiner de vídeo (mp4/mkv/webm) ou formato de áudio (mp3/m4a/...)
    output_format: Mapped[str | None] = mapped_column(String(8))
    # JSON validado com as opções/resultados do conversor. Nullable mantém
    # compatibilidade total com downloads antigos.
    conversion_options: Mapped[str | None] = mapped_column(Text)
    # Upload temporario da conversao GIF. O caminho nunca e exposto ao cliente.
    source_path: Mapped[str | None] = mapped_column(Text)
    operation_type: Mapped[str] = mapped_column(String(32), default="download", index=True)
    deduplication_key: Mapped[str | None] = mapped_column(String(64))
    media_cache_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("media_cache.id", ondelete="SET NULL"), index=True,
    )
    cache_role: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[DownloadStatus] = mapped_column(Enum(DownloadStatus), default=DownloadStatus.queued, index=True)
    progress: Mapped[float | None] = mapped_column(Float)
    progress_message: Mapped[str | None] = mapped_column(String(255))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    stage: Mapped[str | None] = mapped_column(String(24))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(255))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(Text)  # caminho interno; nunca exposto
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    storage_backend: Mapped[str] = mapped_column(String(24), default="local")
    storage_state: Mapped[str] = mapped_column(String(32), default=StorageState.local_ready.value)
    # Chave interna do backend (ID do Drive no modo remoto); nunca é serializada.
    storage_key: Mapped[str | None] = mapped_column(Text)
    storage_reserved_key: Mapped[str | None] = mapped_column(Text)
    storage_idempotency_key: Mapped[str | None] = mapped_column(String(200))
    remote_parent_id: Mapped[str | None] = mapped_column(Text)
    remote_size: Mapped[int | None] = mapped_column(BigInteger)
    remote_checksum: Mapped[str | None] = mapped_column(String(128))
    remote_mime_type: Mapped[str | None] = mapped_column(String(255))
    remote_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    local_delete_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    file_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    storage_error: Mapped[str | None] = mapped_column(Text)
    # Fonte opcional preservada por política; nunca é entregue ao cliente.
    source_storage_backend: Mapped[str | None] = mapped_column(String(24))
    source_storage_key: Mapped[str | None] = mapped_column(Text)
    source_storage_reserved_key: Mapped[str | None] = mapped_column(Text)
    source_storage_idempotency_key: Mapped[str | None] = mapped_column(String(200))
    source_remote_parent_id: Mapped[str | None] = mapped_column(Text)
    source_remote_size: Mapped[int | None] = mapped_column(BigInteger)
    source_remote_checksum: Mapped[str | None] = mapped_column(String(128))
    source_remote_mime_type: Mapped[str | None] = mapped_column(String(255))
    source_remote_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_time: Mapped[float | None] = mapped_column(Float)  # segundos
    title: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    interrupted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    statistics_counted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


@event.listens_for(Download, "before_update")
def _increment_download_revision(_mapper, _connection, target: Download) -> None:
    """Uma revisão por flush permite descartar eventos duplicados/atrasados."""
    target.revision = (target.revision or 0) + 1


class StorageCursor(Base):
    """Cursor persistente para auditorias paginadas do armazenamento remoto."""

    __tablename__ = "storage_cursors"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )


class ApiRequest(Base):
    __tablename__ = "api_requests"
    __table_args__ = (
        Index("ix_api_requests_device_timestamp", "device_id", "timestamp"),
        Index("ix_api_requests_ip_timestamp", "ip", "timestamp"),
        Index(
            "ix_api_requests_device_endpoint_timestamp",
            "device_id", "endpoint", "timestamp",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"), index=True)
    endpoint: Mapped[str] = mapped_column(String(128), index=True)
    ip: Mapped[str | None] = mapped_column(String(45))
    country: Mapped[str | None] = mapped_column(String(2))
    asn: Mapped[str | None] = mapped_column(String(16))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    request_id: Mapped[str | None] = mapped_column(String(64))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class SecurityLog(Base):
    __tablename__ = "security_logs"
    __table_args__ = (
        Index("ix_security_logs_ip_event_created", "ip", "event", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"), index=True)
    ip: Mapped[str | None] = mapped_column(String(45), index=True)
    event: Mapped[str] = mapped_column(String(64), index=True)
    details: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class SecurityNonce(Base):
    """Nonce curto para challenge, anti-replay e tickets de WebSocket."""

    __tablename__ = "security_nonces"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    device_public_key: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Ban(Base):
    __tablename__ = "bans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"), index=True)
    ip: Mapped[str | None] = mapped_column(String(45), index=True)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # None = permanente
