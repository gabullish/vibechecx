-- Metric refresh worker — keep tweet engagement counts fresh between scrapes.
-- A background thread polls X's TweetResultByRestId GraphQL endpoint for
-- recent tweets and overwrites the metric columns in place.
ALTER TABLE tweets ADD COLUMN IF NOT EXISTS metrics_refreshed_at TIMESTAMPTZ;

-- Plain index — NOW() can't appear in a partial-index WHERE clause, so we
-- filter "<= 30 days" at query time. The index makes the NULLS-FIRST scan
-- fast enough that filtering by created_at is cheap.
CREATE INDEX IF NOT EXISTS idx_tweets_metrics_refresh
    ON tweets(metrics_refreshed_at NULLS FIRST);
