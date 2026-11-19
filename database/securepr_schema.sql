/* =============================================================================
   SecurePR - SQL Server Database Schema
   -----------------------------------------------------------------------------
   Run this script in SQL Server Management Studio (SSMS), connected to a
   SQL Server instance where you want the SecurePR database created.

   This script is SAFE TO RERUN: every object is created with an existence
   check, so re-running it will not delete or overwrite existing user data.
   It only creates objects that are missing.
   ============================================================================= */

IF DB_ID('SecurePR') IS NULL
BEGIN
    CREATE DATABASE SecurePR;
END
GO

USE SecurePR;
GO

/* ---------------------------------------------------------------- users --- */
IF OBJECT_ID('dbo.users', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.users (
        id                UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_users_id DEFAULT NEWID() PRIMARY KEY,
        github_login      NVARCHAR(255) NOT NULL,
        github_user_id    NVARCHAR(64) NULL,
        display_name      NVARCHAR(255) NULL,
        email             NVARCHAR(255) NULL,
        is_repo_owner     BIT NOT NULL CONSTRAINT DF_users_owner DEFAULT 0,
        created_at        DATETIME2 NOT NULL CONSTRAINT DF_users_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_users_github_login UNIQUE (github_login)
    );
END
GO

/* ---------------------------------------------------------- repositories -- */
IF OBJECT_ID('dbo.repositories', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.repositories (
        id                 UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_repositories_id DEFAULT NEWID() PRIMARY KEY,
        github_repo_id     NVARCHAR(64) NOT NULL,
        owner_login        NVARCHAR(255) NOT NULL,
        name               NVARCHAR(255) NOT NULL,
        full_name          NVARCHAR(512) NOT NULL,
        default_branch     NVARCHAR(255) NOT NULL CONSTRAINT DF_repositories_branch DEFAULT 'main',
        installation_id    NVARCHAR(64) NULL,
        is_active          BIT NOT NULL CONSTRAINT DF_repositories_active DEFAULT 1,
        is_auto_merge_enabled BIT NOT NULL CONSTRAINT DF_repositories_auto_merge DEFAULT 0,
        created_at         DATETIME2 NOT NULL CONSTRAINT DF_repositories_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_repositories_github_repo_id UNIQUE (github_repo_id),
        CONSTRAINT UQ_repositories_full_name UNIQUE (full_name)
    );
END
GO

IF COL_LENGTH('dbo.repositories', 'is_auto_merge_enabled') IS NULL
BEGIN
    ALTER TABLE dbo.repositories ADD is_auto_merge_enabled BIT NOT NULL
        CONSTRAINT DF_repositories_auto_merge DEFAULT 0;
END
GO

/* --------------------------------------------------------- pull_requests -- */
IF OBJECT_ID('dbo.pull_requests', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.pull_requests (
        id                  UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_pr_id DEFAULT NEWID() PRIMARY KEY,
        repository_id       UNIQUEIDENTIFIER NOT NULL,
        github_pr_number    INT NOT NULL,
        title               NVARCHAR(1024) NULL,
        author_login        NVARCHAR(255) NULL,
        head_sha            NVARCHAR(64) NULL,
        base_branch         NVARCHAR(255) NULL,
        head_branch         NVARCHAR(255) NULL,
        state               NVARCHAR(32) NOT NULL CONSTRAINT DF_pr_state DEFAULT 'open',
        created_at          DATETIME2 NOT NULL CONSTRAINT DF_pr_created DEFAULT SYSUTCDATETIME(),
        updated_at          DATETIME2 NOT NULL CONSTRAINT DF_pr_updated DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_pr_repository FOREIGN KEY (repository_id) REFERENCES dbo.repositories(id),
        CONSTRAINT UQ_pr_repo_number UNIQUE (repository_id, github_pr_number)
    );
    CREATE INDEX IX_pull_requests_repository_id ON dbo.pull_requests(repository_id);
END
GO

/* -------------------------------------------------------------- commits --- */
IF OBJECT_ID('dbo.commits', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.commits (
        id                 UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_commits_id DEFAULT NEWID() PRIMARY KEY,
        pull_request_id    UNIQUEIDENTIFIER NOT NULL,
        sha                NVARCHAR(64) NOT NULL,
        message            NVARCHAR(MAX) NULL,
        author_login       NVARCHAR(255) NULL,
        committed_at       DATETIME2 NULL,
        CONSTRAINT FK_commits_pr FOREIGN KEY (pull_request_id) REFERENCES dbo.pull_requests(id)
    );
    CREATE INDEX IX_commits_pr ON dbo.commits(pull_request_id);
END
GO

/* -------------------------------------------------------------- policies -- */
IF OBJECT_ID('dbo.policies', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.policies (
        id              UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_policies_id DEFAULT NEWID() PRIMARY KEY,
        repository_id   UNIQUEIDENTIFIER NULL,   -- NULL = global/default policy
        name            NVARCHAR(255) NOT NULL,
        version         INT NOT NULL CONSTRAINT DF_policies_version DEFAULT 1,
        content_json    NVARCHAR(MAX) NOT NULL,  -- full policy document snapshot (JSON)
        is_active       BIT NOT NULL CONSTRAINT DF_policies_active DEFAULT 1,
        created_at      DATETIME2 NOT NULL CONSTRAINT DF_policies_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_policies_repository FOREIGN KEY (repository_id) REFERENCES dbo.repositories(id),
        CONSTRAINT CK_policies_json CHECK (ISJSON(content_json) = 1)
    );
END
GO

/* --------------------------------------------------------- circuit_breakers */
IF OBJECT_ID('dbo.circuit_breakers', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.circuit_breakers (
        id               UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_cb_id DEFAULT NEWID() PRIMARY KEY,
        scope            NVARCHAR(16) NOT NULL,   -- GLOBAL | REPOSITORY | USER
        repository_id    UNIQUEIDENTIFIER NULL,
        user_login       NVARCHAR(255) NULL,
        is_tripped       BIT NOT NULL CONSTRAINT DF_cb_tripped DEFAULT 0,
        reason           NVARCHAR(1024) NULL,
        tripped_by       NVARCHAR(255) NULL,
        tripped_at       DATETIME2 NULL,
        auto_resume_at   DATETIME2 NULL,
        resumed_at       DATETIME2 NULL,
        CONSTRAINT FK_cb_repository FOREIGN KEY (repository_id) REFERENCES dbo.repositories(id),
        CONSTRAINT CK_cb_scope CHECK (scope IN ('GLOBAL', 'REPOSITORY', 'USER'))
    );
END
GO

IF OBJECT_ID('dbo.circuit_breaker_events', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.circuit_breaker_events (
        id                   UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_cbe_id DEFAULT NEWID() PRIMARY KEY,
        circuit_breaker_id   UNIQUEIDENTIFIER NOT NULL,
        action               NVARCHAR(32) NOT NULL,  -- TRIP | RESUME | AUTO_RESUME
        actor                NVARCHAR(255) NULL,
        reason               NVARCHAR(1024) NULL,
        created_at           DATETIME2 NOT NULL CONSTRAINT DF_cbe_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_cbe_cb FOREIGN KEY (circuit_breaker_id) REFERENCES dbo.circuit_breakers(id)
    );
END
GO

/* ---------------------------------------------------- auto_approval_eligibility */
IF OBJECT_ID('dbo.auto_approval_eligibility', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.auto_approval_eligibility (
        id                   UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_eligibility_id DEFAULT NEWID() PRIMARY KEY,
        repository_id        UNIQUEIDENTIFIER NOT NULL,
        user_id              UNIQUEIDENTIFIER NOT NULL,
        is_enabled           BIT NOT NULL CONSTRAINT DF_eligibility_enabled DEFAULT 0,
        max_risk_threshold   INT NOT NULL CONSTRAINT DF_eligibility_threshold DEFAULT 39,
        updated_by           NVARCHAR(255) NULL,
        updated_at           DATETIME2 NOT NULL CONSTRAINT DF_eligibility_updated DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_eligibility_repo FOREIGN KEY (repository_id) REFERENCES dbo.repositories(id),
        CONSTRAINT FK_eligibility_user FOREIGN KEY (user_id) REFERENCES dbo.users(id),
        CONSTRAINT UQ_eligibility_repo_user UNIQUE (repository_id, user_id)
    );
END
GO

/* ---------------------------------------------------------------- reviews -- */
IF OBJECT_ID('dbo.reviews', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.reviews (
        id                        UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_reviews_id DEFAULT NEWID() PRIMARY KEY,
        pull_request_id           UNIQUEIDENTIFIER NOT NULL,
        policy_id                 UNIQUEIDENTIFIER NULL,
        head_sha                  NVARCHAR(64) NOT NULL,
        is_reevaluation           BIT NOT NULL CONSTRAINT DF_reviews_reeval DEFAULT 0,
        superseded_review_id      UNIQUEIDENTIFIER NULL,
        is_ci_wait_placeholder    BIT NOT NULL CONSTRAINT DF_rv_ci_wait_placeholder DEFAULT 0,
        ci_wait_started_at        DATETIME2 NULL,
        ci_wait_pending_checks    NVARCHAR(MAX) NULL,

        score_change_surface      FLOAT NOT NULL CONSTRAINT DF_rv_cs DEFAULT 0,
        score_reversibility       FLOAT NOT NULL CONSTRAINT DF_rv_rev DEFAULT 0,
        score_data_security       FLOAT NOT NULL CONSTRAINT DF_rv_ds DEFAULT 0,
        score_operational         FLOAT NOT NULL CONSTRAINT DF_rv_op DEFAULT 0,
        score_verification        FLOAT NOT NULL CONSTRAINT DF_rv_ver DEFAULT 0,
        score_policy_compliance   FLOAT NOT NULL CONSTRAINT DF_rv_pc DEFAULT 0,
        overall_score             FLOAT NOT NULL CONSTRAINT DF_rv_overall DEFAULT 0,
        risk_category             NVARCHAR(16) NOT NULL CONSTRAINT DF_rv_category DEFAULT 'NOT_EVALUATED',

        decision                  NVARCHAR(16) NOT NULL CONSTRAINT DF_rv_decision DEFAULT 'PENDING',
        decision_reasons          NVARCHAR(MAX) NULL,  -- JSON array of strings
        circuit_breaker_active    BIT NOT NULL CONSTRAINT DF_rv_cb_active DEFAULT 0,
        github_comment_url        NVARCHAR(1024) NULL,
        github_review_state       NVARCHAR(32) NULL,
        github_action_confirmed   BIT NOT NULL CONSTRAINT DF_rv_gh_confirmed DEFAULT 0,
        auto_merge_status         NVARCHAR(48) NULL,
        auto_merge_attempted_at   DATETIME2 NULL,
        auto_merged_at            DATETIME2 NULL,
        merge_commit_sha          NVARCHAR(64) NULL,
        auto_merge_failure_reason NVARCHAR(MAX) NULL,

        analysis_started_at       DATETIME2 NULL,
        analysis_completed_at     DATETIME2 NULL,
        created_at                DATETIME2 NOT NULL CONSTRAINT DF_rv_created DEFAULT SYSUTCDATETIME(),

        CONSTRAINT FK_reviews_pr FOREIGN KEY (pull_request_id) REFERENCES dbo.pull_requests(id),
        CONSTRAINT FK_reviews_policy FOREIGN KEY (policy_id) REFERENCES dbo.policies(id),
        CONSTRAINT FK_reviews_superseded FOREIGN KEY (superseded_review_id) REFERENCES dbo.reviews(id),
        CONSTRAINT UQ_reviews_pr_sha_phase UNIQUE (pull_request_id, head_sha, is_ci_wait_placeholder, is_reevaluation),
        CONSTRAINT CK_reviews_decision CHECK (decision IN ('APPROVED', 'BLOCKED', 'MANUAL_REVIEW', 'WAITING_FOR_CI', 'PENDING')),
        CONSTRAINT CK_reviews_category CHECK (risk_category IN ('NOT_EVALUATED', 'LOW', 'MEDIUM', 'HIGH')),
        CONSTRAINT CK_reviews_reasons_json CHECK (decision_reasons IS NULL OR ISJSON(decision_reasons) = 1)
    );
    CREATE INDEX IX_reviews_pr ON dbo.reviews(pull_request_id);
    CREATE INDEX IX_reviews_decision ON dbo.reviews(decision);
END
GO

/* ---------------------------------------------------------------------------
   Upgrade path for databases created before the CI-wait feature was added.
   Safe to rerun: each ALTER only runs if the column/constraint is missing.
   --------------------------------------------------------------------------- */
IF COL_LENGTH('dbo.reviews', 'is_ci_wait_placeholder') IS NULL
BEGIN
    ALTER TABLE dbo.reviews ADD is_ci_wait_placeholder BIT NOT NULL CONSTRAINT DF_rv_ci_wait_placeholder DEFAULT 0;
END
GO
IF COL_LENGTH('dbo.reviews', 'ci_wait_started_at') IS NULL
BEGIN
    ALTER TABLE dbo.reviews ADD ci_wait_started_at DATETIME2 NULL;
END
GO
IF COL_LENGTH('dbo.reviews', 'ci_wait_pending_checks') IS NULL
BEGIN
    ALTER TABLE dbo.reviews ADD ci_wait_pending_checks NVARCHAR(MAX) NULL;
END
GO
-- Enforce webhook idempotency across application workers. If an older
-- installation already has duplicate legacy rows, preserve that audit data
-- and report it rather than deleting history during a schema upgrade.
IF NOT EXISTS (SELECT 1 FROM sys.key_constraints WHERE name = 'UQ_reviews_pr_sha_phase')
AND NOT EXISTS (
    SELECT pull_request_id, head_sha, is_ci_wait_placeholder, is_reevaluation
    FROM dbo.reviews
    GROUP BY pull_request_id, head_sha, is_ci_wait_placeholder, is_reevaluation
    HAVING COUNT(*) > 1
)
BEGIN
    ALTER TABLE dbo.reviews ADD CONSTRAINT UQ_reviews_pr_sha_phase
        UNIQUE (pull_request_id, head_sha, is_ci_wait_placeholder, is_reevaluation);
END
ELSE IF NOT EXISTS (SELECT 1 FROM sys.key_constraints WHERE name = 'UQ_reviews_pr_sha_phase')
BEGIN
    PRINT 'UQ_reviews_pr_sha_phase was not added because duplicate legacy review phases exist; remediate before enforcing it.';
END
GO
IF COL_LENGTH('dbo.reviews', 'auto_merge_status') IS NULL
BEGIN
    ALTER TABLE dbo.reviews ADD auto_merge_status NVARCHAR(48) NULL;
END
GO
IF COL_LENGTH('dbo.reviews', 'auto_merge_attempted_at') IS NULL
BEGIN
    ALTER TABLE dbo.reviews ADD auto_merge_attempted_at DATETIME2 NULL;
END
GO
IF COL_LENGTH('dbo.reviews', 'auto_merged_at') IS NULL
BEGIN
    ALTER TABLE dbo.reviews ADD auto_merged_at DATETIME2 NULL;
END
GO
IF COL_LENGTH('dbo.reviews', 'merge_commit_sha') IS NULL
BEGIN
    ALTER TABLE dbo.reviews ADD merge_commit_sha NVARCHAR(64) NULL;
END
GO
IF COL_LENGTH('dbo.reviews', 'auto_merge_failure_reason') IS NULL
BEGIN
    ALTER TABLE dbo.reviews ADD auto_merge_failure_reason NVARCHAR(MAX) NULL;
END
GO
-- Widen the decision CHECK constraint to allow WAITING_FOR_CI, if the table
-- was created before this status existed.
IF EXISTS (
    SELECT 1 FROM sys.check_constraints
    WHERE name = 'CK_reviews_decision'
      AND definition NOT LIKE '%WAITING_FOR_CI%'
)
BEGIN
    ALTER TABLE dbo.reviews DROP CONSTRAINT CK_reviews_decision;
    ALTER TABLE dbo.reviews WITH CHECK ADD CONSTRAINT CK_reviews_decision
        CHECK (decision IN ('APPROVED', 'BLOCKED', 'MANUAL_REVIEW', 'WAITING_FOR_CI', 'PENDING'));
END
GO

/* ----------------------------------------------------------- changed_files */
IF OBJECT_ID('dbo.changed_files', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.changed_files (
        id                UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_cf_id DEFAULT NEWID() PRIMARY KEY,
        review_id         UNIQUEIDENTIFIER NOT NULL,
        file_path         NVARCHAR(1024) NOT NULL,
        change_type       NVARCHAR(16) NULL,  -- added|modified|removed|renamed
        additions         INT NOT NULL CONSTRAINT DF_cf_additions DEFAULT 0,
        deletions         INT NOT NULL CONSTRAINT DF_cf_deletions DEFAULT 0,
        is_blocked_path   BIT NOT NULL CONSTRAINT DF_cf_blocked DEFAULT 0,
        patch_excerpt     NVARCHAR(MAX) NULL,
        CONSTRAINT FK_cf_review FOREIGN KEY (review_id) REFERENCES dbo.reviews(id)
    );
    CREATE INDEX IX_changed_files_review ON dbo.changed_files(review_id);
END
GO

/* --------------------------------------------------------------- findings -- */
IF OBJECT_ID('dbo.findings', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.findings (
        id                     UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_findings_id DEFAULT NEWID() PRIMARY KEY,
        review_id              UNIQUEIDENTIFIER NOT NULL,
        dimension              NVARCHAR(64) NULL,
        severity               NVARCHAR(16) NULL,  -- LOW|MEDIUM|HIGH|CRITICAL
        title                  NVARCHAR(512) NULL,
        description            NVARCHAR(MAX) NULL,
        file_path              NVARCHAR(1024) NULL,
        line_reference         NVARCHAR(64) NULL,
        is_mandatory_blocker   BIT NOT NULL CONSTRAINT DF_findings_blocker DEFAULT 0,
        CONSTRAINT FK_findings_review FOREIGN KEY (review_id) REFERENCES dbo.reviews(id),
        CONSTRAINT CK_findings_severity CHECK (severity IS NULL OR severity IN ('LOW','MEDIUM','HIGH','CRITICAL'))
    );
    CREATE INDEX IX_findings_review ON dbo.findings(review_id);
END
GO

/* --------------------------------------------------------------- evidence -- */
IF OBJECT_ID('dbo.evidence', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.evidence (
        id              UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_evidence_id DEFAULT NEWID() PRIMARY KEY,
        review_id       UNIQUEIDENTIFIER NOT NULL,
        evidence_type   NVARCHAR(64) NULL,  -- code_snippet|ci_result|diff|policy_match|ai_rationale|error
        source          NVARCHAR(255) NULL,
        content         NVARCHAR(MAX) NULL,
        created_at      DATETIME2 NOT NULL CONSTRAINT DF_evidence_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_evidence_review FOREIGN KEY (review_id) REFERENCES dbo.reviews(id)
    );
    CREATE INDEX IX_evidence_review ON dbo.evidence(review_id);
END
GO

/* ------------------------------------------------------- impact_predictions */
IF OBJECT_ID('dbo.impact_predictions', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.impact_predictions (
        id                      UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_impact_id DEFAULT NEWID() PRIMARY KEY,
        review_id               UNIQUEIDENTIFIER NOT NULL,
        component_type          NVARCHAR(64) NULL,  -- function|api|service|module|database|dependency|test
        component_name          NVARCHAR(512) NULL,
        confidence              NVARCHAR(16) NOT NULL CONSTRAINT DF_impact_confidence DEFAULT 'INFERRED',
        evidence_summary        NVARCHAR(MAX) NULL,
        potential_consequence   NVARCHAR(MAX) NULL,
        CONSTRAINT FK_impact_review FOREIGN KEY (review_id) REFERENCES dbo.reviews(id),
        CONSTRAINT CK_impact_confidence CHECK (confidence IN ('CONFIRMED', 'INFERRED', 'UNRESOLVED'))
    );
    CREATE INDEX IX_impact_review ON dbo.impact_predictions(review_id);
END
GO

/* ----------------------------------------------------------- github_actions */
IF OBJECT_ID('dbo.github_actions', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.github_actions (
        id                 UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_gha_id DEFAULT NEWID() PRIMARY KEY,
        review_id          UNIQUEIDENTIFIER NULL,
        action_type        NVARCHAR(64) NULL,  -- post_comment|submit_review|notify
        request_payload    NVARCHAR(MAX) NULL,
        response_status    INT NULL,
        response_payload   NVARCHAR(MAX) NULL,
        succeeded          BIT NOT NULL CONSTRAINT DF_gha_succeeded DEFAULT 0,
        created_at         DATETIME2 NOT NULL CONSTRAINT DF_gha_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_gha_review FOREIGN KEY (review_id) REFERENCES dbo.reviews(id)
    );
    CREATE INDEX IX_github_actions_review ON dbo.github_actions(review_id);
END
GO

/* -------------------------------------------------- review_publications */
IF OBJECT_ID('dbo.review_publications', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.review_publications (
        id                 UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_rpub_id DEFAULT NEWID() PRIMARY KEY,
        repository_id      UNIQUEIDENTIFIER NOT NULL,
        pull_request_id    UNIQUEIDENTIFIER NOT NULL,
        head_sha           NVARCHAR(64) NOT NULL,
        message_type       NVARCHAR(32) NOT NULL,
        state              NVARCHAR(16) NOT NULL CONSTRAINT DF_rpub_state DEFAULT 'PENDING',
        github_message_id  NVARCHAR(64) NULL,
        marker             NVARCHAR(255) NULL,
        published_at       DATETIME2 NULL,
        created_at         DATETIME2 NOT NULL CONSTRAINT DF_rpub_created DEFAULT SYSUTCDATETIME(),
        updated_at         DATETIME2 NOT NULL CONSTRAINT DF_rpub_updated DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_rpub_revision_type UNIQUE (repository_id, pull_request_id, head_sha, message_type),
        CONSTRAINT FK_rpub_repository FOREIGN KEY (repository_id) REFERENCES dbo.repositories(id),
        CONSTRAINT FK_rpub_pull_request FOREIGN KEY (pull_request_id) REFERENCES dbo.pull_requests(id)
    );
    CREATE INDEX IX_review_publications_pr_sha ON dbo.review_publications(pull_request_id, head_sha);
END
GO

/* ------------------------------------------------------------ notifications */
IF OBJECT_ID('dbo.notifications', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.notifications (
        id            UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_notif_id DEFAULT NEWID() PRIMARY KEY,
        review_id     UNIQUEIDENTIFIER NULL,
        recipient     NVARCHAR(255) NULL,
        channel       NVARCHAR(32) NULL,  -- github|webhook
        subject       NVARCHAR(512) NULL,
        body          NVARCHAR(MAX) NULL,
        delivered     BIT NOT NULL CONSTRAINT DF_notif_delivered DEFAULT 0,
        created_at    DATETIME2 NOT NULL CONSTRAINT DF_notif_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_notif_review FOREIGN KEY (review_id) REFERENCES dbo.reviews(id)
    );
END
GO

/* ------------------------------------------------------------- audit_log -- */
IF OBJECT_ID('dbo.audit_log', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.audit_log (
        id             UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_audit_id DEFAULT NEWID() PRIMARY KEY,
        actor          NVARCHAR(255) NULL,
        action         NVARCHAR(255) NULL,
        target_type    NVARCHAR(64) NULL,
        target_id      UNIQUEIDENTIFIER NULL,
        details_json   NVARCHAR(MAX) NULL,
        created_at     DATETIME2 NOT NULL CONSTRAINT DF_audit_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT CK_audit_json CHECK (details_json IS NULL OR ISJSON(details_json) = 1)
    );
END
GO

/* ----------------------------------------------- independent review and CI */
IF OBJECT_ID('dbo.background_jobs', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.background_jobs (
        id                 UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_jobs_id DEFAULT NEWID() PRIMARY KEY,
        job_type           NVARCHAR(32) NOT NULL,
        idempotency_key    NVARCHAR(512) NOT NULL,
        payload            NVARCHAR(MAX) NOT NULL,
        state              NVARCHAR(16) NOT NULL CONSTRAINT DF_jobs_state DEFAULT 'QUEUED',
        attempts           INT NOT NULL CONSTRAINT DF_jobs_attempts DEFAULT 0,
        available_at       DATETIME2 NOT NULL CONSTRAINT DF_jobs_available DEFAULT SYSUTCDATETIME(),
        lease_until        DATETIME2 NULL,
        last_error         NVARCHAR(MAX) NULL,
        created_at         DATETIME2 NOT NULL CONSTRAINT DF_jobs_created DEFAULT SYSUTCDATETIME(),
        updated_at         DATETIME2 NOT NULL CONSTRAINT DF_jobs_updated DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_background_jobs_idempotency UNIQUE (idempotency_key),
        CONSTRAINT CK_background_jobs_payload CHECK (ISJSON(payload) = 1)
    );
    CREATE INDEX IX_background_jobs_dispatch
        ON dbo.background_jobs(job_type, state, available_at);
END
GO

IF OBJECT_ID('dbo.ci_revisions', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ci_revisions (
        id                 UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_ci_revisions_id DEFAULT NEWID() PRIMARY KEY,
        repository_id      UNIQUEIDENTIFIER NOT NULL,
        pull_request_id    UNIQUEIDENTIFIER NOT NULL,
        head_sha           NVARCHAR(64) NOT NULL,
        state              NVARCHAR(24) NOT NULL CONSTRAINT DF_ci_revisions_state DEFAULT 'UNKNOWN',
        error              NVARCHAR(MAX) NULL,
        retry_count        INT NOT NULL CONSTRAINT DF_ci_revisions_retry DEFAULT 0,
        poll_count         INT NOT NULL CONSTRAINT DF_ci_revisions_poll DEFAULT 0,
        updated_at         DATETIME2 NOT NULL CONSTRAINT DF_ci_revisions_updated DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_ci_revision UNIQUE (repository_id, pull_request_id, head_sha),
        CONSTRAINT FK_ci_revision_repository FOREIGN KEY (repository_id) REFERENCES dbo.repositories(id),
        CONSTRAINT FK_ci_revision_pull_request FOREIGN KEY (pull_request_id) REFERENCES dbo.pull_requests(id)
    );
    CREATE INDEX IX_ci_revision_pr_sha ON dbo.ci_revisions(pull_request_id, head_sha);
END
GO

IF OBJECT_ID('dbo.ci_executions', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ci_executions (
        id                 UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_ci_executions_id DEFAULT NEWID() PRIMARY KEY,
        ci_revision_id     UNIQUEIDENTIFIER NOT NULL,
        identity           NVARCHAR(255) NOT NULL,
        kind               NVARCHAR(24) NOT NULL,
        provider           NVARCHAR(255) NULL,
        context            NVARCHAR(512) NULL,
        name               NVARCHAR(512) NULL,
        status             NVARCHAR(32) NULL,
        conclusion         NVARCHAR(32) NULL,
        external_url       NVARCHAR(2048) NULL,
        head_sha           NVARCHAR(64) NOT NULL,
        updated_at         DATETIME2 NOT NULL CONSTRAINT DF_ci_executions_updated DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_ci_execution_identity UNIQUE (ci_revision_id, kind, identity),
        CONSTRAINT FK_ci_execution_revision FOREIGN KEY (ci_revision_id) REFERENCES dbo.ci_revisions(id)
    );
    CREATE INDEX IX_ci_execution_revision ON dbo.ci_executions(ci_revision_id);
END
GO

PRINT 'SecurePR schema verified/created successfully.';
GO
