import logging
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api import dashboard, webhooks
from app.core.config import (
    get_dashboard_origins,
    get_settings,
    is_production_runtime,
    validate_production_settings,
)
from app.core.db import Base, get_engine
from app.models import models  # noqa: F401 ensures models are registered on Base.metadata
from app.services.background_jobs import (
    background_worker_status,
    start_background_workers,
    stop_background_workers,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("securepr")

settings = get_settings()
production_runtime = is_production_runtime(settings)
migration_status = "not_checked"

app = FastAPI(
    title="SecurePR",
    version="1.0.0",
    description="AI-powered Pull Request risk assessment and automatic approval platform.",
    docs_url=None if production_runtime else "/docs",
    redoc_url=None if production_runtime else "/redoc",
    openapi_url=None if production_runtime else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_dashboard_origins(settings),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def disable_dashboard_api_caching(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.include_router(webhooks.router)
app.include_router(dashboard.router)


@app.on_event("startup")
def on_startup():
    global migration_status
    validate_production_settings(settings)
    if not settings.GITHUB_WEBHOOK_SECRET:
        logger.warning(
            "GITHUB_WEBHOOK_SECRET is not configured; webhook requests will be rejected."
        )
    if production_runtime:
        alembic_config = Config(
            str(Path(__file__).resolve().parents[1] / "alembic.ini")
        )
        expected_revision = ScriptDirectory.from_config(
            alembic_config
        ).get_current_head()
        with get_engine().connect() as connection:
            current_revision = MigrationContext.configure(
                connection
            ).get_current_revision()
        if current_revision != expected_revision:
            raise RuntimeError(
                "Production database migrations are not current; "
                "run `alembic upgrade head` before starting the application."
            )
        migration_status = "current"
    else:
        # Local development can create new databases; production schema changes
        # are exclusively applied by the Render pre-deploy migration command.
        Base.metadata.create_all(bind=get_engine())
        migration_status = "local_schema_created"
    start_background_workers()
    logger.info("Database schema verified.")


@app.on_event("shutdown")
def on_shutdown():
    stop_background_workers()


@app.get("/health")
def health():
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.exception("Health check failed because the database is unavailable.")
        raise HTTPException(
            status_code=503,
            detail="Database is unavailable.",
        ) from exc
    workers = background_worker_status()
    if not all(workers.values()):
        logger.error(
            "Health check failed because one or more durable workers are not alive: %s",
            workers,
            extra={"worker_status": workers, "health_check_failed": True},
        )
        raise HTTPException(
            status_code=503,
            detail="One or more durable background workers are unavailable.",
        )
    return {
        "status": "ok",
        "service": "SecurePR",
        "env": settings.SECUREPR_ENV,
        "workers": workers,
        "dependencies": {
            "database": "available",
            "migrations": migration_status,
            "github_app": (
                "configured"
                if settings.GITHUB_APP_ID
                and settings.GITHUB_WEBHOOK_SECRET
                and (
                    settings.GITHUB_PRIVATE_KEY
                    or Path(settings.GITHUB_PRIVATE_KEY_PATH).is_file()
                )
                else "missing_configuration"
            ),
            "gemini": {
                "configuration": (
                    "configured"
                    if settings.GEMINI_API_KEY and settings.GEMINI_MODEL
                    else "missing_configuration"
                ),
                "model": settings.GEMINI_MODEL,
                "reachability": "not_checked",
            },
        },
    }


@app.get("/")
def root():
    return {"message": "SecurePR API is running."}
