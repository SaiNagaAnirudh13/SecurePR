-- Authoritative CI-gated review lifecycle and GitHub auto-merge reconciliation.
-- Safe to re-run; existing review, CI, and PR history is preserved.

ALTER TABLE reviews
    ADD COLUMN IF NOT EXISTS lifecycle_state VARCHAR(32) NOT NULL DEFAULT 'RECEIVED';
ALTER TABLE reviews
    ADD COLUMN IF NOT EXISTS ci_verified_at TIMESTAMP WITHOUT TIME ZONE;

ALTER TABLE ci_revisions
    ADD COLUMN IF NOT EXISTS required_checks JSON NOT NULL DEFAULT '[]'::json;
ALTER TABLE ci_revisions
    ADD COLUMN IF NOT EXISTS discovery_complete BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE ci_revisions
    ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMP WITHOUT TIME ZONE;

ALTER TABLE ci_executions
    ADD COLUMN IF NOT EXISTS app_id VARCHAR(64);

ALTER TABLE pull_requests
    ADD COLUMN IF NOT EXISTS merged_at TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE pull_requests
    ADD COLUMN IF NOT EXISTS merge_commit_sha VARCHAR(64);

ALTER TABLE review_publications
    ALTER COLUMN message_type TYPE VARCHAR(64);
