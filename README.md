# SecurePR

AI-powered Pull Request risk assessment and automatic-approval platform.
SecurePR analyzes GitHub Pull Requests, scores risk across six weighted
dimensions, evaluates configurable policies, and publishes the AI review
only after executable CI for the exact revision is verified. GitHub auto-merge
is requested only through GitHub's protected auto-merge flow, is opt-in, and
is disabled by default. A dark,
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
│   │   └── integrations/       GitHub App client, Groq client
│   ├── tests/                 pytest unit tests
│   ├── run.py                  cloud-platform entry point (uses PORT)
│   ├── requirements.txt
│   ├── requirements-dev.txt   QA tools
│   └── .env.example
├── database/
│   ├── securepr_schema.sql  retained SQL Server fallback schema (run in SSMS)
│   └── postgresql/
│       ├── 001_initial_schema.sql  Supabase PostgreSQL bootstrap schema
│       ├── 002_independent_ci_monitoring.sql  durable review/CI workers
│       ├── 003_review_publications.sql
│       └── 004_ci_gated_decisions.sql
├── policies/
│   └── default_policy.json   initial policy document (no code change needed to edit)
├── frontend/dashboard/        static HTML/CSS/JS dashboard + Vercel build config
├── .github/workflows/         backend, dashboard, dependency and secret checks
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

- **AI review is a required decision prerequisite.** Groq must return
  validated risk dimensions, explanations, and findings for the exact PR head
  SHA. The deterministic decision engine applies CI and repository policy to
  that completed analysis. If AI fails or returns invalid data, SecurePR
  records `REVIEW_UNAVAILABLE`, reports risk as not evaluated, and cannot
  approve or auto-merge.
- **CI gates every final decision.** A pull-request webhook queues durable
  review, status-comment, and CI-discovery jobs. SecurePR updates one
  `Waiting for CI` comment while checks are pending. CI failure blocks
  approval regardless of risk; unknown, unavailable, incomplete, empty, and
  non-success check results cannot approve. Only after every discovered
  execution and applicable required status check for the exact head SHA passes
  and AI analysis are valid for the exact head SHA does the policy engine
  evaluate risk and eligibility. See
  [`docs/CI_DECISION_LIFECYCLE.md`](docs/CI_DECISION_LIFECYCLE.md).
- **Groq model selection is explicit.** SecurePR uses only the configured
  `GROQ_MODEL`; an unavailable model or provider failure produces an
  unavailable review instead of silently switching models or substituting
  deterministic scores.
- **Guarded GitHub auto-merge.** Only a saved dashboard policy for the exact
  GitHub account and repository can authorize it. Before requesting GitHub auto-merge,
  SecurePR rechecks the reviewed commit, CI, branch protection, required
  reviews, GitHub settings, and permissions. SecurePR never performs an
  immediate or force merge. See
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
  - Review and CI tasks use a database-backed queue, so queued work survives
    process restarts. Deployments must apply all required numbered migrations,
    including `004_ci_gated_decisions.sql`, before deploying this version.
  - GitHub webhooks remain the primary CI update mechanism; configure Check
    run, Check suite, Commit status, and pull-request events on the GitHub App.
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
pip install -r requirements-dev.txt
pytest -q
ruff check --select E9,F63,F7,F82 app tests
mypy --ignore-missing-imports --follow-imports=skip app/services/ci_status.py app/services/comment_formatter.py app/integrations/groq_client.py
pip-audit -r requirements.txt
```

## License

Provided as-is for your own use; no license restrictions imposed by this
deliverable.
