"""Cloud entry point; migrate production PostgreSQL before accepting traffic."""

import logging
import os
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.core.config import (
    get_settings,
    is_production_runtime,
    validate_production_settings,
)
from app.core.db import get_engine

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("securepr.startup")
logger.setLevel(logging.INFO)
_MIGRATION_LOCK_ID = 73102026112003


def _upgrade_production_schema() -> None:
    settings = get_settings()
    validate_production_settings(settings)
    engine = get_engine()
    with engine.connect() as connection:
        connection.execute(
            text("SELECT pg_advisory_lock(:lock_id)"),
            {"lock_id": _MIGRATION_LOCK_ID},
        )
        connection.commit()
        try:
            config = Config(str(Path(__file__).resolve().parent / "alembic.ini"))
            config.attributes["connection"] = connection
            expected_revision = ScriptDirectory.from_config(config).get_current_head()
            current_revision = MigrationContext.configure(
                connection
            ).get_current_revision()
            logger.info(
                "Starting database migration: current_revision=%s target_revision=%s",
                current_revision or "base",
                expected_revision,
            )
            command.upgrade(config, "head")
            current_revision = MigrationContext.configure(
                connection
            ).get_current_revision()
            if current_revision != expected_revision:
                raise RuntimeError(
                    "Database migration verification failed: "
                    f"expected {expected_revision!r}, found {current_revision!r}."
                )
            logger.info(
                "Database migration complete: current_revision=%s",
                current_revision,
            )
        except Exception:
            if connection.in_transaction():
                connection.rollback()
            logger.exception("Database migration failed; the API will not start.")
            raise
        finally:
            if connection.in_transaction():
                connection.rollback()
            unlocked = connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": _MIGRATION_LOCK_ID},
            ).scalar_one()
            connection.commit()
            if not unlocked:
                raise RuntimeError("Could not release the database migration lock.")


def main() -> None:
    settings = get_settings()
    if is_production_runtime(settings):
        _upgrade_production_schema()
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
