-- Migration 006: enforce reply-direction integrity at the DB layer.
--
-- Context (May 2026): collector/replyminer.py had a filter bug that allowed
-- the conversation-ancestor chain returned by X's TweetDetail GraphQL response
-- to be stored as "replies to" the tweet being mined. That inverted the
-- directionality of 50/55 rows in `replies`. The application-layer filter is
-- fixed, but we add a DB trigger here so no future code path can recreate the
-- same bug silently — a reply must not be older than its parent.
--
-- Same invariant applies to the tweets table: a tweet with reply_to_tweet_id
-- set must have been created after the parent.
--
-- The trigger ALLOWS the case where the parent isn't (yet) in our DB —
-- happens when a child is scraped before its parent. We only reject when we
-- can prove the inversion (we know both timestamps and they're inconsistent).

CREATE OR REPLACE FUNCTION validate_reply_direction() RETURNS TRIGGER AS $$
DECLARE
    parent_ts TIMESTAMPTZ;
BEGIN
    SELECT created_at INTO parent_ts
    FROM tweets WHERE tweet_id = NEW.tweet_id;
    IF parent_ts IS NOT NULL AND NEW.created_at < parent_ts THEN
        RAISE EXCEPTION
            'reply % to parent % rejected: reply created_at % predates parent %',
            NEW.reply_id, NEW.tweet_id, NEW.created_at, parent_ts;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS replies_validate_direction ON replies;
CREATE TRIGGER replies_validate_direction
BEFORE INSERT OR UPDATE OF created_at, tweet_id ON replies
FOR EACH ROW EXECUTE FUNCTION validate_reply_direction();


CREATE OR REPLACE FUNCTION validate_tweet_reply_direction() RETURNS TRIGGER AS $$
DECLARE
    parent_ts TIMESTAMPTZ;
BEGIN
    IF NEW.reply_to_tweet_id IS NOT NULL THEN
        SELECT created_at INTO parent_ts
        FROM tweets WHERE tweet_id = NEW.reply_to_tweet_id;
        IF parent_ts IS NOT NULL AND NEW.created_at < parent_ts THEN
            RAISE EXCEPTION
                'tweet % rejected: created_at % predates its parent % (%)',
                NEW.tweet_id, NEW.created_at, NEW.reply_to_tweet_id, parent_ts;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tweets_validate_reply_direction ON tweets;
CREATE TRIGGER tweets_validate_reply_direction
BEFORE INSERT OR UPDATE OF created_at, reply_to_tweet_id ON tweets
FOR EACH ROW EXECUTE FUNCTION validate_tweet_reply_direction();
