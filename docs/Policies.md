# SecurePR Policies

This document describes how SecurePR's policy engine works and the initial
default policy shipped in `policies/default_policy.json`. Policies are plain
JSON files — **no application code changes are needed to update them.**

## How policies are loaded

1. SecurePR looks for a repository-specific file named
   `policies/<owner>__<repo>.json` (slashes replaced with `__`).
2. If none exists, it falls back to `policies/default_policy.json`.
3. Every policy document is versioned (`"version"` field). Each `Review`
   record stores which policy name + version it was evaluated against, so
   changing a policy later never rewrites history — old reviews still show
   exactly what they were judged by.

To add a repository override, copy `default_policy.json` to
`policies/<owner>__<repo>.json` and edit it. To change it via the database
instead (so the dashboard can edit it live), insert/update a row in the
`policies` table with `repository_id` set to that repo's id.

## Default policy fields

| Field | Meaning |
|---|---|
| `auto_approval_risk_threshold` | Default max overall risk score (0-100) eligible PRs may auto-approve at. Per-user thresholds set in the dashboard override this per author, but can never exceed what mandatory checks and blocked paths allow. |
| `mandatory_ci_checks` | Optional logical CI check names that must report a successful terminal result on the PR's head commit. The default policy uses an empty list; all discovered checks are still tracked, but only explicitly configured names are mandatory policy gates. A completed unsuccessful required check is a **hard violation** → BLOCKED; a missing or in-progress required check remains pending. |
| `ci_check_aliases` | Optional per-policy mapping of the logical name to one or more GitHub check-run names, such as `Build / Install dependencies`, `Tests / Unit tests`, or `test` when that check runs unit tests. Names match by normalized exact equality or an explicit alias; unrelated substring matches do not satisfy a mandatory check. |
| `auto_merge_allowed_risk_categories` | Risk categories eligible for repository auto-merge. Defaults to `["LOW"]`; MEDIUM/HIGH require explicit policy permission, and the global `AUTO_MERGE_LOW_RISK_ONLY` setting can further restrict this list. |
| `blocked_paths` | Glob patterns. Any changed file matching one of these is a **hard violation** → BLOCKED, regardless of risk score or user eligibility. |
| `human_review_required_paths` | Glob patterns that force MANUAL_REVIEW (not a block) even for otherwise-eligible, low-risk changes — e.g. auth, payments, CI workflow files. |
| `max_files_for_auto_approval` | PRs touching more files than this are routed to manual review, independent of risk score. |
| `disallowed_authors` | GitHub logins explicitly barred from automatic approval (hard violation), useful for bots or contractors under heightened review. |
| `circuit_breaker_conditions` | Conditions under which SecurePR should automatically trip a circuit breaker (see below). |

## Dynamic CI discovery and legacy-policy migration

The default policy (`version: 3`) no longer assumes that repositories run
checks named `build`, `unit-tests`, or `lint`. SecurePR discovers check runs,
commit statuses, and check suites for the current PR head SHA and shows their
names, providers, results, URLs, and SHA in the dashboard and review comment.
The default `mandatory_ci_checks` and `ci_check_aliases` are empty.

This is an intentional, versioned policy change, not an automatic rewrite of
repository overrides. Existing `policies/<owner>__<repo>.json` files and
explicit `mandatory_ci_checks` entries remain in place and continue to be
enforced. To keep any of the previous baseline requirements for a repository,
copy the names and aliases from its existing policy into that repository's
override before adopting version 3. Missing required checks are never treated
as passed.

CI discovery waits until checks are terminal and the configured
`CI_DISCOVERY_STABILIZATION_SECONDS` window passes without a newly discovered
or changed check. New webhook events and polling use the same evaluator. When
no checks appear after stabilization, SecurePR records either
`NO_CI_CONFIGURED` (no suites) or `NO_CHECKS_FOUND` (suites but no runs/statuses)
and routes the review to manual review; it never reports an empty CI result as
successful. Transient API failures remain `ERROR` while SecurePR retries and
become `TIMEOUT` at `CI_WAIT_TIMEOUT_SECONDS`, which also routes to manual
review.

| GitHub result | SecurePR CI state |
|---|---|
| queued, pending, requested, or in progress | `RUNNING` |
| completed / success | `SUCCESS` (unless another check fails or is neutral) |
| completed / neutral or skipped | `NEUTRAL` (unless another check fails) |
| completed / failure, cancelled, timed out, stale, action required, or commit-status error | `FAILURE` |
| stable discovery period, new checks may still appear | `DISCOVERING` |
| no check suites, runs, or statuses after stabilization | `NO_CI_CONFIGURED` |
| suites exist but no runs or statuses after stabilization | `NO_CHECKS_FOUND` |
| GitHub API failure | `ERROR`, then `TIMEOUT` if retrying exceeds the wait limit |

CI status is persisted as review evidence and remains distinct from the AI
review decision and GitHub branch protection. Optional mandatory checks remain
a separate policy evaluation. Auto-merge remains opt-in and disabled by
default; a re-check with missing, failed, unavailable, or absent CI will not
merge.

## Conditions that always prevent automatic approval

Regardless of any policy file content, the decision engine
(`backend/app/services/decision_engine.py`) hard-codes these non-negotiable
rules, evaluated in order:

1. **Incomplete analysis** (e.g. GitHub API fetch failed) → MANUAL_REVIEW.
2. **Any CRITICAL finding** (e.g. a hardcoded secret or destructive SQL
   statement found directly in the diff) → BLOCKED **immediately** — this
   does not wait for CI, since there's no reason to make a contributor wait
   to find out their PR touched a blocked path or leaked a credential.
3. **Any policy hard violation already knowable** (blocked path touched, a
   disallowed author, or a mandatory CI check that has **completed** and did
   not succeed) → BLOCKED.
4. **CI discovery is still running or an explicitly required check is
   pending** → **WAITING_FOR_CI**. This is a deferred state, not a verdict:
   SecurePR re-evaluates when webhooks arrive and polls as a fallback. It is
   never treated as success.
5. **Active Circuit Breaker** (global, repo, or user scope) → MANUAL_REVIEW.
6. **Author not explicitly marked eligible** for automatic approval in this
   repository → MANUAL_REVIEW.
7. **Overall risk score above the author's configured threshold** →
   MANUAL_REVIEW.
8. **Policy soft-warning path touched, or file-count limit exceeded** →
   MANUAL_REVIEW.
9. Otherwise → APPROVED.

No per-user setting or risk threshold can ever skip rules 1–4. This is
enforced in code, not just in the policy file, specifically so a
misconfigured policy document cannot silently disable the safety floor.

### Waiting for GitHub checks before the final decision

A mandatory check in `mandatory_ci_checks` is classified into exactly one of
three policy states by `policy_engine.evaluate_policy`, based on GitHub's own
`status` (queued/in_progress/completed) and `conclusion` fields — never a
fixed delay:

- **Succeeded** — `status: completed`, `conclusion` in (`success`,
  `neutral`, `skipped`).
- **Failed** — `status: completed`, anything else → a hard violation (rule 3
  above), regardless of how long it takes other checks to finish.
- **Pending** — not yet `completed` at all, including a check that hasn't
  started reporting yet → WAITING_FOR_CI (rule 4), never a violation.

Check discovery itself is stabilized separately from the required-check
policy. If pending checks or GitHub API failures persist until
`CI_WAIT_TIMEOUT_SECONDS` (15 minutes by default), SecurePR finalizes as
MANUAL_REVIEW rather than waiting forever or approving on an incomplete
picture. Repositories with no CI finalize as MANUAL_REVIEW after the
stabilization window. See `docs/SETUP_GUIDE.md` section 9 for webhook and
polling behavior.

## Circuit Breaker conditions

**Inactive** means no global, repository, or user breaker is currently blocking
automated approval. **Active/Tripped** means a breaker at one of those scopes
temporarily suspends automated approval. CI event processing and risk analysis
continue independently; the breaker is evaluated as part of the final decision
and does not prevent a pending review from being finalized.

The default policy's `circuit_breaker_conditions` block documents intent;
actually tripping a breaker automatically (e.g. "auto-trip after 3 blocked
PRs in 60 minutes") is implemented by a scheduled check you can add as a
periodic job calling `POST /api/circuit-breakers/trip` — see
`docs/SETUP_GUIDE.md` for wiring this up. Out of the box, Circuit Breakers
are controlled manually via the dashboard's Circuit Breaker tab.

## Editing policies safely

- Always bump `"version"` when you change a policy's substantive rules, so
  historical reviews remain distinguishable from reviews made under the new
  rules.
- Keep `blocked_paths` and `mandatory_ci_checks` conservative — these are
  hard blockers with no override.
- Use `human_review_required_paths` for "important but not inherently
  dangerous" areas (e.g. billing, infra-as-code) rather than `blocked_paths`,
  so PRs still get analyzed and commented on, just not auto-approved.
