CREATE TABLE IF NOT EXISTS media_cache (
    id UUID PRIMARY KEY,
    cache_key VARCHAR(64) NOT NULL UNIQUE,
    platform VARCHAR(32) NOT NULL,
    media_id VARCHAR(255) NOT NULL,
    operation_type VARCHAR(32) NOT NULL,
    request_fingerprint VARCHAR(64) NOT NULL,
    request_parameters TEXT NOT NULL,
    state VARCHAR(16) NOT NULL DEFAULT 'processing',
    owner_download_id UUID,
    progress DOUBLE PRECISION NOT NULL DEFAULT 0,
    stage VARCHAR(24),
    title TEXT,
    mode VARCHAR(4) NOT NULL,
    output_format VARCHAR(8),
    conversion_options TEXT,
    storage_backend VARCHAR(24),
    storage_key TEXT,
    storage_reserved_key TEXT,
    remote_parent_id TEXT,
    remote_checksum VARCHAR(128),
    remote_mime_type VARCHAR(255),
    remote_uploaded_at TIMESTAMPTZ,
    file_size BIGINT,
    request_count BIGINT NOT NULL DEFAULT 0,
    hit_count BIGINT NOT NULL DEFAULT 0,
    external_downloads BIGINT NOT NULL DEFAULT 0,
    bytes_reused BIGINT NOT NULL DEFAULT 0,
    bytes_downloaded_external BIGINT NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_error VARCHAR(120),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processing_started_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    ready_at TIMESTAMPTZ,
    last_accessed_at TIMESTAMPTZ,
    validated_at TIMESTAMPTZ,
    CONSTRAINT ck_media_cache_request_count CHECK (request_count >= 0),
    CONSTRAINT ck_media_cache_hit_count CHECK (hit_count >= 0),
    CONSTRAINT ck_media_cache_external_downloads CHECK (external_downloads >= 0),
    CONSTRAINT ck_media_cache_bytes_reused CHECK (bytes_reused >= 0),
    CONSTRAINT ck_media_cache_bytes_external CHECK (bytes_downloaded_external >= 0),
    CONSTRAINT ck_media_cache_file_size CHECK (file_size IS NULL OR file_size >= 0)
);

ALTER TABLE media_cache
    ADD COLUMN IF NOT EXISTS bytes_downloaded_external BIGINT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS ix_media_cache_state_updated
    ON media_cache (state, updated_at);
CREATE INDEX IF NOT EXISTS ix_media_cache_owner
    ON media_cache (owner_download_id);
CREATE INDEX IF NOT EXISTS ix_media_cache_media
    ON media_cache (platform, media_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_media_cache_storage_key
    ON media_cache (storage_key) WHERE storage_key IS NOT NULL;

ALTER TABLE downloads
    ADD COLUMN IF NOT EXISTS media_cache_id UUID REFERENCES media_cache(id) ON DELETE SET NULL;
ALTER TABLE downloads
    ADD COLUMN IF NOT EXISTS cache_role VARCHAR(16);
CREATE INDEX IF NOT EXISTS ix_downloads_media_cache
    ON downloads (media_cache_id, cache_role);
