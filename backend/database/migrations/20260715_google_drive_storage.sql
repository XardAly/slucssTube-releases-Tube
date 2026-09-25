-- Migração aditiva e retrocompatível do armazenamento privado.
-- Execute os CREATE INDEX CONCURRENTLY com autocommit habilitado.

ALTER TABLE downloads ADD COLUMN IF NOT EXISTS storage_backend VARCHAR(24) NOT NULL DEFAULT 'local';
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS storage_state VARCHAR(32) NOT NULL DEFAULT 'local_ready';
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS storage_key TEXT;
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS storage_reserved_key TEXT;
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS storage_idempotency_key VARCHAR(200);
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS remote_parent_id TEXT;
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS remote_size BIGINT;
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS remote_checksum VARCHAR(128);
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS remote_mime_type VARCHAR(255);
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS remote_uploaded_at TIMESTAMPTZ;
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS local_delete_pending BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS file_deleted_at TIMESTAMPTZ;
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS storage_error TEXT;
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS source_storage_backend VARCHAR(24);
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS source_storage_key TEXT;
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS source_storage_reserved_key TEXT;
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS source_storage_idempotency_key VARCHAR(200);
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS source_remote_parent_id TEXT;
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS source_remote_size BIGINT;
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS source_remote_checksum VARCHAR(128);
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS source_remote_mime_type VARCHAR(255);
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS source_remote_uploaded_at TIMESTAMPTZ;

-- Registros antigos continuam sendo entregues pelo caminho local atual.
UPDATE downloads
SET storage_backend = 'local',
    storage_key = file_path,
    storage_state = CASE
        WHEN status = 'completed' AND file_path IS NOT NULL THEN 'completed'
        ELSE 'local_ready'
    END
WHERE storage_key IS NULL AND file_path IS NOT NULL;

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_downloads_storage_idempotency
ON downloads (storage_idempotency_key)
WHERE storage_idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_downloads_source_storage_idempotency
ON downloads (source_storage_idempotency_key)
WHERE source_storage_idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_downloads_storage_reserved_key
ON downloads (storage_reserved_key)
WHERE storage_reserved_key IS NOT NULL;

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_downloads_source_storage_reserved_key
ON downloads (source_storage_reserved_key)
WHERE source_storage_reserved_key IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_downloads_storage_pending
ON downloads (storage_state, updated_at);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_downloads_storage_expires
ON downloads (expires_at, storage_state);

ALTER TABLE downloads DROP CONSTRAINT IF EXISTS ck_downloads_remote_size_nonnegative;
ALTER TABLE downloads ADD CONSTRAINT ck_downloads_remote_size_nonnegative
CHECK (remote_size IS NULL OR remote_size >= 0) NOT VALID;
ALTER TABLE downloads VALIDATE CONSTRAINT ck_downloads_remote_size_nonnegative;

CREATE TABLE IF NOT EXISTS storage_cursors (
    key VARCHAR(64) PRIMARY KEY,
    cursor TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
