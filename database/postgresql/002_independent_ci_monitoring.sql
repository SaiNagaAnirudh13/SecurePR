-- Durable review and CI state. Safe to re-run; no existing data is dropped.
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
