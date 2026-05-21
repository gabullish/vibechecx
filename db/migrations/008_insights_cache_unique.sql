-- Migration 008: ensure insights_cache has at most ONE row per
-- (scope_type, scope_id, period).
--
-- Context: before this, every regen was INSERTing a new row, so rows
-- accumulated indefinitely. The read path picked the newest with ORDER BY
-- LIMIT 1 so behavior was correct but the table grew. Combined with the
-- application-layer change to DELETE-then-INSERT in cached_insights (Follow-up
-- D refinement, 2026-05-18), this unique index is the structural guarantee.
--
-- Run BEFORE adding the index against a fresh database: no de-dup needed.
-- Against an existing DB that may have accumulated duplicates, de-dup first:
--   WITH ranked AS (
--     SELECT id, ROW_NUMBER() OVER (
--       PARTITION BY scope_type, scope_id, period
--       ORDER BY generated_at DESC
--     ) AS rn FROM insights_cache
--   )
--   DELETE FROM insights_cache WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

CREATE UNIQUE INDEX IF NOT EXISTS idx_insights_cache_unique_scope
    ON insights_cache (scope_type, scope_id, period);
