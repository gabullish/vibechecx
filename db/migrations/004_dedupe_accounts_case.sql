-- Migration 004: case-insensitive account dedupe + permanent uniqueness guard.
--
-- The recent lowercasing in scope.py created shadow rows (e.g. `SolflareEmpire`
-- + `solflareempire`), splitting each handle's tweets across two account_ids.
-- This migration:
--   1. For each (lower(username)) group with >1 row, picks the row with the
--      most associated tweets as the canonical id.
--   2. Repoints every FK column to the canonical id.
--   3. Backfills missing display_name/avatar_url/bio/followers/following/
--      tweets_count from the duplicate(s) into the canonical row.
--   4. Drops the duplicate rows.
--   5. Lowercases the canonical row's username.
--   6. Adds a unique index on LOWER(username) so future inserts can't split.
--
-- Idempotent: re-running is a no-op once the unique index exists.

BEGIN;

-- Canonical mapping: one row per lower(username), pointing to the id
-- that owns the most tweets (or smallest id as tiebreaker).
CREATE TEMP TABLE _canon ON COMMIT DROP AS
SELECT DISTINCT ON (LOWER(a.username))
       LOWER(a.username) AS lname,
       a.id              AS canonical_id
FROM accounts a
LEFT JOIN tweets t ON t.author_account_id = a.id
GROUP BY a.id, a.username
ORDER BY LOWER(a.username), COUNT(t.tweet_id) DESC, a.id ASC;

-- Map every account_id → canonical_id (1:1 for groups of size 1).
CREATE TEMP TABLE _map ON COMMIT DROP AS
SELECT a.id AS old_id, c.canonical_id AS new_id
FROM accounts a
JOIN _canon c ON c.lname = LOWER(a.username);

-- 2. Repoint FKs.
UPDATE tweets t SET author_account_id  = m.new_id FROM _map m WHERE t.author_account_id  = m.old_id AND m.new_id <> m.old_id;
UPDATE tweets t SET reply_to_account_id= m.new_id FROM _map m WHERE t.reply_to_account_id= m.old_id AND m.new_id <> m.old_id;
UPDATE replies r SET author_account_id = m.new_id FROM _map m WHERE r.author_account_id  = m.old_id AND m.new_id <> m.old_id;
UPDATE cohort_members SET account_id   = m.new_id FROM _map m WHERE account_id           = m.old_id AND m.new_id <> m.old_id;
UPDATE cohort_interactions SET from_account_id = m.new_id FROM _map m WHERE from_account_id = m.old_id AND m.new_id <> m.old_id;
UPDATE cohort_interactions SET to_account_id   = m.new_id FROM _map m WHERE to_account_id   = m.old_id AND m.new_id <> m.old_id;
UPDATE scrape_sessions SET target_account_id   = m.new_id FROM _map m WHERE target_account_id = m.old_id AND m.new_id <> m.old_id;
-- tweet_observations has a composite PK (tweet_id, observer_account_id, observed_at).
-- A direct UPDATE could violate uniqueness if both old and new rows already exist.
-- Strategy: insert (tweet_id, new_id, observed_at, context) for any old observation
-- that doesn't yet exist under new_id, then drop the old rows.
INSERT INTO tweet_observations (tweet_id, observer_account_id, observed_at, context)
SELECT obs.tweet_id, m.new_id, obs.observed_at, obs.context
FROM tweet_observations obs
JOIN _map m ON m.old_id = obs.observer_account_id
WHERE m.new_id <> m.old_id
ON CONFLICT DO NOTHING;
DELETE FROM tweet_observations obs
USING _map m
WHERE obs.observer_account_id = m.old_id AND m.new_id <> m.old_id;

-- 3. Backfill canonical with any populated fields from the duplicate(s).
UPDATE accounts canon SET
  display_name    = COALESCE(NULLIF(canon.display_name,    ''), dup.display_name),
  avatar_url      = COALESCE(NULLIF(canon.avatar_url,      ''), dup.avatar_url),
  bio             = COALESCE(NULLIF(canon.bio,             ''), dup.bio),
  followers_count = GREATEST(COALESCE(canon.followers_count,0), COALESCE(dup.followers_count,0)),
  following_count = GREATEST(COALESCE(canon.following_count,0), COALESCE(dup.following_count,0)),
  tweets_count    = GREATEST(COALESCE(canon.tweets_count,   0), COALESCE(dup.tweets_count,   0))
FROM accounts dup
JOIN _map m ON m.old_id = dup.id AND m.new_id <> m.old_id
WHERE canon.id = m.new_id;

-- 4. Delete dup rows.
DELETE FROM accounts a USING _map m WHERE a.id = m.old_id AND m.new_id <> m.old_id;

-- 5. Lowercase canonical usernames.
UPDATE accounts SET username = LOWER(username) WHERE username <> LOWER(username);

-- 6. Permanent guard.
DROP INDEX IF EXISTS accounts_username_lower_unique;
CREATE UNIQUE INDEX accounts_username_lower_unique ON accounts (LOWER(username));

COMMIT;
