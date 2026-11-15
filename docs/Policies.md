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
| `mandatory_ci_checks` | Logical CI check names that must report `success` on the PR's head commit. Any missing or non-passing mandatory check is a **hard violation** → BLOCKED. |
| `ci_check_aliases` | Optional per-policy mapping of the logical name to one or more GitHub check-run names, such as `Build / Install dependencies` or `Lint / Ruff`. This lets SecurePR match human-readable GitHub titles to the logical requirement names without brittle exact string equality. |
| `blocked_paths` | Glob patterns. Any changed file matching one of these is a **hard violation** → BLOCKED, regardless of risk score or user eligibility. |
| `human_review_required_paths` | Glob patterns that force MANUAL_REVIEW (not a block) even for otherwise-eligible, low-risk changes — e.g. auth, payments, CI workflow files. |
| `max_files_for_auto_approval` | PRs touching more files than this are routed to manual review, independent of risk score. |
| `disallowed_authors` | GitHub logins explicitly barred from automatic approval (hard violation), useful for bots or contractors under heightened review. |
| `circuit_breaker_conditions` | Conditions under which SecurePR should automatically trip a circuit breaker (see below). |

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
4. **A mandatory CI check that exists but hasn't completed yet** (queued or
   in progress) → **WAITING_FOR_CI**. This is a deferred state, not a
   verdict: SecurePR posts a short "waiting" comment and re-evaluates
   automatically once GitHub reports a result (see "Waiting for GitHub
   checks" below) — it is never treated as a violation, and it never
   silently resolves to APPROVED.
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
three states by `policy_engine.evaluate_policy`, based on GitHub's own
`status` (queued/in_progress/completed) and `conclusion` fields — never a
fixed delay:

- **Succeeded** — `status: completed`, `conclusion` in (`success`,
  `neutral`, `skipped`).
- **Failed** — `status: completed`, anything else → a hard violation (rule 3
  above), regardless of how long it takes other checks to finish.
- **Pending** — not yet `completed` at all, including a check that hasn't
  started reporting yet → WAITING_FOR_CI (rule 4), never a violation.

Once every mandatory check is Succeeded or Failed (no more Pending), the
pipeline re-runs and the decision resolves normally. If that never happens
within `CI_WAIT_TIMEOUT_SECONDS` (15 minutes by default), SecurePR finalizes
as MANUAL_REVIEW rather than waiting forever or approving on an incomplete
picture — see `docs/SETUP_GUIDE.md` section 9 for the full mechanism
(webhook fast path + polling fallback).

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
