import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.models import models as m

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECISION_STATES = tuple(state.value for state in m.DecisionStatus)
LIFECYCLE_STATES = {
    "RECEIVED",
    "ANALYSIS_FAILED",
    "WAITING_FOR_AI",
    "WAITING_FOR_CI",
    "APPROVED",
    "BLOCKED",
    "MANUAL_REVIEW",
    "REVIEW_UNAVAILABLE",
    "PENDING",
}


@pytest.fixture()
def postgres_lifecycle_migration():
    database_url = os.environ.get("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip(
            "PostgreSQL migration regression not run: TEST_POSTGRES_URL is not configured."
        )

    url = make_url(database_url)
    if url.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("TEST_POSTGRES_URL must point to a local PostgreSQL test server.")

    schema = f"securepr_ai_migration_test_{uuid.uuid4().hex}"
    admin_engine = create_engine(url, future=True)
    with admin_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')

    engine = create_engine(
        url,
        connect_args={"options": f"-csearch_path={schema}"},
        future=True,
    )
    connection = engine.connect()
    with connection.begin():
        connection.exec_driver_sql(
            """
            CREATE TABLE reviews (
                id VARCHAR(36) PRIMARY KEY,
                decision VARCHAR(14) NOT NULL DEFAULT 'PENDING',
                lifecycle_state VARCHAR(14) NOT NULL DEFAULT 'RECEIVED'
            )
            """
        )
        connection.execute(
            text(
                "INSERT INTO reviews (id, decision, lifecycle_state) "
                "VALUES ('existing-review', 'PENDING', 'RECEIVED')"
            )
        )

    config = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    config.attributes["connection"] = connection
    try:
        # Production can have revision 03 recorded even though its DDL did not run.
        command.stamp(config, "20261120_03")
        yield connection, config
    finally:
        connection.close()
        engine.dispose()
        with admin_engine.begin() as cleanup:
            cleanup.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin_engine.dispose()


def _column_lengths(connection) -> dict[str, int]:
    return dict(
        connection.execute(
            text(
                "SELECT column_name, character_maximum_length "
                "FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'reviews' "
                "AND column_name IN ('decision', 'lifecycle_state')"
            )
        ).all()
    )


def test_postgres_repair_migration_preserves_rows_and_saves_unavailable_state(
    postgres_lifecycle_migration,
):
    connection, config = postgres_lifecycle_migration
    assert _column_lengths(connection) == {"decision": 14, "lifecycle_state": 14}

    command.upgrade(config, "head")

    assert _column_lengths(connection) == {"decision": 40, "lifecycle_state": 40}
    connection.execute(
        text(
            "UPDATE reviews SET decision = 'REVIEW_UNAVAILABLE', "
            "lifecycle_state = 'REVIEW_UNAVAILABLE' WHERE id = 'existing-review'"
        )
    )
    assert connection.execute(
        text(
            "SELECT id, decision, lifecycle_state FROM reviews "
            "WHERE id = 'existing-review'"
        )
    ).one() == (
        "existing-review",
        "REVIEW_UNAVAILABLE",
        "REVIEW_UNAVAILABLE",
    )


def test_postgres_repair_migration_accepts_every_current_state(
    postgres_lifecycle_migration,
):
    connection, config = postgres_lifecycle_migration
    command.upgrade(config, "head")

    for decision in DECISION_STATES:
        connection.execute(
            text("UPDATE reviews SET decision = :value WHERE id = 'existing-review'"),
            {"value": decision},
        )
        assert (
            connection.execute(
                text("SELECT decision FROM reviews WHERE id = 'existing-review'")
            ).scalar_one()
            == decision
        )

    for lifecycle_state in LIFECYCLE_STATES:
        connection.execute(
            text(
                "UPDATE reviews SET lifecycle_state = :value "
                "WHERE id = 'existing-review'"
            ),
            {"value": lifecycle_state},
        )
        assert (
            connection.execute(
                text("SELECT lifecycle_state FROM reviews WHERE id = 'existing-review'")
            ).scalar_one()
            == lifecycle_state
        )


def test_postgres_repair_migration_keeps_preexpanded_columns_and_values(
    postgres_lifecycle_migration,
):
    connection, config = postgres_lifecycle_migration
    with connection.begin():
        connection.exec_driver_sql(
            "ALTER TABLE reviews ALTER COLUMN decision TYPE VARCHAR(64)"
        )
        connection.exec_driver_sql(
            "ALTER TABLE reviews ALTER COLUMN lifecycle_state TYPE VARCHAR(64)"
        )
        connection.execute(
            text(
                "UPDATE reviews SET decision = 'REVIEW_UNAVAILABLE', "
                "lifecycle_state = 'REVIEW_UNAVAILABLE' "
                "WHERE id = 'existing-review'"
            )
        )

    command.upgrade(config, "head")

    assert _column_lengths(connection) == {"decision": 64, "lifecycle_state": 64}
    assert connection.execute(
        text(
            "SELECT decision, lifecycle_state FROM reviews WHERE id = 'existing-review'"
        )
    ).one() == ("REVIEW_UNAVAILABLE", "REVIEW_UNAVAILABLE")
