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
- `GROQ_API_KEY`, `GROQ_MODEL` — see section 8 (Groq setup).

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

URL-encode special characters in the password. `sslmode=require` enables TLS
for the Supabase connection. The numbered SQL files in `database/postgresql/`
are the legacy bootstrap history. The deployment migration is now
Alembic-managed:
`backend/migrations/versions/20261120_01_reconcile_ci_gate_schema.py` and
`20261120_02_ai_review_lifecycle.py` reconcile the CI and AI lifecycle
schemas, while `20261121_01_groq_review_metadata.py` records provider, model,
and token usage and adds fencing tokens to durable job leases. The migrations
preserve existing rows and are forward-only where rollback could discard
production history.

Render starts the backend through `python run.py`. In production, that wrapper
validates settings, acquires a bounded PostgreSQL migration advisory lock on a
dedicated non-pooled connection, runs `alembic upgrade head`, verifies the
revision, releases the lock on the same connection, then starts Uvicorn. A
failed or timed-out migration prevents the app from starting. Set Render's
`DATABASE_URL` to the intended production PostgreSQL URL; it is not printed.
Production startup does not call `create_all`; local development retains it
for disposable databases.

For a fresh database, initialize the tables with
`database/postgresql/001_initial_schema.sql` and the required legacy bootstrap
scripts first, then run `alembic upgrade head`. The existing
`database/securepr_schema.sql` remains the SQL Server fallback and must not be
run against PostgreSQL.

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
   - Pull requests: **Read & write** (to read PRs and publish reviews or request GitHub auto-merge)
   - Checks: **Read-only** (to read CI check results)
   - Commit statuses: **Read-only** (to read legacy CI provider results)
   - Issues: **Read & write** (to post and update the single PR status comment)
   - Administration: **Read-only** (to inspect branch protection and rulesets)
   - Metadata: **Read-only** (mandatory)
   - Do not grant any broader permissions than those listed. SecurePR does not request the immediate merge API or bypass branch protection.
7. **Subscribe to events:** `Pull request`, `Check run`, `Check suite`, and `Commit status`.
   CI events update the authoritative revision-specific decision; no final
   decision or approval is published until required checks are verified.
8. **Where can this GitHub App be installed:** "Only on this account" is fine for testing.
9. Click **Create GitHub App**. Note the **App ID** shown at the top → put it in `GITHUB_APP_ID`.
10. Scroll down → **Generate a private key** → a `.pem` file downloads. For local development, create a `backend/secrets/` folder, store the key there, and set `GITHUB_PRIVATE_KEY_PATH=./secrets/github-app-private-key.pem`. For Render, enter the PEM contents in the backend-only `GITHUB_PRIVATE_KEY` secret setting. Literal `\n` sequences are also normalized by the backend. Do not commit the key or put it in `GITHUB_PRIVATE_KEY_PATH` as a public URL.
11. **Install the App** on the repository you want SecurePR to analyze (Install App → choose repository).

## 8. Groq API configuration

1. Create a backend-only API key in the Groq Console.
2. Set `GROQ_API_KEY` in `.env`.
3. Set `GROQ_MODEL` to a supported model available to your API key. The
   initial model is `openai/gpt-oss-120b`; Groq currently lists it as a
   production model with strict JSON-schema structured-output support.
   SecurePR uses only that configured model and does not silently substitute
   another model. A missing model, authentication failure, quota exhaustion,
   provider outage, timeout, or invalid response leaves the AI review
   unavailable; no risk score or approval is produced.
4. Short transient rate limits and network errors use bounded retries.
   Longer `Retry-After` delays are queued for later; daily quota exhaustion is
   not repeatedly retried.

## 9. CI-gated review lifecycle

The PR webhook stores the current head SHA and queues database-backed review,
status-comment, and CI-refresh jobs. SecurePR promptly posts one
`SecurePR Review — Waiting for CI` comment and updates that same comment as
verification changes. Risk analysis may run while CI is pending, but its
score is informational: no final approval or manual-review decision is
published until CI is verified. A failed execution blocks approval regardless
of risk. Missing, empty, incomplete, unavailable, unknown, pending, skipped,
neutral, cancelled, or stale CI is never considered successful. Once CI passes,
the deterministic policy engine evaluates risk and eligibility.

CI discovery is tied to the exact repository, PR, and head SHA. SecurePR
aggregates Check Runs and the latest Commit Status for each context; Check
Suites are not executable checks. It also discovers required contexts from
branch protection and applicable rulesets. Discovery errors, incomplete
pagination, and missing required checks remain non-approvable. Duplicate
webhooks, checks, and retries are reconciled through revision-scoped durable
jobs and publication markers. A new commit invalidates the previous
revision's checks and decision; a stale job cannot publish against it.

| CI state | Meaning |
|---|---|
| `UNKNOWN` | Initial discovery is incomplete or returned an unrecognized result. |
| `UNAVAILABLE` | GitHub discovery failed; the error is preserved and retry continues with capped backoff. |
| `NOT_CONFIGURED` | Successful discovery found no check runs or commit statuses. This is not a pass. |
| `IN_PROGRESS` | A real execution is queued/running or a required context has not appeared. No final decision is published. |
| `PASSED` | Discovery is complete, at least one execution exists, every execution passed, and every required context passed. |
| `FAILED` | A discovered execution ended with failure, error, timeout, startup failure, or action required. Approval is blocked. |
| `NON_SUCCESS` | All checks are terminal, but one or more were cancelled, neutral, skipped, or stale. This is not a pass. |

CI API discovery has a 10-second aggregate request deadline. Failures retry
asynchronously with capped exponential backoff, and nonterminal/unknown states
continue polling at a capped interval until GitHub verification succeeds or
the PR revision changes. `REVIEW_RESULT_DEADLINE_SECONDS` is an operational
target, not permission to skip CI verification.

CI failure blocks regardless of `block_on_ci_failure`; that legacy policy
field cannot override the hard gate. A CI transition reconciles the decision
for the same revision. If a previously published SecurePR approval becomes
invalid, SecurePR attempts to dismiss that GitHub review and publishes a
corrective blocked result. GitHub may not allow dismissal in every review
state, so any dismissal failure is recorded and shown.

Configure `GITHUB_WEBHOOK_SECRET` and subscribe the GitHub App to `Pull
request`, `Check run`, `Check suite`, and `Commit status` events. Database
migrations preserve lifecycle and publication state across process restarts.

## 10. Safe auto-approval and auto-merge

The dashboard's saved policy for one GitHub account and repository is the sole
user authorization for automatic approval and GitHub auto-merge. Select a
known PR author, repository, maximum risk threshold (0-100), and enabled
state, then save. Unsaved form values have no effect. No environment variable,
repository toggle, or policy-file category can enable this behavior.

The backend reloads the persisted row for the exact repository and PR author
for every decision. Missing, disabled, inconsistent, or out-of-range settings
fail closed. AI analysis must be validated and complete; the reviewed SHA
must still be current; all discovered and required CI checks must pass; and
GitHub must confirm permissions, branch protection, required reviews, merge
method, and repository auto-merge settings. The Circuit Breaker remains an
emergency stop.

The `AUTO_MERGE_METHOD` setting selects the operational GitHub method only; it
does not authorize a merge. The GitHub App needs Pull requests: Read & write,
Checks: Read-only, Commit statuses: Read-only, Issues: Read & write,
Administration: Read-only, and Metadata: Read-only. Update the GitHub App
installation after changing permissions.

For auto-merge, configure the target branch with at least one required
pull-request approval and enable **Dismiss stale pull request approvals when
new commits are pushed** (classic branch protection) or its equivalent
`pull_request` ruleset settings. SecurePR fails closed if it cannot verify
these settings. This prevents a previously approved revision from satisfying
branch protection after a push.

Before requesting GitHub auto-merge, SecurePR refetches the PR, exact-SHA
checks, branch requirements, GitHub auto-merge setting, supported merge
methods, PR draft/state, required-review status, and permissions. Required
reviews and checks remain enforced by GitHub; if they are outstanding, SecurePR
does not bypass them. On pushes it cancels any prior auto-merge request before
queueing a new revision, and it verifies the head again after enabling
auto-merge, cancelling the request if the revision changed. It calls GitHub's GraphQL
`enablePullRequestAutoMerge` mutation and records success only when GitHub
confirms an enabled auto-merge request. This is not a confirmation that the PR
has merged; only a GitHub merge event records `MERGED`.

The dashboard shows the saved account, repository, threshold, and enabled
state. Every attempt and its response are stored in `github_actions` and
review evidence. Failures are reported accurately; SecurePR never claims a
merge without GitHub confirmation.

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
- `GROQ_MODEL` (defaults to `openai/gpt-oss-120b`)
- `REVIEW_RESULT_DEADLINE_SECONDS`
- `CI_API_MAX_RETRIES`, `CI_FALLBACK_MAX_POLLS`,
  `CI_FALLBACK_POLL_INTERVAL_SECONDS`
- `AUTO_MERGE_METHOD` (defaults to `SQUASH`; does not authorize auto-merge)
- `SECUREPR_BASE_URL` — the Render HTTPS origin
- `DASHBOARD_ORIGIN` — the canonical Vercel HTTPS origin
- `DASHBOARD_ORIGINS` — comma-separated HTTPS CORS origins, including the
  canonical origin

Enter these backend **secrets/private settings** in Render:
`DATABASE_URL`, `SUPABASE_URL`, `SECUREPR_OPERATOR_IDS`,
`SECUREPR_ADMIN_IDS`, `GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY`,
`GITHUB_WEBHOOK_SECRET`, and `GROQ_API_KEY`. `SUPABASE_JWT_SECRET` is needed
only when using legacy HS256 JWT signing. `NOTIFY_WEBHOOK_URL` is optional.
Do not paste any of these values into this guide or a tracked file.

Before upgrading an existing deployment, disable saved dashboard auto-approval
and cancel every outstanding GitHub auto-merge request for installed
repositories. A request already accepted by GitHub can outlive a backend
deployment, so do not re-enable dashboard configurations until the migration,
Groq verification, branch-protection checks, and controlled concurrency tests
have passed.

For an existing Gemini deployment, add `GROQ_API_KEY` as a secret on the
Render backend service and set `GROQ_MODEL` there (the initial supported
choice is `openai/gpt-oss-120b`). Remove `GEMINI_API_KEY`, `GEMINI_MODEL`,
`GEMINI_REQUEST_TIMEOUT_SECONDS`, and `GEMINI_MAX_ATTEMPTS`. Also remove
`AUTO_MERGE_ENABLED` and `AUTO_MERGE_LOW_RISK_ONLY`; neither is read or needed.
Keep `AUTO_MERGE_METHOD` only as an operational preference. Never add
`GROQ_API_KEY` to Vercel or any other frontend environment.

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
| Groq analysis is unavailable                                         | Check the `AI Review Status` and safe error category in the dashboard, then verify `GROQ_API_KEY`, quota, and the configured `GROQ_MODEL`. SecurePR does not substitute heuristic-only scores or approve without validated AI output.                                                                                                                                 |
| GitHub review/comment doesn't post                                    | Check the App's permissions include Pull requests: Read & write, and that the installation ID stored for the repo matches an active installation.                                                                                                                                                                                                     |
| AI review remains queued or processing                                 | Check the `background_jobs` row for the revision, `last_error`, attempt count, and worker logs. A final decision is intentionally withheld until CI verification succeeds or a failure is confirmed.                                                                      |
| CI state is unavailable                                                | Check the stored `ci_revisions.error`, GitHub App Check/Status/Administration permissions, rate-limit responses, and webhook delivery history. Retries continue with capped backoff; no approval is published while verification is unavailable.                       |
| CI state remains unknown                                               | Verify the CI refresh job is queued and that its repository, PR number, and head SHA match the current revision. An unknown or empty result is never displayed as passed.                                                                                                  |
| Dashboard page asks you to type an ID                                 | Shouldn't happen — every page loads its own dropdown data from the backend. Check the browser console and backend log for the underlying failed request.                                                                                                                                                                                              |
| Dashboard shows "API error" toasts on every page                      | Usually CORS or a wrong API base URL — confirm the sidebar's "API base URL" field matches where `uvicorn` is running, and that `DASHBOARD_ORIGIN` in `.env` matches where you're serving the dashboard from.                                                                                                                                          |
| Port `8000` or `5173` already in use                                  | Start on another port, e.g. `uvicorn app.main:app --reload --port 8001` (update the dashboard's API base URL field to match), or `python -m http.server 5174`.                                                                                                                                                                                        |
