-- VibeChecx canonical schema
-- Fresh DB setup:  psql -h localhost -U vibechecx -d vibechecx -f schema.sql
-- Then apply migrations in db/migrations/ in numeric order.

-- users
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- sessions
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

-- accounts (X.com profiles tracked)
CREATE TABLE IF NOT EXISTS accounts (
    id BIGSERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT,
    avatar_url TEXT,
    bio TEXT,
    followers_count INTEGER DEFAULT 0,
    following_count INTEGER DEFAULT 0,
    tweets_count INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    is_blacklisted BOOLEAN DEFAULT FALSE,
    first_seen_at TIMESTAMPTZ DEFAULT NOW(),
    last_updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- tweets
CREATE TABLE IF NOT EXISTS tweets (
    tweet_id TEXT PRIMARY KEY,
    author_account_id BIGINT NOT NULL REFERENCES accounts(id),
    created_at TIMESTAMPTZ NOT NULL,
    content TEXT NOT NULL,
    lang TEXT,
    is_reply BOOLEAN DEFAULT FALSE,
    reply_to_tweet_id TEXT REFERENCES tweets(tweet_id),
    reply_to_account_id BIGINT REFERENCES accounts(id),
    is_quote BOOLEAN DEFAULT FALSE,
    quoted_tweet_id TEXT REFERENCES tweets(tweet_id),
    is_retweet BOOLEAN DEFAULT FALSE,
    is_sensitive BOOLEAN DEFAULT FALSE,
    likes INTEGER DEFAULT 0,
    retweets INTEGER DEFAULT 0,
    replies INTEGER DEFAULT 0,
    quotes INTEGER DEFAULT 0,
    bookmarks INTEGER DEFAULT 0,
    views INTEGER DEFAULT 0,
    sentiment REAL,
    content_type TEXT,
    tags TEXT[],
    category TEXT,
    brand_relevance JSONB,
    quality_score REAL,
    inorganic_score REAL,
    first_seen_at TIMESTAMPTZ DEFAULT NOW(),
    last_measured_at TIMESTAMPTZ DEFAULT NOW(),
    scrape_source TEXT DEFAULT 'graphql',
    validation_status TEXT DEFAULT 'unverified'
);
CREATE INDEX IF NOT EXISTS idx_tweets_author ON tweets(author_account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tweets_reply_to ON tweets(reply_to_tweet_id);
CREATE INDEX IF NOT EXISTS idx_tweets_created ON tweets(created_at DESC);

-- tweet_observations (metric refreshes)
CREATE TABLE IF NOT EXISTS tweet_observations (
    tweet_id TEXT REFERENCES tweets(tweet_id),
    observer_account_id BIGINT REFERENCES accounts(id),
    observed_at TIMESTAMPTZ DEFAULT NOW(),
    context TEXT,
    PRIMARY KEY (tweet_id, observer_account_id, observed_at)
);

-- media attached to tweets
CREATE TABLE IF NOT EXISTS media (
    id BIGSERIAL PRIMARY KEY,
    tweet_id TEXT REFERENCES tweets(tweet_id),
    media_type TEXT,
    url TEXT NOT NULL,
    local_path TEXT,
    alt_text TEXT,
    width INTEGER,
    height INTEGER,
    duration_seconds REAL,
    view_count INTEGER,
    ai_description TEXT,
    ai_labels JSONB,
    analysis_source TEXT DEFAULT 'deepseek',
    downloaded_at TIMESTAMPTZ,
    analyzed_at TIMESTAMPTZ
);

-- replies
CREATE TABLE IF NOT EXISTS replies (
    id BIGSERIAL PRIMARY KEY,
    tweet_id TEXT REFERENCES tweets(tweet_id) ON DELETE CASCADE,
    reply_id TEXT NOT NULL UNIQUE,
    author_account_id BIGINT NOT NULL REFERENCES accounts(id),
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    likes INTEGER DEFAULT 0,
    sentiment REAL,
    is_author_reply BOOLEAN DEFAULT FALSE,
    reply_depth INTEGER DEFAULT 0,
    parent_reply_id TEXT,
    relevance_to_post REAL,
    quality_flag TEXT,
    scraped_at TIMESTAMPTZ DEFAULT NOW()
);

-- cohorts (groups of accounts a user wants to track together)
CREATE TABLE IF NOT EXISTS cohorts (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    pfp_url TEXT DEFAULT '',
    brand_keywords JSONB DEFAULT '[]',
    share_token TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cohort_members (
    cohort_id BIGINT REFERENCES cohorts(id) ON DELETE CASCADE,
    account_id BIGINT REFERENCES accounts(id),
    added_at TIMESTAMPTZ DEFAULT NOW(),
    role TEXT,
    note TEXT DEFAULT '',
    PRIMARY KEY (cohort_id, account_id)
);

-- cohort_interactions (cross-account engagement)
CREATE TABLE IF NOT EXISTS cohort_interactions (
    id BIGSERIAL PRIMARY KEY,
    cohort_id BIGINT REFERENCES cohorts(id) ON DELETE CASCADE,
    from_account_id BIGINT REFERENCES accounts(id),
    to_account_id BIGINT REFERENCES accounts(id),
    tweet_id TEXT REFERENCES tweets(tweet_id),
    interaction_type TEXT,
    observed_at TIMESTAMPTZ DEFAULT NOW(),
    is_public_support BOOLEAN DEFAULT FALSE
);

-- profiles (a user's saved dashboard view: a single handle or a cohort)
CREATE TABLE IF NOT EXISTS profiles (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'single',
    cohort_id INTEGER REFERENCES cohorts(id) ON DELETE SET NULL,
    target_handle TEXT DEFAULT '',
    pfp_url TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- discovery_sessions (in-flight cohort discovery wizard state)
CREATE TABLE IF NOT EXISTS discovery_sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    project_handle TEXT,
    members_json TEXT,
    selected TEXT[],
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- scrape_sessions (one row per scrape run; UI polls this for live status)
CREATE TABLE IF NOT EXISTS scrape_sessions (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    target_account_id BIGINT REFERENCES accounts(id),
    target_handle TEXT,
    cohort_id BIGINT REFERENCES cohorts(id) ON DELETE SET NULL,
    session_type TEXT,
    phase TEXT,
    status TEXT DEFAULT 'running',
    progress_current INTEGER DEFAULT 0,
    progress_total INTEGER DEFAULT 0,
    accounts_done INTEGER DEFAULT 0,
    tweets_collected INTEGER DEFAULT 0,
    replies_collected INTEGER DEFAULT 0,
    pid INTEGER,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    last_heartbeat_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    error_log TEXT
);
CREATE INDEX IF NOT EXISTS idx_scrape_sessions_user_started ON scrape_sessions(user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_scrape_sessions_status_started ON scrape_sessions(status, started_at DESC);

-- insights_cache (cached AI-generated insights per cohort/account/period)
CREATE TABLE IF NOT EXISTS insights_cache (
    id BIGSERIAL PRIMARY KEY,
    scope_type TEXT,
    scope_id BIGINT,
    period TEXT,
    generated_at TIMESTAMPTZ DEFAULT NOW(),
    insights JSONB,
    provider TEXT
);
CREATE INDEX IF NOT EXISTS idx_insights_scope ON insights_cache(scope_type, scope_id, period);

-- score_cache: dropped in migration 002. Leaderboard now computes via a
-- single SQL CTE at request time. See §10.2 of the plan.

-- account_snapshots: follower/following history (added in migration 005).
-- One row written after each profile scrape so we can show period growth.
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
