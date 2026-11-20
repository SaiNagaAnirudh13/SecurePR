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
| `block_on_ci_failure` | Legacy field retained for compatibility. CI failure always blocks approval; this setting cannot disable the hard CI gate. |
| `auto_merge_allowed_risk_categories` | Risk categories eligible for repository auto-merge. Defaults to `LOW`; the global low-risk-only setting can further restrict it. |
| `blocked_paths` | Glob patterns that create a hard policy violation when changed. |
| `human_review_required_paths` | Glob patterns that route an otherwise-eligible change to manual review. |
| `max_files_for_auto_approval` | Maximum changed-file count eligible for automatic approval. |
| `disallowed_authors` | GitHub logins prohibited from automatic approval. |
| `circuit_breaker_conditions` | Documents circuit-breaker intent. Automatic tripping requires a separately scheduled integration; manual dashboard controls work independently. |

## CI-gated review and CI monitoring

The PR webhook queues durable review and CI-refresh jobs for the exact head
SHA. Analysis can run while CI is pending, but only its risk score is
informational: the deterministic decision engine will not publish a final
decision until complete CI discovery verifies every applicable check.

CI is discovered dynamically from GitHub Check Runs and legacy commit
statuses. Check Suites are diagnostic metadata, not executable checks. The
backend also discovers required contexts from branch protection and
applicable rulesets. Each execution retains its SHA, provider, name/context,
status, conclusion, URL, and app identity. Webhooks are the primary update
mechanism; failures retry with capped exponential backoff and incomplete or
nonterminal states continue polling until verification completes or the PR
revision changes.

The CI states are:

| State | Meaning |
|---|---|
| `UNKNOWN` | Discovery is incomplete or returned an unrecognized result. |
| `UNAVAILABLE` | GitHub CI discovery failed; the error is recorded and retries continue with capped backoff. |
| `NOT_CONFIGURED` | Discovery completed successfully but found no check runs or commit statuses. This is not a pass. |
| `IN_PROGRESS` | At least one execution remains queued/running or a required context has not appeared. No final decision is published. |
| `PASSED` | Discovery is complete, at least one execution exists, every execution passed, and all required contexts passed. |
| `FAILED` | At least one discovered execution failed (`failure`, `error`, `timed_out`, `startup_failure`, or `action_required`); approval is prohibited. |
| `NON_SUCCESS` | All executions are terminal, but one or more are `cancelled`, `neutral`, `skipped`, or `stale`; this is not reported as passed. |

Unknown statuses, API errors, empty discovery, missing required checks, and
incomplete pagination are never converted to success. Any known failure
blocks regardless of `block_on_ci_failure`. CI transitions reconcile the
decision for that revision; if a prior SecurePR approval is no longer valid,
SecurePR attempts to dismiss it and records the outcome.

Auto-merge is separately guarded and remains disabled by default. It requires
a fresh live CI re-check in which at least one execution exists and every
discovered and required check is `PASSED`, plus the exact reviewed SHA,
GitHub auto-merge setting, configured method, App permissions, and GitHub
review/branch-protection eligibility. SecurePR requests GitHub's protected
auto-merge; it does not invoke the immediate merge endpoint.

## Conditions that prevent automatic approval

The deterministic decision engine enforces the following:

1. AI must complete and pass response validation for the exact PR head SHA.
   Missing, failed, unavailable, or invalid AI analysis produces
   `REVIEW_UNAVAILABLE`, no risk score, and no approval decision.
2. Critical findings and blocked-path policy violations block.
3. Any known CI failure or configured hard policy violation blocks.
4. An active circuit breaker, an ineligible author, an excessive risk score,
   or a human-review policy warning routes to `MANUAL_REVIEW`.
5. Otherwise, an eligible low-risk review may be approved.

CI pending, absent, incomplete, or unavailable is `WAITING_FOR_CI`; it cannot
be converted to approval or manual review before CI verification.

## Editing policies safely

- Bump `version` when changing substantive policy rules.
- Use `blocked_paths` only for hard prohibitions; use
  `human_review_required_paths` for changes that need human judgment.
- Required CI contexts come from GitHub branch protection/rulesets and are
  matched against actual provider records. Do not silently omit a required
  context that cannot be discovered.
