"""Cloud entry point; migrate production PostgreSQL before accepting traffic."""

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.core.config import (
    get_settings,
    is_production_runtime,
    validate_production_settings,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("securepr.startup")
logger.setLevel(logging.INFO)
_MIGRATION_LOCK_ID = 73102026112003
_MIGRATION_LOCK_WAIT_SECONDS = 60


@contextmanager
def _production_migration_lock(connection) -> Iterator[None]:
    acquired = False
    acquisition_result = "error"
    started = time.monotonic()
    deadline = started + _MIGRATION_LOCK_WAIT_SECONDS
    backoff = 0.25
    retry_count = 0
    try:
        while True:
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:lock_id)"),
                    {"lock_id": _MIGRATION_LOCK_ID},
                ).scalar_one()
            )
            connection.commit()
            if acquired:
                acquisition_result = "acquired"
                break
            retry_count += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                acquisition_result = "timeout"
                raise TimeoutError(
                    "Timed out waiting for the production migration lock."
                )
            time.sleep(min(backoff, remaining))
            backoff = min(backoff * 2, 5)
        logger.info(
            "Database migration lock acquired: lock_id=%s wait_duration_ms=%s "
            "acquisition_result=acquired retry_count=%s",
            _MIGRATION_LOCK_ID,
            round((time.monotonic() - started) * 1000),
            retry_count,
            extra={
                "lock_id": _MIGRATION_LOCK_ID,
                "wait_duration_ms": round((time.monotonic() - started) * 1000),
                "acquisition_result": "acquired",
                "retry_count": retry_count,
            },
        )
        yield
    except Exception:
        if connection.in_transaction():
            connection.rollback()
        logger.exception(
            "Database migration lock operation failed: lock_id=%s "
            "wait_duration_ms=%s acquisition_result=%s retry_count=%s",
            _MIGRATION_LOCK_ID,
            round((time.monotonic() - started) * 1000),
            acquisition_result,
            retry_count,
            extra={
                "lock_id": _MIGRATION_LOCK_ID,
                "wait_duration_ms": round((time.monotonic() - started) * 1000),
                "acquisition_result": acquisition_result,
                "retry_count": retry_count,
            },
        )
        raise
    finally:
        if acquired:
            try:
                if connection.in_transaction():
                    connection.rollback()
                unlocked = connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": _MIGRATION_LOCK_ID},
                ).scalar_one()
                connection.commit()
                if not unlocked:
                    raise RuntimeError(
                        "PostgreSQL reported that this connection did not own the migration lock."
                    )
                logger.info(
                    "Database migration lock released: lock_id=%s "
                    "wait_duration_ms=%s release_result=released retry_count=%s",
                    _MIGRATION_LOCK_ID,
                    round((time.monotonic() - started) * 1000),
                    retry_count,
                    extra={
                        "lock_id": _MIGRATION_LOCK_ID,
                        "wait_duration_ms": round((time.monotonic() - started) * 1000),
                        "release_result": "released",
                        "retry_count": retry_count,
                    },
                )
            except Exception:
                connection.invalidate()
                logger.exception(
                    "Database migration lock release failed: lock_id=%s "
                    "wait_duration_ms=%s release_result=connection_invalidated "
                    "retry_count=%s",
                    _MIGRATION_LOCK_ID,
                    round((time.monotonic() - started) * 1000),
                    retry_count,
                    extra={
                        "lock_id": _MIGRATION_LOCK_ID,
                        "wait_duration_ms": round((time.monotonic() - started) * 1000),
                        "release_result": "connection_invalidated",
                        "retry_count": retry_count,
                    },
                )
                raise


def _upgrade_production_schema() -> None:
    settings = get_settings()
    validate_production_settings(settings)
    engine = create_engine(
        settings.DATABASE_URL,
        poolclass=NullPool,
        pool_pre_ping=True,
        future=True,
    )
    try:
        with engine.connect() as connection, _production_migration_lock(connection):
            config = Config(str(Path(__file__).resolve().parent / "alembic.ini"))
            config.attributes["connection"] = connection
            expected_revision = ScriptDirectory.from_config(config).get_current_head()
            current_revision = MigrationContext.configure(
                connection
            ).get_current_revision()
            connection.commit()
            logger.info(
                "Starting database migration: current_revision=%s target_revision=%s",
                current_revision or "base",
                expected_revision,
            )
            command.upgrade(config, "head")
            connection.commit()
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
    finally:
        engine.dispose()


def main() -> None:
    settings = get_settings()
    if is_production_runtime(settings):
        _upgrade_production_schema()
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
