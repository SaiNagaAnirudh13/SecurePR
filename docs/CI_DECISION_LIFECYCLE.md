# CI-Gated Review Lifecycle

SecurePR's decision engine is the single authority for review and merge
decisions. A risk score is never sufficient to approve a pull request.

## Lifecycle

```text
RECEIVED
  -> ANALYZING
  -> WAITING_FOR_CI
       -> CI_VERIFIED -> POLICY_EVALUATION -> APPROVED
                                        -> MANUAL_REVIEW
                                        -> BLOCKED
       -> BLOCKED (a discovered CI failure)
```

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

## Database update

Apply `database/postgresql/004_ci_gated_decisions.sql` to existing PostgreSQL
installations before deploying this version. SQL Server installations should
rerun `database/securepr_schema.sql`, which contains idempotent upgrade steps.
