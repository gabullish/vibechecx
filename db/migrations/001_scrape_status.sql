-- Migration 001: DB-backed scrape status (replaces /tmp/vibechecx_scrape_status.json)
-- Adds per-user attribution and live progress fields to scrape_sessions so the UI
-- can poll a real DB row instead of a single global file.

ALTER TABLE scrape_sessions
    ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS target_handle TEXT,
    ADD COLUMN IF NOT EXISTS cohort_id BIGINT REFERENCES cohorts(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS phase TEXT,
    ADD COLUMN IF NOT EXISTS progress_current INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS progress_total INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS accounts_done INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS pid INTEGER,
    ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_scrape_sessions_user_started
    ON scrape_sessions(user_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_scrape_sessions_status_started
    ON scrape_sessions(status, started_at DESC);
