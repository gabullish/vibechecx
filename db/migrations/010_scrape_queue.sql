-- 010: scrape queue for multi-user serialization + admin flag on users

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE;

CREATE SEQUENCE IF NOT EXISTS scrape_queue_position_seq;

CREATE TABLE IF NOT EXISTS scrape_queue (
    id          BIGSERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    profile_id  INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    days        INTEGER NOT NULL DEFAULT 30,
    status      TEXT NOT NULL DEFAULT 'pending',
    session_id  BIGINT REFERENCES scrape_sessions(id),
    position    INTEGER NOT NULL DEFAULT nextval('scrape_queue_position_seq'),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    started_at  TIMESTAMPTZ,
    ended_at    TIMESTAMPTZ,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_scrape_queue_status_created
    ON scrape_queue(status, created_at);
CREATE INDEX IF NOT EXISTS idx_scrape_queue_user
    ON scrape_queue(user_id, created_at DESC);
