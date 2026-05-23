-- 011: cohort library — seed_handle + is_public flag
ALTER TABLE cohorts
    ADD COLUMN IF NOT EXISTS seed_handle TEXT,
    ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT FALSE;

-- fast lookup: "is there a public cohort seeded from this handle?"
CREATE INDEX IF NOT EXISTS idx_cohorts_library
    ON cohorts (LOWER(seed_handle))
    WHERE is_public = TRUE;
