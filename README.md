# SecurePR

AI-powered Pull Request risk assessment and automatic-approval platform.
SecurePR analyzes GitHub Pull Requests, scores risk across six weighted
dimensions, evaluates configurable policies, waits for your CI to actually
finish before deciding, simulates change impact, and either approves,
blocks, or routes eligible low-risk PRs for manual review. A separately
guarded auto-merge flow is opt-in and disabled by default. A dark,
dropdown-driven dashboard (no internal
IDs to copy/paste) covers every stage of that pipeline end to end.

## Start here

1. **`docs/SETUP_GUIDE.md`** — configure the Supabase PostgreSQL connection,
   start the backend and dashboard. Start here.
2. **`docs/TESTING_GUIDE.md`** — a step-by-step real-world test plan using
   the included `sample-repo/`, covering low/medium/high-risk PRs, blocked
   paths, failing CI, and all three differentiators.
3. **`docs/Policies.md`** — how the policy engine and default policy work.

## Project layout

```
SecurePR/
├── backend/                 FastAPI app (Python)
│   ├── app/
│   │   ├── api/              webhook + dashboard REST endpoints
│   │   ├── core/              config, DB session
│   │   ├── models/            SQLAlchemy ORM models
│   │   ├── schemas/            Pydantic request/response schemas
│   │   ├── services/           risk engine, policy engine, circuit breaker,
│   │   │                       impact simulator, decision engine, pipeline
│   │   └── integrations/       GitHub App client, Gemini client
│   ├── tests/                 pytest unit tests
│   ├── run.py                  cloud-platform entry point (uses PORT)
│   ├── requirements.txt
│   └── .env.example
├── database/
│   ├── securepr_schema.sql  retained SQL Server fallback schema (run in SSMS)
│   └── postgresql/
│       └── 001_initial_schema.sql  Supabase PostgreSQL bootstrap schema
├── policies/
│   └── default_policy.json   initial policy document (no code change needed to edit)
├── frontend/dashboard/        static HTML/CSS/JS dashboard + Vercel build config
├── sample-repo/                minimal app + CI workflow for real-world testing
└── docs/
    ├── SETUP_GUIDE.md
    ├── TESTING_GUIDE.md
    └── Policies.md
```

## The three differentiators

1. **Circuit Breaker** — suspend automatic approval globally, per repository,
   or per user (with optional timed auto-resume), without stopping analysis
   or GitHub comments.
2. **Evidence-Backed Risk Assessment & Decision Replay** — every analysis run
   is stored as an immutable `Review` row with its own findings, evidence,
   changed files, and GitHub action log. Re-evaluating a PR never overwrites
   history; it links a new review to the one it supersedes.
3. **Change Impact Simulator** — classifies predicted blast radius
   (functions, APIs, modules, database ops, dependencies, tests) as
   CONFIRMED, INFERRED, or UNRESOLVED, with the evidence behind each call.

## Design notes worth knowing before you run this

- **The AI never decides.** Gemini contributes dimension-score opinions and
  rationale text; `services/decision_engine.py` is pure deterministic Python
  with no AI dependency, and it is the only thing that produces an
  APPROVED/BLOCKED/MANUAL_REVIEW/WAITING_FOR_CI decision. AI scores can only
  raise a heuristic risk score, never lower one (`risk_engine.blend_with_ai`).
- **SecurePR discovers CI dynamically.** It discovers check runs, commit
  statuses, and check suites for each PR head SHA without assuming check
  names. Webhooks and polling share a stabilization-aware evaluator; pending
  checks, late-arriving checks, and API errors cannot be mistaken for success.
  Repositories without CI and unavailable CI are surfaced explicitly and
  routed to manual review. Optional repository policies can still require
  named checks. See `docs/Policies.md` and `docs/SETUP_GUIDE.md` section 9.
- **Gemini model fallback.** `gemini-3.8-flash` is configured by default, but
  `integrations/gemini_client.py` probes model availability at call time and
  falls back to `GEMINI_FALLBACK_MODEL` (default `gemini-2.0-flash`) if it
  isn't available on your account — analysis continues either way.
- **Guarded auto-merge.** It is disabled globally by default and requires
  per-repository opt-in. Before merging, SecurePR rechecks the reviewed
  commit, PR state, CI, policy, risk, and circuit breaker. See
  `docs/SETUP_GUIDE.md` section 10 before enabling it.
- **The dashboard never asks you to type an internal ID.** Every page
  (Repositories & PRs, Circuit Breaker, Evidence & Replay, Change Impact,
  Auto-Approval) is driven by dropdowns populated from the backend and
  clickable rows that carry IDs as JS state — not text fields. Internal
  UUIDs still exist and are visible in `<code>` tags for anyone who needs
  them, but normal usage never requires copying one.
- **This is a working, actively-tested MVP, not a hardened production
  system.** Known limitations worth addressing before a real deployment:
  - The dashboard/API now uses Supabase Auth with backend-validated JWTs and
    operator/admin user-ID allowlists. Set those allowlists and production
    security configuration before deployment; production startup fails when
    critical settings are missing.
  - The CI-wait poller is an in-process Python thread (`services/pipeline.py`
    `_ci_wait_poll_loop`), not a durable job queue — it won't survive a
    backend restart mid-wait (the webhook fast path and the next `uvicorn
    --reload` will generally re-trigger it if a later event arrives, but
    there's no guaranteed resume). A production deployment should move this
    to Celery/RQ/a scheduled job or similar.
  - No rate limiting or retry/backoff on GitHub or Gemini calls.
  - GitHub App keys may be loaded from a local `.pem` file for development;
    production should set the backend-only `GITHUB_PRIVATE_KEY` secret.
  - Use provider secret settings for all production credentials; do not copy
    local `.env` contents into tracked files.
  - GitHub webhook requests are rejected if `GITHUB_WEBHOOK_SECRET` is unset,
    and automatic merging is disabled by default.

## Production deployment

The current dashboard is static HTML/CSS/JavaScript (there is no React/Vite
application in this workspace). It has a dependency-free production build for
Vercel. The backend has a Render Blueprint. See
[`docs/SETUP_GUIDE.md`](docs/SETUP_GUIDE.md) for provider configuration,
public frontend build variables, backend secrets, and the permanent GitHub
webhook URL format.

## Running tests

```powershell
cd backend
venv\Scripts\activate
pytest
```

## License

Provided as-is for your own use; no license restrictions imposed by this
deliverable.
