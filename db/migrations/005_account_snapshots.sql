-- Migration 005: account follower/following growth snapshots.
--
-- accounts.followers_count is a single point-in-time value, overwritten on
-- every scrape. This table records a timestamped snapshot each time we scrape
-- a profile so we can compute "gained N followers this month."
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS account_snapshots (
    id            BIGSERIAL PRIMARY KEY,
    account_id    BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    followers     INTEGER NOT NULL DEFAULT 0,
    following     INTEGER NOT NULL DEFAULT 0,
    tweets_count  INTEGER NOT NULL DEFAULT 0,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_account_snapshots_account
    ON account_snapshots (account_id, recorded_at DESC);
