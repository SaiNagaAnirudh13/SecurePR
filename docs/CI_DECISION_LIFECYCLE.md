# CI-Gated Review Lifecycle

SecurePR's decision engine is the single authority for review and merge
decisions. A risk score is never sufficient to approve a pull request.

## Lifecycle

```text
RECEIVED
  -> AI_PENDING -> AI_RUNNING
       -> AI_COMPLETED -> WAITING_FOR_CI
                       -> CI_VERIFIED -> POLICY_EVALUATION -> APPROVED
                                                        -> MANUAL_REVIEW
                                                        -> BLOCKED
                       -> BLOCKED (a discovered CI failure)
       -> AI_FAILED / AI_UNAVAILABLE / AI_INVALID_RESPONSE
                       -> REVIEW_UNAVAILABLE
```

AI availability is the first decision gate. Gemini must return a complete,
schema-valid analysis for the exact current PR head SHA before any risk-based
decision is possible. An AI failure publishes an explicit `UNAVAILABLE`
result, no risk score, and the independently observed CI state; it never
falls back to heuristic-only risk or approval. The selected Gemini model is
used as configured; SecurePR does not silently select a substitute model.

`UNKNOWN`, `UNAVAILABLE`, incomplete discovery, missing required checks,
pending executions, and terminal non-success conclusions remain
`WAITING_FOR_CI`. A failed execution takes precedence over pending checks and
blocks approval immediately. CI success requires complete discovery, at least
one actual check execution, every discovered execution successful, and every
required branch-protection/ruleset status check satisfied. Skipped, neutral,
cancelled, stale, and unrecognized conclusions are not success.

The risk analysis can run while CI is pending, but its result is informational;
the decision engine evaluates the CI state before any policy/risk-based
approval. When CI changes, a durable revision-scoped job reconciles the stored
analysis rather than rerunning the AI for each poll.

## Discovery and publication

Check Runs and the latest Commit Status for each context are executable
records. Check Suites are diagnostic metadata only. Branch protection and
applicable rulesets are queried for required status contexts. All records and
required contexts are tied to the exact PR head SHA. Discovery errors and
incomplete pagination remain non-approvable; polling and webhook events
reconcile the current revision.

Gemini timeouts, unavailable responses, and malformed output are recorded as
explicit AI lifecycle failures. No heuristic score is published as the AI
result: the risk category and score are not evaluated, and the review becomes
`REVIEW_UNAVAILABLE`. It cannot request approval or auto-merge. The decision
uses the latest CI state for the exact current PR
head SHA; if CI turns green while the initial analysis is running, a new full
review is queued so policy evaluation runs against that verified state. A
later review/webhook retry can recover a failed durable job using its existing
idempotency key.

The four durable workers (`REVIEW`, `CI_REFRESH`, `CI_NOTIFY`, and
`REVIEW_STATUS`) start with the API service. `/health` is unhealthy if any
worker thread is not alive so the hosting platform can detect the issue; job
pickup, completion, retry, and publication outcomes include structured
identifiers in backend logs.

The durable review, CI revision, and publication rows are keyed by PR and
revision. A single waiting comment is updated in place. Final review and
completion publications are idempotent on retries; when a prior approval is
invalidated, SecurePR attempts to dismiss it and publishes a corrective
decision. A stale job cannot publish against a newer PR head.

## Auto-merge

`AUTO_MERGE_ENABLED` is false by default. A separate per-repository SecurePR
setting is also required. SecurePR rechecks the exact head SHA, CI, branch
protection, required reviews, GitHub repository auto-merge setting, App
permissions, and configured merge method. It requests GitHub auto-merge through
the GraphQL `enablePullRequestAutoMerge` mutation; it does not call GitHub's
immediate merge endpoint or bypass branch protection. The dashboard reports
the confirmed request separately from a GitHub-confirmed merge event.

GitHub App installations need read access to Checks, Commit Statuses, pull
requests, repository metadata, and branch/ruleset protections, plus
`Pull requests: write` to publish reviews and request auto-merge. Missing
permission or unavailable protection data blocks approval or merge rather than
being treated as an empty successful result.

## Render migration startup

The Render service installs `backend/requirements.txt` in its build command,
which provides Alembic before the service starts. Render's Pre-Deploy Command
is not available for this service, so `python run.py` acquires a PostgreSQL
advisory lock, logs the current and target revisions, runs `alembic upgrade
head`, and verifies the resulting revision before starting Uvicorn. The lock
serializes startup across service instances. A migration error or failed
revision check stops startup; FastAPI independently verifies that migrations
are current before starting background workers.

## Database update

Apply `database/postgresql/004_ci_gated_decisions.sql` to existing PostgreSQL
installations before deploying this version. SQL Server installations should
rerun `database/securepr_schema.sql`, which contains idempotent upgrade steps.
