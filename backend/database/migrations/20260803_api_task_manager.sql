-- Fila persistente e segurança transitória centralizadas no PostgreSQL.
-- Execute antes de iniciar a versão nova da API. Todas as operações são
-- aditivas/idempotentes e preservam o histórico existente.

ALTER TYPE downloadstatus ADD VALUE IF NOT EXISTS 'cancelled';

ALTER TABLE downloads ADD COLUMN IF NOT EXISTS operation_type VARCHAR(32) NOT NULL DEFAULT 'download';
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS deduplication_key VARCHAR(64);
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS progress_message VARCHAR(255);
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ;
ALTER TABLE downloads ADD COLUMN IF NOT EXISTS interrupted_at TIMESTAMPTZ;

UPDATE downloads
SET operation_type = CASE
    WHEN mode = 'gif' AND source_path IS NOT NULL THEN 'gif_upload'
    WHEN mode = 'gif' THEN 'gif_url'
    ELSE 'download'
END
WHERE operation_type = 'download';

UPDATE downloads
SET status = 'cancelled', stage = 'cancelled', error = NULL
WHERE status = 'failed' AND error = '__cancelled__';

CREATE INDEX IF NOT EXISTS ix_downloads_queued_created ON downloads (queued_at)
WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS ix_downloads_operation_status ON downloads (operation_type, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_downloads_active_deduplication
ON downloads (device_id, deduplication_key)
WHERE deduplication_key IS NOT NULL AND status IN ('queued', 'processing');

CREATE TABLE IF NOT EXISTS security_nonces (
    id VARCHAR(128) PRIMARY KEY,
    kind VARCHAR(16) NOT NULL,
    device_public_key TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_security_nonces_kind ON security_nonces (kind);
CREATE INDEX IF NOT EXISTS ix_security_nonces_expires_at ON security_nonces (expires_at);
