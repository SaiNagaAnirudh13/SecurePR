# SecurePR — Complete Testing Guide

This walks through testing SecurePR end-to-end: starting the full application,
verifying GitHub/webhook integration, and running the major decision
scenarios (clean PR, risky/policy-violating PR, Circuit Breaker, and a PR
whose CI takes a while to finish). It uses the `sample-repo/` folder included
in this project.

## 1. Start the complete application

Follow `docs/SETUP_GUIDE.md` end to end first. In short, in this order:

1. `DATABASE_URL` points to the Supabase PostgreSQL project; the backend creates any missing ORM tables on startup.
2. Backend: `cd backend && venv\Scripts\activate && uvicorn app.main:app --reload --port 8000`
3. Frontend: `cd frontend\dashboard && python -m http.server 5173`
4. If testing against real GitHub (not just unit tests), a tunnel to your backend: `ngrok http 8000`, with that URL set as the GitHub App's webhook URL.
5. Open the dashboard: **http://localhost:5173**

## 2. Verify backend and frontend are healthy

- `http://localhost:8000/health` → `{"status":"ok", ...}`
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

Install the SecurePR GitHub App on this repository (`docs/SETUP_GUIDE.md`, section 7, step 11). The CI workflow at `.github/workflows/ci.yml` defines three jobs — `build`, `unit-tests`, `lint` — matching the `mandatory_ci_checks` in `policies/default_policy.json` exactly; this is what lets the CI-wait scenario in section 8 below actually exercise something real.

## 5. Configure automatic-approval eligibility (no IDs to type)

1. Dashboard → **Auto-Approval** tab.
2. Pick your repository from the **Repository** dropdown.
3. Pick your GitHub username from the **GitHub user** dropdown (it's populated from PR authors SecurePR has already seen — if you haven't opened a PR yet, use the manual field once, then it'll appear in the dropdown afterward).
4. Check **Enabled**, set a threshold (e.g. `39` for LOW-risk only), click **Save**.
5. Leave a second GitHub user (a collaborator or a fork's author) unconfigured, to test the manual-review path in Scenario D below.

## 6. Verify the dashboard's data (general)

- **Repositories & PRs** tab: pick a repository from the dropdown, click any PR row to see its latest decision and a link into **Evidence & Replay**.
- **Evidence & Replay** tab: pick a repository, then a PR; each past review appears as an expandable row — click one to see its full findings, evidence, changed files, GitHub actions, and decision reasons, exactly as they were at the time (re-evaluating never overwrites this).
- **Change Impact** tab: pick a recent review from the dropdown to see its CONFIRMED/INFERRED/UNRESOLVED component predictions.
- **Circuit Breaker** tab: shows every breaker as a card with its scope, status, and a one-click Resume button.
- **Policies** tab: shows the policy document(s) SecurePR has actually evaluated PRs against, including the mandatory checks, blocked paths, and thresholds.

None of these pages should ever ask you to paste a Repository ID, PR ID, or Review ID — if one does, something upstream failed to load; check the browser console.

---

## 7. Decision Scenarios

### Scenario A — Low-Risk / Clean Pull Request

```powershell
git checkout -b low-risk-fix
```
Edit `app/utils.py`: fix the typo in the `greet()` docstring. Commit and push, then open a PR.

**Expected:** CI runs (`build`, `unit-tests`, `lint` all pass) → SecurePR's comment starts with "⏳ Waiting for CI" and is replaced within seconds of CI finishing by a final comment showing **✅ Approved** (if your user is eligible with a high-enough threshold) with a low risk score, a green CI Verification table, and "All required checks passed" among the decision reasons. The dashboard's Evidence & Replay tab should show exactly these two review rows: the placeholder, then the final — never duplicated, never stuck.

### Scenario B — Higher-Risk / Security Finding

```powershell
git checkout -b high-risk-secret
```
Edit `app/db.py`: add a line like `API_KEY = "sk-test-12345"` (an obviously-fake test value — never commit a real secret). Commit, push, open a PR.

**Expected:** a 🔴 **Blocked** decision — note it does **not** wait for CI first, since a confirmed secret is a critical finding that blocks immediately regardless of check status. The comment's Findings section calls out the hardcoded secret with a recommended action, and Data Security scores high in the Risk Breakdown table.

### Scenario C — Blocked Path

```powershell
git checkout -b blocked-path-change
```
Edit `infra/secrets/placeholder.pem`. Commit, push, open a PR.

**Expected:** 🔴 **Blocked** — "Blocked path modified" finding, same immediate-block behavior as Scenario B.

### Scenario D — Manual Review (ineligible author or over-threshold)

Repeat Scenario A's change as a user you did **not** mark eligible in step 5 (a collaborator, or push from a fork).

**Expected:** CI passes, risk is LOW, but the decision is 🟡 **Manual Review Required** — "Author is not currently configured as eligible for automatic approval" is the stated reason. This demonstrates eligibility is enforced independently of risk score.

### Scenario E — CI Pending / Slow Checks ("Waiting for CI")

This is the scenario the CI-wait feature exists for.

1. Temporarily add a delay to the `unit-tests` job in `.github/workflows/ci.yml` (e.g. `- run: sleep 90` before the pytest step), push it on its own branch, and open a PR.
2. Watch the PR immediately after opening it: SecurePR should post the **"⏳ Waiting for CI"** comment within a few seconds, naming `unit-tests` (and possibly `lint`/`build` if they haven't started) as pending — and should **not** post any approve/block verdict yet.
3. Once the delayed job finishes, within a few seconds (the `check_run`/`check_suite` webhook fast path) a second comment should appear with the final decision, now using the completed CI results.
4. In the **Evidence & Replay** tab for this PR, you should see exactly two review rows: "Waiting for CI" followed by the final one — confirming no duplicate placeholder comments were posted while waiting.
5. Optional — to see the **timeout fallback** instead of the fast path: set `CI_WAIT_TIMEOUT_SECONDS=30` in `.env` before starting the backend, and make a check run take longer than that. SecurePR should finalize as **Manual Review Required** with a reason stating it timed out waiting for CI, rather than silently approving.

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

Enable `AUTO_MERGE_ENABLED=true`, restart the backend, and set `is_auto_merge_enabled=1` for only the test repository as documented in the setup guide.

- A: low risk + passed CI: expect Waiting for CI, Approved, then Approved & Automatically Merged.
- B: disable the global or repository flag: expect Approved but no merge.
- C: medium risk: expect Manual Review and no merge.
- D: high/critical finding: expect Blocked and no merge.
- E: trip the circuit breaker: expect Manual Review and no merge.
- F: remove Contents write from the installation or prevent the bot with branch protection: expect Approved, Manual Merge Required, `auto_merge_status=FAILED`, and an open PR.

Run automated tests with `cd backend; venv\Scripts\python -m pytest`.

## 10. Final checklist

- [ ] Backend and frontend both start without errors.
- [ ] Dashboard successfully calls the backend (no CORS/API-error toasts).
- [ ] A real GitHub webhook reaches the backend and is processed.
- [ ] SecurePR correctly waits for mandatory CI checks before deciding (Scenario E), and never silently approves on a timeout.
- [ ] The final SecurePR comment uses the completed CI results, not a snapshot taken before CI finished.
- [ ] Circuit Breaker trip/resume visibly changes PR decisions without disabling analysis or comments.
- [ ] Every dashboard page can be used end-to-end via dropdowns and clicks, with no Repository/PR/Review ID typed anywhere.
- [ ] No console errors in the browser dev tools while navigating all seven dashboard tabs.
