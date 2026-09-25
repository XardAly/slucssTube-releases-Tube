CREATE TABLE IF NOT EXISTS local_operations (
    id UUID PRIMARY KEY,
    device_id UUID NOT NULL REFERENCES devices(id),
    operation VARCHAR(24) NOT NULL,
    url TEXT,
    parameters TEXT NOT NULL DEFAULT '{}',
    permit_nonce_hash VARCHAR(64) NOT NULL UNIQUE,
    status VARCHAR(16) NOT NULL DEFAULT 'authorized',
    stage VARCHAR(32) NOT NULL DEFAULT 'authorized',
    progress DOUBLE PRECISION NOT NULL DEFAULT 0,
    progress_message VARCHAR(160),
    client_ip VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    CONSTRAINT ck_local_operations_progress_range
        CHECK (progress >= 0 AND progress <= 100)
);

CREATE INDEX IF NOT EXISTS ix_local_operations_device_created
    ON local_operations (device_id, created_at);
CREATE INDEX IF NOT EXISTS ix_local_operations_status_expires
    ON local_operations (status, expires_at);
