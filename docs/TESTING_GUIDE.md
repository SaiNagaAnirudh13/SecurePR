# SecurePR — Complete Testing Guide

This walks through testing SecurePR end to end, including the CI-gated
decision and CI-monitoring lifecycles. It uses the `sample-repo/` folder
included in this project.

## 1. Start the complete application

Follow `docs/SETUP_GUIDE.md` end to end first. In short, in this order:

1. `DATABASE_URL` points to the Supabase PostgreSQL project; apply the
   numbered migrations, including
   `database/postgresql/004_ci_gated_decisions.sql`, before deploying to an
   existing database.
2. Backend: `cd backend && venv\Scripts\activate && uvicorn app.main:app --reload --port 8000`
3. Frontend: `cd frontend\dashboard && python -m http.server 5173`
4. If testing against real GitHub (not just unit tests), a tunnel to your backend: `ngrok http 8000`, with that URL set as the GitHub App's webhook URL.
5. Open the dashboard: **http://localhost:5173**

## 2. Verify backend and frontend are healthy

- `http://localhost:8000/health` → `{"status":"ok", ...}` when the database is reachable; HTTP 503 indicates the database is unavailable.
- `http://localhost:8000/docs` → Swagger UI lists every endpoint
- Dashboard **Overview** tab loads with no "API error" toasts (it will show all-zero counts until your first PR is analyzed — that's expected)

## 3. Verify the GitHub App and webhook integration

1. In the GitHub App settings, open **Advanced** → **Recent Deliveries** to confirm GitHub is actually sending webhooks to your `ngrok` URL.
2. Open any PR (even a trivial one) in the repository the App is installed on.
3. Watch the `uvicorn` terminal — within a second or two you should see `New GitHub webhook request received` and the installation-ID diagnostic log lines, ending in `Webhook processing completed successfully.`
4. Refresh the dashboard's **Overview** tab — the repository should now appear under "Connected Repositories", and a Recent Pull Requests row should appear once analysis finishes.

If the webhook never arrives, see the Troubleshooting table at the end of `docs/SETUP_GUIDE.md`.

## 4. Create a sample GitHub repository and push the sample app

```powershell
cd sample-repo
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<your-username>/securepr-sample-app.git
git push -u origin main
```

Install the SecurePR GitHub App on this repository (`docs/SETUP_GUIDE.md`, section 7, step 11). SecurePR discovers actual GitHub check runs and commit statuses for each head SHA; it does not require configured names or aliases.

## 5. Configure automatic-approval eligibility (no IDs to type)

1. Dashboard → **Auto-Approval** tab.
2. Pick your repository from the **Repository** dropdown.
3. Pick your GitHub username from the **GitHub user** dropdown (it's populated from PR authors SecurePR has already seen — if you haven't opened a PR yet, use the manual field once, then it'll appear in the dropdown afterward).
4. Check **Enabled**, set a threshold (e.g. `39` for LOW-risk only), click **Save**.
5. Leave a second GitHub user (a collaborator or a fork's author) unconfigured, to test the manual-review path in Scenario D below.

## 6. Verify the dashboard's data (general)

- **Repositories & PRs** tab: pick a repository from the dropdown, click any PR row to see separate AI-review and CI states and a link into **Evidence & Replay**.
- **Evidence & Replay** tab: pick a repository, then a PR; each past review appears as an expandable row — click one to see its full findings, evidence, changed files, GitHub actions, and decision reasons, exactly as they were at the time (re-evaluating never overwrites this).
- **Change Impact** tab: pick a recent review from the dropdown to see its CONFIRMED/INFERRED/UNRESOLVED component predictions.
- **Circuit Breaker** tab: shows every breaker as a card with its scope, status, and a one-click Resume button.
- **Policies** tab: shows the policy documents SecurePR has evaluated, including CI failure behavior, blocked paths, and thresholds.

None of these pages should ever ask you to paste a Repository ID, PR ID, or Review ID — if one does, something upstream failed to load; check the browser console.

---

## 7. Decision Scenarios

### Scenario A — Low-Risk / Clean Pull Request

```powershell
git checkout -b low-risk-fix
```
Edit `app/utils.py`: fix the typo in the `greet()` docstring. Commit and push, then open a PR.

**Expected:** SecurePR promptly posts `SecurePR Review — Waiting for CI`. It may compute a risk score while CI runs, but it does not publish an approval or manual-review decision until every applicable check for the exact SHA is verified. Once checks pass, the policy decision and detailed report are published. The waiting comment is updated in place; duplicate webhooks do not duplicate it.

### Scenario B — Higher-Risk / Security Finding

```powershell
git checkout -b high-risk-secret
```
Edit `app/db.py`: add a line like `API_KEY = "sk-test-12345"` (an obviously-fake test value — never commit a real secret). Commit, push, open a PR.

**Expected:** while CI is pending, the only decision is `WAITING_FOR_CI`; a risk score is informational. If CI passes, the critical finding blocks. If CI fails, the result is `BLOCKED: CI Failed` and the report lists failed checks as well as risk context.

### Scenario C — Blocked Path

```powershell
git checkout -b blocked-path-change
```
Edit `infra/secrets/placeholder.pem`. Commit, push, open a PR.

**Expected:** 🔴 **Blocked** — "Blocked path modified" finding, same immediate-block behavior as Scenario B.

### Scenario D — Manual Review (ineligible author or over-threshold)

Repeat Scenario A's change as a user you did **not** mark eligible in step 5 (a collaborator, or push from a fork).

**Expected:** CI passes, risk is LOW, but the decision is 🟡 **Manual Review Required** — "Author is not currently configured as eligible for automatic approval" is the stated reason. This demonstrates eligibility is enforced independently of risk score.

### Scenario E — CI Pending / Slow Checks

This verifies the hard CI gate and that the risk analysis is not mistaken for a
final decision.

1. Temporarily add a delay to the `unit-tests` job in `.github/workflows/ci.yml` (e.g. `- run: sleep 90` before the pytest step), push it on its own branch, and open a PR.
2. The waiting-for-CI status comment should appear promptly while the job remains in progress; it must not wait for the job.
3. Confirm the dashboard shows the exact SHA, risk score, and CI `IN_PROGRESS`, while the authoritative decision remains `WAITING_FOR_CI`; no approval review or completion notification should exist yet.
4. When the delayed job finishes successfully, its webhook should update the same status comment, evaluate policy, and publish one final decision.
5. Also test a PR with no CI workflow, an API error, and an empty/incomplete discovery response. Each must remain non-approvable; polling/retries continue, and the dashboard must never show CI passed.
6. Make one check fail while another passes. Expect `BLOCKED: CI Failed`, the failed check/provider/details, no APPROVE review, and no auto-merge request.
7. Test cancelled, skipped, neutral, and unknown conclusions. None satisfies the gate unless a future explicit policy rule is added.
8. Push a new commit while a prior SHA is being reviewed. Confirm the old job/result cannot publish an approval for the new head.

### Scenario F — Circuit Breaker

1. Dashboard → **Circuit Breaker** tab. Scope: **Repository**, pick your repo, give a reason (e.g. "investigating a bad merge"), click **Trip Breaker** (confirm the dialog).
2. Push a new commit to the low-risk PR from Scenario A (or open a new low-risk PR).
3. **Expected:** SecurePR still analyzes and comments as usual, but the decision is now 🟡 **Manual Review Required**, with "Circuit breaker is active: investigating a bad merge" in the decision reasons — not a silent skip, and not a BLOCKED.
4. Back in the dashboard, click **Resume** on that breaker's card.
5. Re-evaluate the PR from the **Evidence & Replay** tab ("Re-evaluate (keeps history)") and confirm the decision returns to its normal (e.g. Approved) outcome, while the circuit-breaker-affected review from step 3 is still visible in the history, unmodified.

---

## 8. Verify the GitHub bot comment quality

For at least one PR from the scenarios above, open the actual comment on GitHub and confirm it reads like a polished report, not a debug dump:

- A clear header stating the decision, with an emoji and a summary table (risk score, CI status, circuit breaker status, timestamp).
- A collapsible **Risk Breakdown** section with all six dimensions and a visual score bar.
- A collapsible **CI Verification** table showing which checks passed/failed/were pending.
- A **Findings** section with severity, a clear title, a plain-language description, and a recommended action — not a generic repeated message.
- A collapsible **Changed Files** section with a +/- summary.
- A collapsible **Change Impact Simulator** table, when the PR touched anything the simulator could analyze.
- A **Why this decision** section in plain language.
- A footer explaining that auto-merge is disabled by default and requires every safety gate, plus a link back into the dashboard.

## 9. Auto-merge scenarios

Only in a disposable test environment, save an enabled account/repository
configuration with a deliberate risk threshold in the dashboard. The saved
dashboard configuration is the only user authorization source; do not use
environment variables or direct database edits to enable auto-approval or
auto-merge. Production remains disabled until its dashboard configuration and
all backend safety gates are verified.

- A: low risk + all discovered and required checks passed: expect SecurePR to request GitHub auto-merge, and report it enabled only after GitHub confirms.
- B: disable or remove the saved dashboard configuration: expect the exact dashboard-disabled/missing status and no GitHub request.
- C: configure GitHub repository auto-merge off: expect `GITHUB_AUTO_MERGE_DISABLED`.
- D: remove required GitHub App permissions: expect `PERMISSION_MISSING` with the API reason and no merge request.
- E: CI pending/failed, required review missing, branch protection blocked, or configured method unavailable: expect the exact blocker and no request.
- F: simulate a GitHub rejection of the GraphQL mutation: expect failure recorded accurately; do not report enabled or merged.
- G: verify `MERGED` appears only after GitHub confirms the merge through a webhook.
- H: disable stale-approval dismissal on the base branch: expect branch-protection failure and no auto-merge request.
- I: push a new head during auto-merge activation: expect a verified cancellation; if cancellation cannot be verified, expect an explicit failure and no success status.
- J: make cancellation fail on a `synchronize` webhook: expect a retryable 503 and no review job for the new head.

Run automated tests with `cd backend; venv\Scripts\python -m pytest`.

## 10. Final checklist

- [ ] Backend and frontend both start without errors.
- [ ] Dashboard successfully calls the backend (no CORS/API-error toasts).
- [ ] A real GitHub webhook reaches the backend and is processed.
- [ ] A slow, absent, incomplete, or unavailable CI result never produces a final approval (Scenario E).
- [ ] CI transitions update the same waiting comment and never turn missing, unknown, or failed CI into a passed result.
- [ ] Circuit Breaker trip/resume visibly changes PR decisions without disabling analysis or comments.
- [ ] Every dashboard page can be used end-to-end via dropdowns and clicks, with no Repository/PR/Review ID typed anywhere.
- [ ] No console errors in the browser dev tools while navigating all seven dashboard tabs.
