-- Durable idempotency for SecurePR GitHub bot messages.
CREATE TABLE IF NOT EXISTS review_publications (
	id VARCHAR(36) NOT NULL,
	repository_id VARCHAR(36) NOT NULL,
	pull_request_id VARCHAR(36) NOT NULL,
	head_sha VARCHAR(64) NOT NULL,
	message_type VARCHAR(32) NOT NULL,
	state VARCHAR(16) DEFAULT 'PENDING' NOT NULL,
	github_message_id VARCHAR(64),
	marker VARCHAR(255),
	published_at TIMESTAMP WITHOUT TIME ZONE,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_review_publication_revision_type
		UNIQUE (repository_id, pull_request_id, head_sha, message_type),
	FOREIGN KEY(repository_id) REFERENCES repositories (id),
	FOREIGN KEY(pull_request_id) REFERENCES pull_requests (id)
);

CREATE INDEX IF NOT EXISTS ix_review_publications_pr_sha
	ON review_publications (pull_request_id, head_sha);
