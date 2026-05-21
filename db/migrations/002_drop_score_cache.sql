-- Migration 002: drop score_cache table
-- §10.2 of the plan rewrites the leaderboard as a single SQL CTE; the
-- precomputed cache is no longer needed and was causing the "Loading…"
-- stripe because compute_scores() blocked the request handler on cache miss.

DROP TABLE IF EXISTS score_cache;
