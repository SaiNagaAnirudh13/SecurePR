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

URL-encode special characters in the password. `sslmode=require` enables TLS for the Supabase connection. On first startup, SQLAlchemy creates missing tables, constraints, and indexes from `backend/app/models/models.py`. For existing PostgreSQL databases, apply `database/postgresql/002_independent_ci_monitoring.sql` before deploying the independent workers; it creates durable job, CI revision, and CI execution tables without dropping data. The reference schema `database/postgresql/001_initial_schema.sql` also includes those tables for new installations. The existing `database/securepr_schema.sql` remains the SQL Server fallback and must not be run against PostgreSQL.

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
   CI events update the separate background monitor; the AI review is published
   independently and never waits for these events.
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

## 9. Independent AI review and GitHub CI monitoring

The PR webhook validates and stores the current head SHA, then enqueues
database-backed AI-review, initial-status-comment, and CI-refresh jobs. The
status worker promptly posts `SecurePR Review — Waiting for CI` without
waiting for a CI timeout. It reports only actual checks already discovered and
updates the same comment as new CI or analysis results become available. The
review worker fetches the PR changes, analyzes risk, applies review policy,
and publishes findings as soon as analysis completes. It never waits for CI
discovery or completion. A queued or in-progress check, no configured CI, an
unfamiliar check name, and a GitHub CI API error cannot hold the AI review.
CI webhooks and polling update CI state and the status comment without rerunning
AI analysis. The dashboard reports AI review lifecycle separately from CI.

CI discovery is associated with the exact repository, PR, and head SHA.
SecurePR reads actual Check Runs, Check Suites, and legacy commit statuses;
it stores provider, name/context, status, conclusion, URL, and SHA for each
record. Check suites alone do not count as active check executions.
`check_run`, `check_suite`, and `status` webhooks enqueue idempotent CI refresh
jobs. Polling is only a bounded fallback when actual executions remain in
progress.

| CI state | Meaning |
|---|---|
| `UNKNOWN` | Initial discovery has not completed or returned an unrecognized result. |
| `UNAVAILABLE` | Discovery API failed; the error is preserved and bounded retries continue. |
| `NOT_CONFIGURED` | Successful discovery found no check runs or commit statuses. This is not a pass. |
| `IN_PROGRESS` | At least one real execution is queued or running; the AI review can already be published. |
| `PASSED` | Every discovered execution passed. |
| `FAILED` | A discovered execution ended with failure, error, timeout, startup failure, or action required. |
| `NON_SUCCESS` | All checks are terminal, but one or more were cancelled, neutral, skipped, or stale. This is not a pass. |

CI API discovery has a 10-second aggregate deadline. Failures are retried
asynchronously with bounded exponential backoff; background polling is
limited by `CI_FALLBACK_MAX_POLLS`. `REVIEW_RESULT_DEADLINE_SECONDS` defaults
to 180 seconds and is used as the operational review target, not as a wait for
CI. If analysis is still executing, the persistent review job remains
`PROCESSING`; CI state remains separately visible.

The default policy has no named mandatory-check list or aliases.
`block_on_ci_failure` controls whether a genuine failure already discovered
at analysis time blocks the review. A later failure updates the status comment
and CI state and posts a separate failure notification with a link to the
actual execution; it never reruns or duplicates the AI review. Missing,
unknown, and unavailable CI are not treated as either pass or failure.

Database-backed jobs survive backend process restarts. Apply
`database/postgresql/002_independent_ci_monitoring.sql` before deploying to an
existing PostgreSQL database. The SQL Server fallback schema is safe to rerun
and creates the same new tables. Configure `GITHUB_WEBHOOK_SECRET` and
subscribe the GitHub App to `Pull request`, `Check run`, `Check suite`, and
`Commit status` events. Webhook delivery IDs and revision-specific database
keys make duplicate events idempotent. Review publication includes a
revision-specific marker and rechecks GitHub before posting so a worker retry
after a crash does not duplicate the review.

## 10. Safe auto-merge

Auto-merge is off by default and runs only after the AI analysis produces `APPROVED`.

The repository policy's `auto_merge_allowed_risk_categories` defaults to
`["LOW"]`; MEDIUM/HIGH categories require explicit inclusion in that
repository's policy. `AUTO_MERGE_LOW_RISK_ONLY=true` remains a global cap
that a repository policy cannot override. At least one discovered CI
execution must exist and every discovered execution must be `PASSED` before
auto-merge; no-check, pending, failed, non-success, unknown, and unavailable
states fail closed. No check names or aliases are hardcoded.

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
the repository's Python 3.12 target), and checks `/health`. Apply the database
migration described in section 5 before deploying the independent workers.
Set the
required backend environment values in Render; its `PORT` value is consumed
automatically. Set the following backend **public configuration**:

- `SECUREPR_ENV=production`
- `SUPABASE_AUTH_AUDIENCE`
- `SUPABASE_JWT_ALGORITHM`
- `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL`, and
  `GEMINI_REQUEST_TIMEOUT_SECONDS`
- `REVIEW_RESULT_DEADLINE_SECONDS`
- `CI_API_MAX_RETRIES`, `CI_FALLBACK_MAX_POLLS`,
  `CI_FALLBACK_POLL_INTERVAL_SECONDS`
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
| AI review remains queued or processing                                 | Check the `background_jobs` row for the revision, `last_error`, attempt count, and worker logs. The CI monitor is a separate job and cannot block this review.                                                                                                         |
| CI state is unavailable                                                | Check the stored `ci_revisions.error`, GitHub App Check/Status permissions, rate-limit responses, and webhook delivery history. Bounded retries continue independently of the AI review.                                                                                |
| CI state remains unknown                                               | Verify the CI refresh job is queued and that its repository, PR number, and head SHA match the current revision. An unknown or empty result is never displayed as passed.                                                                                                  |
| Dashboard page asks you to type an ID                                 | Shouldn't happen — every page loads its own dropdown data from the backend. Check the browser console and backend log for the underlying failed request.                                                                                                                                                                                              |
| Dashboard shows "API error" toasts on every page                      | Usually CORS or a wrong API base URL — confirm the sidebar's "API base URL" field matches where `uvicorn` is running, and that `DASHBOARD_ORIGIN` in `.env` matches where you're serving the dashboard from.                                                                                                                                          |
| Port `8000` or `5173` already in use                                  | Start on another port, e.g. `uvicorn app.main:app --reload --port 8001` (update the dashboard's API base URL field to match), or `python -m http.server 5174`.                                                                                                                                                                                        |
