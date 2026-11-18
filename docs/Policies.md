# SecurePR Policies

This document describes the policy engine and the default policy in
`policies/default_policy.json`. Policies are plain JSON files; changing
policy rules does not require application-code changes.

## How policies are loaded

1. SecurePR looks for `policies/<owner>__<repo>.json`.
2. If no repository-specific file exists, it uses `policies/default_policy.json`.
3. Each review stores the policy name and version used at analysis time.

## Review policy fields

| Field | Meaning |
|---|---|
| `auto_approval_risk_threshold` | Default maximum overall risk score for eligible authors. A per-author dashboard threshold may be lower. |
| `block_on_ci_failure` | When true, a discovered terminal CI failure known before review publication is a policy violation and blocks the review. Failures discovered later update CI state and trigger a separate notification; they do not rerun or duplicate the AI review. |
| `auto_merge_allowed_risk_categories` | Risk categories eligible for repository auto-merge. Defaults to `LOW`; the global low-risk-only setting can further restrict it. |
| `blocked_paths` | Glob patterns that create a hard policy violation when changed. |
| `human_review_required_paths` | Glob patterns that route an otherwise-eligible change to manual review. |
| `max_files_for_auto_approval` | Maximum changed-file count eligible for automatic approval. |
| `disallowed_authors` | GitHub logins prohibited from automatic approval. |
| `circuit_breaker_conditions` | Documents circuit-breaker intent. Automatic tripping requires a separately scheduled integration; manual dashboard controls work independently. |

## Independent AI review and CI monitoring

The PR webhook queues two durable jobs: an AI review for the exact head SHA
and an independent CI refresh. Review analysis does not query, poll, or wait
for CI before it can publish. Queued or in-progress CI, absent checks, unknown
check names, and discovery API errors do not hold the AI review.

CI is discovered dynamically from GitHub Check Runs, Check Suites, and legacy
commit statuses. Each actual record retains its SHA, provider, name/context,
status, conclusion, and external URL. Check suites alone do not count as an
active execution. Webhooks are the primary update mechanism; bounded,
exponential-backoff retries and bounded polling are fallback mechanisms.

The CI states are:

| State | Meaning |
|---|---|
| `UNKNOWN` | Discovery has not completed or returned an unrecognized result. |
| `UNAVAILABLE` | GitHub CI discovery failed; the error is recorded and bounded retries continue. |
| `NOT_CONFIGURED` | Discovery completed successfully but found no check runs or commit statuses. This is not a pass. |
| `IN_PROGRESS` | At least one real discovered execution remains queued or running. The AI review can already be published. |
| `PASSED` | At least one execution was discovered and every execution passed. |
| `FAILED` | At least one discovered terminal execution failed (`failure`, `error`, `timed_out`, `startup_failure`, or `action_required`). |
| `NON_SUCCESS` | All executions are terminal, but one or more are `cancelled`, `neutral`, `skipped`, or `stale`; this is not reported as passed. |

Unknown statuses remain `UNKNOWN`; API errors and missing records are never
converted to success or failure. A real failure known at analysis time blocks
only when `block_on_ci_failure` is enabled. A late failure updates the CI
dashboard and creates a separate failure notification with a link to the
discovered execution; it does not rerun the analysis.

Auto-merge is separately guarded and remains disabled by default. It requires
a fresh live CI re-check in which at least one execution exists and every
discovered execution is `PASSED`. Missing, pending, unavailable, failed, or
non-success CI states fail closed for merging.

## Conditions that prevent automatic approval

The deterministic decision engine enforces the following:

1. Incomplete changed-file analysis routes to `MANUAL_REVIEW`.
2. Critical findings and blocked-path policy violations block.
3. A configured policy violation, including an applicable known CI failure,
   blocks.
4. An active circuit breaker, an ineligible author, an excessive risk score,
   or a human-review policy warning routes to `MANUAL_REVIEW`.
5. Otherwise, an eligible low-risk review may be approved.

CI pending, absent, or unavailable is not a deferred AI-review state.

## Editing policies safely

- Bump `version` when changing substantive policy rules.
- Use `blocked_paths` only for hard prohibitions; use
  `human_review_required_paths` for changes that need human judgment.
- Do not add named mandatory-check lists or aliases. CI identities are
  discovered from actual provider records, not matched against a fixed list.
