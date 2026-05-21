-- Migration 003: add tweets_count column on accounts.
-- The collector writes it from GraphQL `user_legacy.statuses_count` but the
-- live DB was missing the column, so the UPDATE silently failed and
-- followers/following/tweets all stayed at 0 for every cohort member.

ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS tweets_count INTEGER DEFAULT 0;
