-- SecurePR PostgreSQL initial schema, generated from app/models/models.py.

-- Idempotent bootstrap: creates missing tables and indexes; does not drop data.

CREATE TABLE IF NOT EXISTS audit_log (
	id VARCHAR(36) NOT NULL, 
	actor VARCHAR(255), 
	action VARCHAR(255), 
	target_type VARCHAR(64), 
	target_id VARCHAR(36), 
	details_json JSON, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS repositories (
	id VARCHAR(36) NOT NULL, 
	github_repo_id VARCHAR(64) NOT NULL, 
	owner_login VARCHAR(255) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	full_name VARCHAR(512) NOT NULL, 
	default_branch VARCHAR(255) DEFAULT 'main' NOT NULL, 
	installation_id VARCHAR(64), 
	is_active BOOLEAN DEFAULT true NOT NULL, 
	is_auto_merge_enabled BOOLEAN DEFAULT false NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (github_repo_id), 
	UNIQUE (full_name)
);

CREATE TABLE IF NOT EXISTS users (
	id VARCHAR(36) NOT NULL, 
	github_login VARCHAR(255) NOT NULL, 
	github_user_id VARCHAR(64), 
	display_name VARCHAR(255), 
	email VARCHAR(255), 
	is_repo_owner BOOLEAN DEFAULT false NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (github_login), 
	UNIQUE (github_user_id)
);

CREATE TABLE IF NOT EXISTS auto_approval_eligibility (
	id VARCHAR(36) NOT NULL, 
	repository_id VARCHAR(36) NOT NULL, 
	user_id VARCHAR(36) NOT NULL, 
	is_enabled BOOLEAN DEFAULT false NOT NULL, 
	max_risk_threshold INTEGER DEFAULT 39 NOT NULL, 
	updated_by VARCHAR(255), 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_repo_user_eligibility UNIQUE (repository_id, user_id), 
	FOREIGN KEY(repository_id) REFERENCES repositories (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS circuit_breakers (
	id VARCHAR(36) NOT NULL, 
	scope VARCHAR(10) NOT NULL, 
	repository_id VARCHAR(36), 
	user_login VARCHAR(255), 
	is_tripped BOOLEAN DEFAULT false NOT NULL, 
	reason VARCHAR(1024), 
	tripped_by VARCHAR(255), 
	tripped_at TIMESTAMP WITHOUT TIME ZONE, 
	auto_resume_at TIMESTAMP WITHOUT TIME ZONE, 
	resumed_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_circuit_breakers_scope CHECK (scope IN ('GLOBAL', 'REPOSITORY', 'USER')), 
	FOREIGN KEY(repository_id) REFERENCES repositories (id)
);

CREATE TABLE IF NOT EXISTS policies (
	id VARCHAR(36) NOT NULL, 
	repository_id VARCHAR(36), 
	name VARCHAR(255) NOT NULL, 
	version INTEGER DEFAULT 1 NOT NULL, 
	content_json JSON NOT NULL, 
	is_active BOOLEAN DEFAULT true NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(repository_id) REFERENCES repositories (id)
);

CREATE TABLE IF NOT EXISTS pull_requests (
	id VARCHAR(36) NOT NULL, 
	repository_id VARCHAR(36) NOT NULL, 
	github_pr_number INTEGER NOT NULL, 
	title VARCHAR(1024), 
	author_login VARCHAR(255), 
	head_sha VARCHAR(64), 
	base_branch VARCHAR(255), 
	head_branch VARCHAR(255), 
	state VARCHAR(32) DEFAULT 'open' NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_repo_pr_number UNIQUE (repository_id, github_pr_number), 
	FOREIGN KEY(repository_id) REFERENCES repositories (id)
);

CREATE TABLE IF NOT EXISTS circuit_breaker_events (
	id VARCHAR(36) NOT NULL, 
	circuit_breaker_id VARCHAR(36) NOT NULL, 
	action VARCHAR(32), 
	actor VARCHAR(255), 
	reason VARCHAR(1024), 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(circuit_breaker_id) REFERENCES circuit_breakers (id)
);

CREATE TABLE IF NOT EXISTS commits (
	id VARCHAR(36) NOT NULL, 
	pull_request_id VARCHAR(36) NOT NULL, 
	sha VARCHAR(64) NOT NULL, 
	message TEXT, 
	author_login VARCHAR(255), 
	committed_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(pull_request_id) REFERENCES pull_requests (id)
);

CREATE TABLE IF NOT EXISTS reviews (
	id VARCHAR(36) NOT NULL, 
	pull_request_id VARCHAR(36) NOT NULL, 
	policy_id VARCHAR(36), 
	head_sha VARCHAR(64) NOT NULL, 
	is_reevaluation BOOLEAN DEFAULT false NOT NULL, 
	superseded_review_id VARCHAR(36), 
	is_ci_wait_placeholder BOOLEAN DEFAULT false NOT NULL, 
	ci_wait_started_at TIMESTAMP WITHOUT TIME ZONE, 
	ci_wait_pending_checks JSON, 
	score_change_surface FLOAT DEFAULT 0 NOT NULL, 
	score_reversibility FLOAT DEFAULT 0 NOT NULL, 
	score_data_security FLOAT DEFAULT 0 NOT NULL, 
	score_operational FLOAT DEFAULT 0 NOT NULL, 
	score_verification FLOAT DEFAULT 0 NOT NULL, 
	score_policy_compliance FLOAT DEFAULT 0 NOT NULL, 
	overall_score FLOAT DEFAULT 0 NOT NULL, 
	risk_category VARCHAR(16) DEFAULT 'NOT_EVALUATED' NOT NULL, 
	decision VARCHAR(14) DEFAULT 'PENDING' NOT NULL, 
	decision_reasons JSON, 
	circuit_breaker_active BOOLEAN DEFAULT false NOT NULL, 
	github_comment_url VARCHAR(1024), 
	github_review_state VARCHAR(32), 
	github_action_confirmed BOOLEAN DEFAULT false NOT NULL, 
	auto_merge_status VARCHAR(48), 
	auto_merge_attempted_at TIMESTAMP WITHOUT TIME ZONE, 
	auto_merged_at TIMESTAMP WITHOUT TIME ZONE, 
	merge_commit_sha VARCHAR(64), 
	auto_merge_failure_reason TEXT, 
	analysis_started_at TIMESTAMP WITHOUT TIME ZONE, 
	analysis_completed_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_review_pr_sha_phase UNIQUE (pull_request_id, head_sha, is_ci_wait_placeholder, is_reevaluation), 
	CONSTRAINT ck_reviews_category CHECK (risk_category IN ('NOT_EVALUATED', 'LOW', 'MEDIUM', 'HIGH')), 
	FOREIGN KEY(pull_request_id) REFERENCES pull_requests (id), 
	FOREIGN KEY(policy_id) REFERENCES policies (id), 
	FOREIGN KEY(superseded_review_id) REFERENCES reviews (id), 
	CONSTRAINT ck_reviews_decision CHECK (decision IN ('APPROVED', 'BLOCKED', 'MANUAL_REVIEW', 'WAITING_FOR_CI', 'PENDING'))
);

CREATE TABLE IF NOT EXISTS changed_files (
	id VARCHAR(36) NOT NULL, 
	review_id VARCHAR(36) NOT NULL, 
	file_path VARCHAR(1024) NOT NULL, 
	change_type VARCHAR(16), 
	additions INTEGER DEFAULT 0 NOT NULL, 
	deletions INTEGER DEFAULT 0 NOT NULL, 
	is_blocked_path BOOLEAN DEFAULT false NOT NULL, 
	patch_excerpt TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(review_id) REFERENCES reviews (id)
);

CREATE TABLE IF NOT EXISTS evidence (
	id VARCHAR(36) NOT NULL, 
	review_id VARCHAR(36) NOT NULL, 
	evidence_type VARCHAR(64), 
	source VARCHAR(255), 
	content TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(review_id) REFERENCES reviews (id)
);

CREATE TABLE IF NOT EXISTS findings (
	id VARCHAR(36) NOT NULL, 
	review_id VARCHAR(36) NOT NULL, 
	dimension VARCHAR(64), 
	severity VARCHAR(16), 
	title VARCHAR(512), 
	description TEXT, 
	file_path VARCHAR(1024), 
	line_reference VARCHAR(64), 
	is_mandatory_blocker BOOLEAN DEFAULT false NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_findings_severity CHECK (severity IS NULL OR severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')), 
	FOREIGN KEY(review_id) REFERENCES reviews (id)
);

CREATE TABLE IF NOT EXISTS github_actions (
	id VARCHAR(36) NOT NULL, 
	review_id VARCHAR(36), 
	action_type VARCHAR(64), 
	request_payload JSON, 
	response_status INTEGER, 
	response_payload JSON, 
	succeeded BOOLEAN DEFAULT false NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(review_id) REFERENCES reviews (id)
);

CREATE TABLE IF NOT EXISTS impact_predictions (
	id VARCHAR(36) NOT NULL, 
	review_id VARCHAR(36) NOT NULL, 
	component_type VARCHAR(64), 
	component_name VARCHAR(512), 
	confidence VARCHAR(10) DEFAULT 'INFERRED' NOT NULL, 
	evidence_summary TEXT, 
	potential_consequence TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(review_id) REFERENCES reviews (id), 
	CONSTRAINT ck_impact_predictions_confidence CHECK (confidence IN ('CONFIRMED', 'INFERRED', 'UNRESOLVED'))
);

CREATE TABLE IF NOT EXISTS notifications (
	id VARCHAR(36) NOT NULL, 
	review_id VARCHAR(36), 
	recipient VARCHAR(255), 
	channel VARCHAR(32), 
	subject VARCHAR(512), 
	body TEXT, 
	delivered BOOLEAN DEFAULT false NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(review_id) REFERENCES reviews (id)
);

CREATE INDEX IF NOT EXISTS ix_pull_requests_repository_id ON pull_requests (repository_id);

CREATE INDEX IF NOT EXISTS ix_commits_pr ON commits (pull_request_id);

CREATE INDEX IF NOT EXISTS ix_reviews_decision ON reviews (decision);

CREATE INDEX IF NOT EXISTS ix_reviews_pr ON reviews (pull_request_id);

CREATE INDEX IF NOT EXISTS ix_changed_files_review ON changed_files (review_id);

CREATE INDEX IF NOT EXISTS ix_evidence_review ON evidence (review_id);

CREATE INDEX IF NOT EXISTS ix_findings_review ON findings (review_id);

CREATE INDEX IF NOT EXISTS ix_github_actions_review ON github_actions (review_id);

CREATE INDEX IF NOT EXISTS ix_impact_review ON impact_predictions (review_id);

CREATE TABLE IF NOT EXISTS background_jobs (
	id VARCHAR(36) NOT NULL,
	job_type VARCHAR(32) NOT NULL,
	idempotency_key VARCHAR(512) NOT NULL,
	payload JSON NOT NULL,
	state VARCHAR(16) DEFAULT 'QUEUED' NOT NULL,
	attempts INTEGER DEFAULT 0 NOT NULL,
	available_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	lease_until TIMESTAMP WITHOUT TIME ZONE,
	last_error TEXT,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (idempotency_key)
);

CREATE TABLE IF NOT EXISTS ci_revisions (
	id VARCHAR(36) NOT NULL,
	repository_id VARCHAR(36) NOT NULL,
	pull_request_id VARCHAR(36) NOT NULL,
	head_sha VARCHAR(64) NOT NULL,
	state VARCHAR(24) DEFAULT 'UNKNOWN' NOT NULL,
	error TEXT,
	retry_count INTEGER DEFAULT 0 NOT NULL,
	poll_count INTEGER DEFAULT 0 NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_ci_revision UNIQUE (repository_id, pull_request_id, head_sha),
	FOREIGN KEY(repository_id) REFERENCES repositories (id),
	FOREIGN KEY(pull_request_id) REFERENCES pull_requests (id)
);

CREATE TABLE IF NOT EXISTS ci_executions (
	id VARCHAR(36) NOT NULL,
	ci_revision_id VARCHAR(36) NOT NULL,
	identity VARCHAR(255) NOT NULL,
	kind VARCHAR(24) NOT NULL,
	provider VARCHAR(255),
	context VARCHAR(512),
	name VARCHAR(512),
	status VARCHAR(32),
	conclusion VARCHAR(32),
	external_url VARCHAR(2048),
	head_sha VARCHAR(64) NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_ci_execution_identity UNIQUE (ci_revision_id, kind, identity),
	FOREIGN KEY(ci_revision_id) REFERENCES ci_revisions (id)
);

CREATE INDEX IF NOT EXISTS ix_background_jobs_dispatch
	ON background_jobs (job_type, state, available_at);
CREATE INDEX IF NOT EXISTS ix_ci_revision_pr_sha
	ON ci_revisions (pull_request_id, head_sha);
CREATE INDEX IF NOT EXISTS ix_ci_execution_revision
	ON ci_executions (ci_revision_id);
