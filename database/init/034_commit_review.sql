-- 034_commit_review.sql
-- Persist the explicit comparison mode and the pinned base revision for source reviews.

ALTER TABLE public.scans
    ADD COLUMN IF NOT EXISTS comparison_mode text NOT NULL DEFAULT 'full_repository',
    ADD COLUMN IF NOT EXISTS base_commit_sha text;
