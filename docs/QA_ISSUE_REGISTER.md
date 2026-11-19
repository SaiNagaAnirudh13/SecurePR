# SecurePR Stabilization QA Issue Register

This register records confirmed defects addressed during the stabilization
review and the validation gaps that remain. Automated tests use mocked GitHub
and AI clients unless a test explicitly states otherwise; live GitHub App,
Supabase, and Render services were not exercised.

| ID | Severity | Bug and reproduction | Root cause | Fix | Regression coverage |
|---|---|---|---|---|---|
| QA-01 | High | Trigger a PR webhook and let SecurePR list prior reviews or marked comments. | GitHub returns top-level JSON arrays for these endpoints, but the client required a named object wrapper, causing a successful response to raise `ValueError`. | Accept both raw arrays and the requested wrapper while keeping malformed payloads and HTTP/API errors explicit. | `test_github_client.py`: raw and empty collections, pagination, unexpected payload, lookup failure. |
| QA-02 | High | Open, reopen, or synchronize a PR while CI is delayed. | The lifecycle lacked a prompt durable status-comment job tied to the PR revision. | Queue an idempotent waiting-status job and update its marked comment with available analysis and actual CI state, independently of CI timing. | `test_webhook_lifecycle.py`, `test_review_ci_independence.py`, `test_comment_formatter.py`. |
| QA-03 | High | Retry publishing after a lost response, or receive repeated webhook deliveries. | Publication retries could not reliably reconcile already-created bot records. | Search for the marker before retrying, update the existing marked issue comment in place, and preserve API errors rather than treating them as empty results. | `test_github_client.py`: retry/idempotency and in-place comment updates. |
| QA-04 | High | Deliver delayed/out-of-order PR webhooks or change the PR head during analysis. | Stored PR revision state could lag GitHub's current head and allow stale work to proceed. | Reconcile the current GitHub head before processing and enqueue work for a newly observed revision; handle closed PR state and refresh PR metadata. | `test_review_ci_independence.py`, `test_webhook_lifecycle.py`. |
| QA-05 | High | Have one CI discovery API fail while another source has known executions. | A partial discovery failure could erase previously discovered executions. | Preserve stored executions when only one discovery source fails; expose unavailability instead of converting it to success or no checks. | `test_review_ci_independence.py`: partial discovery failure preservation. |
| QA-06 | Medium | Return malformed Gemini JSON, incomplete score dimensions, or out-of-range values. | Model output was consumed without fully validating its schema and numeric ranges. | Validate model output and use the existing deterministic scoring path for unusable model responses. | `test_gemini_client.py`: malformed and out-of-range output cases. |
| QA-07 | Medium | Query `/health` while PostgreSQL is unavailable. | Health status did not reflect database connectivity. | Return HTTP 503 when database connectivity fails. | `test_health.py`. |
| QA-08 | Medium | Inspect a review after failed publication or view a finding with a known line. | Dashboard status reflected analysis completion without confirming GitHub publication, and finding detail omitted the line reference. | Report unconfirmed publication as queued/processing/failed based on durable job state; include finding line references in the API and dashboard. | `test_dashboard_contract.py`; backend API tests. |
| QA-09 | High | Run the dependency audit against the original pinned requirements. | Several pinned packages had published vulnerability advisories. | Upgrade the affected production and QA dependencies to audited versions. | `pip-audit -r requirements.txt`: no known vulnerabilities found. |
| QA-10 | Medium | Inspect repository automation on push or pull request. | The main repository had no CI workflow covering its backend, frontend build, dependency audit, and secret scan. | Add `.github/workflows/ci.yml` with those checks and currently scoped Ruff/Mypy gates. | Workflow definition added; execution on GitHub remains unverified until pushed and run. |

## Remaining verification gaps

- The complete local backend suite passes, and the dashboard production build,
  scoped Ruff/Mypy checks, and dependency audit pass.
- Full-project Ruff and Mypy still report pre-existing baseline debt (108 Ruff
  findings, 31 files needing formatting, and 117 Mypy errors); the stabilization
  workflow gates fatal Ruff classes and the changed critical Mypy modules
  rather than silently suppressing or mass-reformatting that debt.
- The production PostgreSQL migrations were not applied to a live Supabase
  database. Local model-backed SQLite tests do not establish production
  migration correctness.
- No authenticated live GitHub App/test-repository workflow or Render runtime
  deployment was available for end-to-end verification. GitHub Actions,
  secret-scanning results, production health, and deployed commit SHA therefore
  remain unverified.
