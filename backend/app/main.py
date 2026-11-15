import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import dashboard, webhooks
from app.core.config import (
    get_dashboard_origins,
    get_settings,
    is_production_runtime,
    validate_production_settings,
)
from app.core.db import Base, get_engine
from app.models import models  # noqa: F401 ensures models are registered on Base.metadata

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("securepr")

settings = get_settings()
production_runtime = is_production_runtime(settings)

app = FastAPI(title="SecurePR", version="1.0.0",
              description="AI-powered Pull Request risk assessment and automatic approval platform.",
              docs_url=None if production_runtime else "/docs",
              redoc_url=None if production_runtime else "/redoc",
              openapi_url=None if production_runtime else "/openapi.json")

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_dashboard_origins(settings),
    allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
)

app.include_router(webhooks.router)
app.include_router(dashboard.router)


@app.on_event("startup")
def on_startup():
    validate_production_settings(settings)
    if not settings.GITHUB_WEBHOOK_SECRET:
        logger.warning("GITHUB_WEBHOOK_SECRET is not configured; webhook requests will be rejected.")
    # Create missing tables from the ORM models. The SQL Server schema remains
    # available for existing local installations and is not used for PostgreSQL.
    Base.metadata.create_all(bind=get_engine())
    logger.info("Database tables verified/created.")


@app.get("/health")
def health():
    return {"status": "ok", "service": "SecurePR", "env": settings.SECUREPR_ENV}


@app.get("/")
def root():
    return {"message": "SecurePR API is running."}
