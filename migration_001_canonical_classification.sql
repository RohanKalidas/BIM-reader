-- migration_001_canonical_classification.sql
--
-- Adds four classification columns to the components table:
--   - canonical_name (string, single value from the canonical vocabulary)
--   - style_tags    (text[], list from STYLE_TAGS vocab)
--   - context_tags  (text[], list from CONTEXT_TAGS vocab)
--   - quality_class (string, one of premium|standard|basic)
--
-- Plus a couple of indexes for fast lookup during fixture matching.
--
-- All four columns are nullable. Components classified retroactively get
-- values via the backfill script. Components classified at ingest time
-- get values immediately. Anything still NULL falls back to legacy
-- fuzzy-name matching, so the system is forward-compatible.
--
-- This migration is REVERSIBLE — see migration_001_revert.sql below.

BEGIN;

ALTER TABLE components
    ADD COLUMN IF NOT EXISTS canonical_name TEXT,
    ADD COLUMN IF NOT EXISTS style_tags     TEXT[],
    ADD COLUMN IF NOT EXISTS context_tags   TEXT[],
    ADD COLUMN IF NOT EXISTS quality_class  TEXT;

-- Primary lookup: by canonical_name. Filtered scans on this are the
-- hottest path during generation.
CREATE INDEX IF NOT EXISTS idx_components_canonical_name
    ON components (canonical_name)
    WHERE canonical_name IS NOT NULL;

-- For tag filtering. GIN indexes handle array overlap efficiently.
CREATE INDEX IF NOT EXISTS idx_components_style_tags
    ON components USING GIN (style_tags)
    WHERE style_tags IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_components_context_tags
    ON components USING GIN (context_tags)
    WHERE context_tags IS NOT NULL;

-- Backfill state tracking — lets us resume the backfill if it's
-- interrupted. NULL = not yet classified. Updated by classifier.
ALTER TABLE components
    ADD COLUMN IF NOT EXISTS classified_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_components_classified_at
    ON components (classified_at)
    WHERE classified_at IS NULL;

COMMIT;
