-- Migration 007: per-cohort curated ecosystem-handle allowlist.
--
-- Context: the hallucination validator on insights flagged real ecosystem
-- entities (@solana, @toly, @solflare) as hallucinations because they weren't
-- in our DB. Migration 006 broadened the allowlist by mining @-mentions from
-- tweet content. This migration adds a small user-curated escape hatch for
-- the remaining cases: a JSON array of handles the user explicitly marks as
-- legitimate ecosystem references for THIS cohort.
--
-- Handles here render as external x.com links (the system knows they're real
-- but not in our data); they no longer trigger the warning chip.

ALTER TABLE cohorts
    ADD COLUMN IF NOT EXISTS ecosystem_handles JSONB DEFAULT '[]'::jsonb;

COMMENT ON COLUMN cohorts.ecosystem_handles IS
'JSON array of handles (lowercase, no leading @) that the cohort owner has '
'whitelisted as legitimate ecosystem references. Example: ["solana", "toly"]. '
'These render as external x.com deeplinks in insights output instead of '
'tripping the hallucination warning.';
