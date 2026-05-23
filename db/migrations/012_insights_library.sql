-- Insights library: track which user generated each cached insight and give it a display name.
ALTER TABLE insights_cache
    ADD COLUMN IF NOT EXISTS user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS display_name TEXT;

CREATE INDEX IF NOT EXISTS idx_insights_cache_user
    ON insights_cache(user_id, generated_at DESC)
    WHERE user_id IS NOT NULL;
