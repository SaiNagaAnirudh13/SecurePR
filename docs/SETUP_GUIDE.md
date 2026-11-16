# SecurePR — Setup Guide (Windows + VS Code)

This guide assumes no prior project setup. Follow it top to bottom.

## 1. Required software

Install these first:

1. **Python 3.11+** — https://www.python.org/downloads/ (check "Add Python to PATH" during install).
2. **Visual Studio Code** — https://code.visualstudio.com/, plus the "Python" extension from the VS Code marketplace.
3. **Supabase PostgreSQL project** — already created for this setup.
4. **SQL Server and ODBC Driver 17/18** are optional and only needed to use the retained SQL Server fallback.
5. **Node.js 20+** — not required to serve the dashboard locally, but used by its dependency-free production build for Vercel.
6. **Git** (optional but recommended) — https://git-scm.com/download/win, for the real-world testing guide.

## 2. Unzip and open the project

1. Extract `SecurePR.zip` somewhere convenient, e.g. `C:\Projects\SecurePR`.
2. Open VS Code → File → Open Folder → select `C:\Projects\SecurePR`.

## 3. Create a Python virtual environment

Open a VS Code terminal (``Ctrl+` ``) and run:

```powershell
cd backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

The PostgreSQL driver is installed from `requirements.txt`. Keep the ODBC driver only if you intend to use the SQL Server fallback.

## 4. Configure `.env`

If `backend/.env` does not exist yet, create it from the example. If it already
exists, do not overwrite it; update `DATABASE_URL` and preserve its other local
settings.

```powershell
Copy-Item .env.example .env
```

Open `backend\.env` in VS Code and set:

- `DATABASE_URL` — the Supabase PostgreSQL URL described in section 5 below.
- `GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY_PATH` or `GITHUB_PRIVATE_KEY`, and `GITHUB_WEBHOOK_SECRET` — see section 7 (GitHub App setup).
- `GEMINI_API_KEY`, `GEMINI_MODEL` — see section 8 (Gemini setup).

**Never commit `.env`** — it's already covered by `.gitignore`.

### Supabase Auth and dashboard access

The dashboard uses the official Supabase JavaScript client, pinned in
`frontend/dashboard/index.html`. Put only the Supabase project URL, public
anon key, and backend API URL in `frontend/dashboard/config.js`. Never put a
service-role key in the dashboard configuration.

In `backend/.env`, set `SUPABASE_URL`,
`SUPABASE_JWT_ALGORITHM` (the algorithm configured for the Supabase project's
JWT signing key), and `SUPABASE_AUTH_AUDIENCE` (normally `authenticated`).
For legacy `HS256` projects only, set `SUPABASE_JWT_SECRET` as a backend-only
secret with at least 32 characters. Asymmetric keys are verified against
Supabase's JWKS endpoint.

Create operator accounts in Supabase Auth and disable open sign-ups unless
they are needed. Add their Supabase Auth user UUIDs to
`SECUREPR_OPERATOR_IDS`; add administrators to `SECUREPR_ADMIN_IDS` as well.
The backend validates the signed token's issuer, audience, expiry, subject,
and signature before checking these server-side allowlists. Read-only
dashboard/API routes require an operator; re-evaluation, circuit-breaker
changes, and approval-eligibility changes require an administrator. Roles
cannot be granted by frontend data or user-editable metadata.

The browser persists and refreshes the Supabase session. When making backend
requests it sends the access token as a Bearer token. The frontend API base
URL is read from `config.js` and is not user-editable, preventing an
authenticated token from being sent to an arbitrary URL.

## 5. Supabase PostgreSQL setup

Set the connection string in `backend/.env` using the database password from the Supabase project settings. Do not put that password in source control or `.env.example`.

```env
DATABASE_URL=postgresql://postgres:[PASSWORD]@db.haxqjifflpwzxowevzvz.supabase.co:5432/postgres?sslmode=require
```

URL-encode special characters in the password. `sslmode=require` enables TLS for the Supabase connection. On first startup, SQLAlchemy creates missing tables, constraints, and indexes from `backend/app/models/models.py`. The versioned reference schema is also available as `database/postgresql/001_initial_schema.sql` for Supabase SQL Editor. It is safe to rerun and does not drop data; it does not alter columns in tables that already exist. For later schema changes, add a new numbered migration and update the ORM models. The existing `database/securepr_schema.sql` remains the SQL Server fallback and must not be run against PostgreSQL.

The app does not copy existing SQL Server rows automatically. Before migrating any local data, ensure the source SQL Server is available and take a backup; the current local database was not reachable during this setup.

To switch back to SQL Server, point `DATABASE_URL` to a `mssql+pyodbc://...` URL and use the retained SQL Server schema script. `pyodbc` remains in the dependencies for that fallback.

## 6. Frontend development and production build

The existing dashboard is static HTML/CSS/JavaScript, not a React/Vite app.
Local serving has no dependency-install step. The production build uses only
Node.js built-ins and generates a `dist/` folder from public build settings:

```powershell
cd frontend\dashboard
$env:VITE_SUPABASE_URL = "https://your-project-ref.supabase.co"
$env:VITE_SUPABASE_ANON_KEY = "your-public-anon-or-publishable-key"
$env:VITE_SECUREPR_API_URL = "https://your-backend-domain"
npm run build
```

The API URL and Supabase public anon/publishable key are client-visible. Never
use a Supabase service-role/secret key in these settings. The build rejects
service-role key formats and requires HTTPS origins.

## 7. GitHub App setup

1. Go to **GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**.
2. **App name:** `SecurePR` (or any unique name).
3. **Homepage URL:** `http://localhost:8000` (or your deployed URL).
4. **Webhook URL:** for local testing only, use a tunneling tool like `ngrok http 8000` and its temporary HTTPS URL. For production, use `https://<production-backend-domain>/webhooks/github`; never leave the ngrok URL configured in the production GitHub App.
5. **Webhook secret:** generate a random string and put it in both the GitHub App form and `GITHUB_WEBHOOK_SECRET` in `.env`.
6. **Permissions** (Repository permissions):
   - Pull requests: **Read & write** (to read diffs and post reviews/comments)
   - Checks: **Read-only** (to read CI check results)
   - Commit statuses: **Read-only** (to read legacy CI provider results)
   - Contents: **Read & write** (required by GitHub for the Pull Request merge API)
   - Issues: **Read & write** (to post owner-notification comments)
   - Metadata: **Read-only** (mandatory)
   - Do not grant any broader permissions than those listed. Contents write is used only by SecurePR's guarded merge flow; branch protection is never bypassed.
7. **Subscribe to events:** `Pull request`, `Check run`, `Check suite`, and `Commit status`.
   The last two are what let SecurePR notice the moment your CI finishes and finalize
   its CI discovery promptly, instead of waiting for the next poll; final
   review still waits for the configured stabilization window.
8. **Where can this GitHub App be installed:** "Only on this account" is fine for testing.
9. Click **Create GitHub App**. Note the **App ID** shown at the top → put it in `GITHUB_APP_ID`.
10. Scroll down → **Generate a private key** → a `.pem` file downloads. For local development, create a `backend/secrets/` folder, store the key there, and set `GITHUB_PRIVATE_KEY_PATH=./secrets/github-app-private-key.pem`. For Render, enter the PEM contents in the backend-only `GITHUB_PRIVATE_KEY` secret setting. Literal `\n` sequences are also normalized by the backend. Do not commit the key or put it in `GITHUB_PRIVATE_KEY_PATH` as a public URL.
11. **Install the App** on the repository you want SecurePR to analyze (Install App → choose repository).

## 8. Gemini API configuration

1. Get an API key from Google AI Studio: https://aistudio.google.com/apikey.
2. Set `GEMINI_API_KEY` in `.env`.
3. Set `GEMINI_MODEL=gemini-3.8-flash` (the model requested for this project).
   **Model availability note:** SecurePR automatically checks at call time whether
   `GEMINI_MODEL` is listed for your API key. If `gemini-3.8-flash` is not yet
   available on your account/region, it transparently falls back to
   `GEMINI_FALLBACK_MODEL` (default `gemini-2.0-flash`, also configurable in
   `.env`) and logs a warning — analysis continues uninterrupted either way,
   since AI scoring is only one input into the deterministic decision engine,
   never a requirement for it to run.

## 9. Dynamic GitHub CI discovery and stabilization

The default policy does not require names such as `build`, `unit-tests`, or
`lint`. For each revision SecurePR refreshes the PR head SHA, retrieves Check
Runs, legacy commit statuses, and Check Suites for that exact SHA, then
persists each check's identity, provider, name, status, conclusion, URL, and
SHA as review evidence.

1. Pending/queued checks keep the PR in **CI running**. SecurePR listens to
   every `check_run`, `check_suite`, and `status` transition, including
   non-terminal events, and re-fetches authoritative state.
2. Once discovered results become terminal, SecurePR waits for
   `CI_DISCOVERY_STABILIZATION_SECONDS` (default 30s) without new or changed
   checks before normal finalization. A late check resets the window. Webhook
   and polling paths use the same evaluator.
3. If no checks or suites are discovered after stabilization, SecurePR records
   `NO_CI_CONFIGURED`; if suites exist but no check runs or statuses appear,
   it records `NO_CHECKS_FOUND`. Neither state is reported as successful CI;
   the review is routed to manual review.
4. GitHub API failures stay unavailable/error and are retried. The bounded
   poller checks every `CI_WAIT_POLL_INTERVAL_SECONDS` (default 20s) until
   `CI_WAIT_TIMEOUT_SECONDS` (default 900s / 15 minutes), then records
   `TIMEOUT` and routes to manual review. All three values are configurable in
   the backend environment.
5. Explicit `mandatory_ci_checks` in a repository-specific policy remain a
   separate policy gate. Missing required checks are never treated as passed.

All review creation and publication for one repository/PR is serialized with
a PostgreSQL session advisory lock (or a SQL Server application lock when
using the fallback), held across database commits and GitHub publication. The
reviews table's unique revision/phase constraint is
an additional database guard. Duplicate webhook deliveries and concurrent
polls therefore reuse the existing waiting/final review rather than posting
again. CI lifecycle logs include the event type, repository, PR number, event
SHA, current-revision match, finalization trigger, and an ignore reason when
the event does not match an open PR's current head.

This means a webhook delivery for `check_suite`/`check_run`/`status` must reach your
backend the same way the `pull_request` webhook does (step 7, same `ngrok`
tunnel). If you only subscribed to `Pull request` events on an existing
GitHub App, edit the App's webhook event subscriptions to add `Check run`,
`Check suite`, and `Commit status`, then reinstall/update permissions if
GitHub asks you to.

The optional waiting comment is controlled by `POST_CI_WAITING_COMMENT`
(default `true`). `CI_DISCOVERY_STABILIZATION_SECONDS` controls the quiet
window after the last discovered/changed result. `CI_WAIT_POLL_INTERVAL_SECONDS`
and `CI_WAIT_TIMEOUT_SECONDS` configure the fallback monitor; they do not
replace checking GitHub's actual terminal CI results.

## 10. Safe auto-merge

Auto-merge is off by default and runs only after the existing final analysis produces `APPROVED`; the CI-wait placeholder never merges anything.

The repository policy's `auto_merge_allowed_risk_categories` defaults to
`["LOW"]`; MEDIUM/HIGH categories require explicit inclusion in that
repository's policy. `AUTO_MERGE_LOW_RISK_ONLY=true` remains a global cap
that a repository policy cannot override. All discovered CI checks must be
available, terminal, and non-failing before auto-merge; no-check and API-error
states are not mergeable. Explicit mandatory checks remain separately
enforced. `AUTO_MERGE_REQUIRE_ALL_CI` cannot disable these safety gates.

For local testing, set the following in `backend/.env` and restart the backend:

```env
AUTO_MERGE_ENABLED=true
AUTO_MERGE_LOW_RISK_ONLY=true
AUTO_MERGE_REQUIRE_ALL_CI=true
```

After the backend has initialized the schema, explicitly opt in only the test repository:

```sql
UPDATE repositories SET is_auto_merge_enabled = TRUE
WHERE full_name = 'OWNER/REPOSITORY';
```

Set `AUTO_MERGE_ENABLED=false` or that repository flag to `FALSE` to disable it. Both switches must be true. The App requires Pull requests: Read & write, Checks: Read-only, Issues: Read & write, Metadata: Read-only, and Contents: Read & write. Existing App installations must be updated or reinstalled after the Contents permission change.

Immediately before merging, SecurePR refetches the PR and requires: open state; unchanged reviewed head/base; GitHub mergeability and no conflict; all discovered checks terminal and non-failing; approved/LOW decision within the eligible author's threshold; no HIGH/CRITICAL/mandatory-blocker finding or policy violation; and an inactive circuit breaker. The merge API includes the reviewed SHA as a stale-head guard. Branch protection is not bypassed.

The review stores `auto_merge_status`, attempt/merge timestamps, merge SHA, and failure reason. Every attempt is also stored in `github_actions` and evidence. Permissions, protection, conflicts, or race failures leave the PR open and produce an Approved, Manual Merge Required comment.

## 11. Start the application

**Backend** (from `backend/`, with the venv activated):

```powershell
uvicorn app.main:app --reload --port 8000
```

You should see `Database tables verified/created.` in the log, and the development API docs available at `http://localhost:8000/docs`. A database connection or schema-creation error prevents startup; check the sanitized `DATABASE_URL` configuration and Supabase network access.

**Dashboard** (from the project root, in a second terminal):

```powershell
cd frontend\dashboard
python -m http.server 5173
```

Set `supabaseUrl`, `supabaseAnonKey`, and `apiBaseUrl` in
`frontend\dashboard\config.js`, then open **http://localhost:5173** and sign
in with a provisioned Supabase Auth user. In the sidebar, the **API base URL**
is displayed from that configuration and is read-only.

For a hosting platform that supplies `PORT`, run `python run.py` from
`backend/`; it binds to `0.0.0.0` and uses that port (default `8000`).
Production startup requires the database, GitHub webhook, Supabase JWT,
operator/admin allowlists, canonical dashboard/API HTTPS origins, and
PostgreSQL SSL configuration. GitHub App private-key configuration can be
provided as `GITHUB_PRIVATE_KEY`, or as an existing key file through
`GITHUB_PRIVATE_KEY_PATH`. Missing or invalid critical settings stop startup.
A platform-provided `PORT` activates production validation even if
`SECUREPR_ENV` was omitted.

Set `DASHBOARD_ORIGIN` to the canonical frontend HTTPS origin (used for
dashboard links) and `DASHBOARD_ORIGINS` to a comma-separated allowlist that
includes it. Development also permits common localhost origins; production
CORS uses only the configured origin list and rejects wildcard origins.

## 12. Render and Vercel deployment

The repository includes a Render Blueprint in `render.yaml` and a Vercel
static build configuration in `frontend/dashboard/vercel.json`. Connect the
Git repository to each provider and select the `render.yaml` Blueprint for
Render. For Vercel, set the project root to `frontend/dashboard`; the
configured build creates `dist/`.

Render starts the backend with `python run.py`, uses Python 3.12.4 (matching
the repository's Python 3.12 target), and checks `/health`. Set the
required backend environment values in Render; its `PORT` value is consumed
automatically. Set the following backend **public configuration**:

- `SECUREPR_ENV=production`
- `SUPABASE_AUTH_AUDIENCE`
- `SUPABASE_JWT_ALGORITHM`
- `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL`, and
  `GEMINI_REQUEST_TIMEOUT_SECONDS`
- `CI_WAIT_POLL_INTERVAL_SECONDS`, `CI_WAIT_TIMEOUT_SECONDS`,
  `CI_DISCOVERY_STABILIZATION_SECONDS`, `POST_CI_WAITING_COMMENT`
- `AUTO_MERGE_ENABLED`, `AUTO_MERGE_LOW_RISK_ONLY`,
  `AUTO_MERGE_REQUIRE_ALL_CI`
- `SECUREPR_BASE_URL` — the Render HTTPS origin
- `DASHBOARD_ORIGIN` — the canonical Vercel HTTPS origin
- `DASHBOARD_ORIGINS` — comma-separated HTTPS CORS origins, including the
  canonical origin

Enter these backend **secrets/private settings** in Render:
`DATABASE_URL`, `SUPABASE_URL`, `SECUREPR_OPERATOR_IDS`,
`SECUREPR_ADMIN_IDS`, `GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY`,
`GITHUB_WEBHOOK_SECRET`, and `GEMINI_API_KEY`. `SUPABASE_JWT_SECRET` is needed
only when using legacy HS256 JWT signing. `NOTIFY_WEBHOOK_URL` is optional.
Do not paste any of these values into this guide or a tracked file.

Set these Vercel **public build environment variables** for Production,
Preview, and Development as appropriate:

- `VITE_SUPABASE_URL`
- `VITE_SUPABASE_ANON_KEY` — public anon/publishable key only
- `VITE_SECUREPR_API_URL` — the Render backend HTTPS origin

These are read directly by the static Node build script; despite the
conventional `VITE_` prefix, this dashboard does not use Vite.

The backend's public URL is assigned by Render; the frontend's is assigned by
Vercel. Once known, set the Render `SECUREPR_BASE_URL`, `DASHBOARD_ORIGIN`,
and `DASHBOARD_ORIGINS`, then redeploy. Production validation intentionally
fails closed until all required origins and secrets are set. Finally update
the GitHub App webhook URL to
`https://<production-backend-domain>/webhooks/github` and remove the temporary
ngrok URL. Choose an always-on Render instance for dependable webhook
processing; a sleeping instance can delay webhook responses.

## 13. Health check

- `http://localhost:8000/health` → should return `{"status":"ok", ...}` locally.
- `http://localhost:8000/docs` → interactive Swagger UI listing every endpoint.
- Dashboard **Overview** tab → should load without errors (it will show zero repositories/reviews until a webhook fires — see `docs/TESTING_GUIDE.md`). You should never need to type a repository, PR, or review ID anywhere in the dashboard — everything is selected from dropdowns or by clicking a row; if a page is asking you for an ID, something upstream of it failed to load and is worth checking in the backend log.

## Troubleshooting

| Symptom                                                               | Likely cause                                                                                                                                                                                                                                                                                                                                          |
| --------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| PostgreSQL driver install fails                                       | Re-run `pip install -r requirements.txt` in the backend environment.                                                                                                                                                                                                                                                                                 |
| Database connection/schema errors                                    | Check that `DATABASE_URL` is a valid Supabase URL with URL-encoded password and `sslmode=require`, and that outbound network access to Supabase is available.                                                                                                                                                                                       |
| Webhook never fires                                                   | Confirm the GitHub App's webhook URL is reachable from the internet (`ngrok` for local tests, permanent HTTPS backend URL for production), and that the App is installed on the repository.                                                                                                                                                           |
| Gemini calls fail silently                                            | Check `GEMINI_API_KEY` is set; SecurePR falls back to heuristic-only scoring automatically and logs a warning — this is expected behavior, not a crash.                                                                                                                                                                                               |
| GitHub review/comment doesn't post                                    | Check the App's permissions include Pull requests: Read & write, and that the installation ID stored for the repo matches an active installation.                                                                                                                                                                                                     |
| A PR is stuck showing "Waiting for CI"                                | Confirm your GitHub App is subscribed to `Check run`/`Check suite`/`Status` events (section 7), inspect the CI lifecycle log fields (`event_type`, `repository`, `pr_number`, `head_sha`, `matched_current_revision`, `ignore_reason`), and check GitHub API availability. Polling is a fallback; the 15-minute timeout is the final manual-review path. |
| A configured mandatory check is still pending                         | Confirm the check name or explicit `ci_check_aliases` in the repository policy, then verify GitHub reports that check on the current PR head SHA. Missing mandatory checks are never treated as successful.                                                                                                                                       |
| Dashboard page asks you to type an ID                                 | Shouldn't happen — every page loads its own dropdown data from the backend. Check the browser console and backend log for the underlying failed request.                                                                                                                                                                                              |
| Dashboard shows "API error" toasts on every page                      | Usually CORS or a wrong API base URL — confirm the sidebar's "API base URL" field matches where `uvicorn` is running, and that `DASHBOARD_ORIGIN` in `.env` matches where you're serving the dashboard from.                                                                                                                                          |
| Port `8000` or `5173` already in use                                  | Start on another port, e.g. `uvicorn app.main:app --reload --port 8001` (update the dashboard's API base URL field to match), or `python -m http.server 5174`.                                                                                                                                                                                        |
