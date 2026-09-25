-- Estatísticas persistentes de fila e conclusões do site.
-- A migração é aditiva, idempotente e não altera tarefas existentes.

ALTER TABLE downloads
ADD COLUMN IF NOT EXISTS statistics_counted_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS system_counters (
    key VARCHAR(64) PRIMARY KEY,
    value BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_system_counters_value_nonnegative CHECK (value >= 0)
);

WITH counted AS (
    UPDATE downloads AS download
    SET statistics_counted_at = COALESCE(download.completed_at, download.updated_at, NOW())
    FROM devices AS device
    WHERE download.device_id = device.id
      AND download.status = 'completed'
      AND download.statistics_counted_at IS NULL
      AND device.platform = 'web'
    RETURNING download.id
), delta AS (
    SELECT COUNT(*)::BIGINT AS value FROM counted
)
INSERT INTO system_counters (key, value, updated_at)
SELECT 'site_downloads_completed', value, NOW() FROM delta
ON CONFLICT (key) DO UPDATE
SET value = system_counters.value + EXCLUDED.value,
    updated_at = NOW();

UPDATE downloads
SET statistics_counted_at = COALESCE(completed_at, updated_at, NOW())
WHERE status = 'completed'
  AND statistics_counted_at IS NULL;
