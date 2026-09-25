-- Execute com autocommit habilitado. CREATE INDEX CONCURRENTLY não pode
-- rodar dentro de uma transação explícita.

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_downloads_device_created
ON downloads (device_id, created_at DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_downloads_processing_heartbeat
ON downloads (heartbeat_at)
WHERE status = 'processing';

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_downloads_queued_created
ON downloads (created_at)
WHERE status = 'queued';

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_downloads_expiration_pending
ON downloads (completed_at)
WHERE file_path IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_downloads_completed_recent
ON downloads (completed_at)
WHERE status = 'completed';

-- Consultas executadas em toda requisição autenticada e nos limites antifraude.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_api_requests_device_timestamp
ON api_requests (device_id, timestamp);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_api_requests_ip_timestamp
ON api_requests (ip, timestamp);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_api_requests_device_endpoint_timestamp
ON api_requests (device_id, endpoint, timestamp);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_security_logs_ip_event_created
ON security_logs (ip, event, created_at);
