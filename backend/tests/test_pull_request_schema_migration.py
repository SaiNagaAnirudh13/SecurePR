import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import DateTime, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.db import Base
from app.models import models as m

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_pull_request_model_declares_nullable_merge_metadata():
    merged_at = m.PullRequest.__table__.c.merged_at
    merge_commit_sha = m.PullRequest.__table__.c.merge_commit_sha

    assert merged_at.nullable
    assert isinstance(merged_at.type, DateTime)
    assert merge_commit_sha.nullable
    assert merge_commit_sha.type.length == 64


@pytest.fixture()
def postgres_migration_database():
    database_url = os.environ.get("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_URL is not configured.")

    url = make_url(database_url)
    if url.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("TEST_POSTGRES_URL must point to a local PostgreSQL test server.")

    schema = f"securepr_migration_test_{uuid.uuid4().hex}"
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
            CREATE TABLE pull_requests (
                id VARCHAR(36) PRIMARY KEY,
                repository_id VARCHAR(36) NOT NULL,
                github_pr_number INTEGER NOT NULL,
                title VARCHAR(1024),
                author_login VARCHAR(255),
                head_sha VARCHAR(64),
                base_branch VARCHAR(255),
                head_branch VARCHAR(255),
                state VARCHAR(32) NOT NULL DEFAULT 'open',
                created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
                updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE reviews (
                id VARCHAR(36) PRIMARY KEY
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE ci_revisions (
                id VARCHAR(36) PRIMARY KEY
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE ci_executions (
                id VARCHAR(36) PRIMARY KEY
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE review_publications (
                id VARCHAR(36) PRIMARY KEY,
                message_type VARCHAR(32) NOT NULL
            )
            """
        )
        connection.execute(
            text(
                "INSERT INTO pull_requests "
                "(id, repository_id, github_pr_number, title, author_login, head_sha, "
                "base_branch, head_branch) "
                "VALUES ('existing-pr', 'existing-repo', 7, 'Existing PR', "
                "'octocat', 'original-sha', 'main', 'feature')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO review_publications (id, message_type) "
                "VALUES ('existing-publication', 'REVIEW')"
            )
        )

    config = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    config.attributes["connection"] = connection
    try:
        yield connection, config
    finally:
        connection.close()
        engine.dispose()
        with admin_engine.begin() as cleanup:
            cleanup.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin_engine.dispose()


def test_postgres_migration_adds_columns_and_preserves_existing_pr(
    postgres_migration_database,
):
    connection, config = postgres_migration_database

    command.upgrade(config, "head")

    columns = {
        row[0]
        for row in connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'pull_requests'"
            )
        )
    }
    assert {"merged_at", "merge_commit_sha"}.issubset(columns)
    assert connection.execute(
        text("SELECT id, title, head_sha FROM pull_requests WHERE id = 'existing-pr'")
    ).one() == ("existing-pr", "Existing PR", "original-sha")
    session = Session(bind=connection)
    try:
        migrated_pr = session.query(m.PullRequest).filter_by(id="existing-pr").one()
        assert migrated_pr.merged_at is None
        assert migrated_pr.merge_commit_sha is None
    finally:
        session.close()

    publication_length = connection.execute(
        text(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'review_publications' AND column_name = 'message_type'"
        )
    ).scalar_one()
    assert publication_length == 64


def test_postgres_migration_rerun_is_safe(postgres_migration_database):
    connection, config = postgres_migration_database

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    assert (
        connection.execute(
            text("SELECT COUNT(*) FROM pull_requests WHERE id = 'existing-pr'")
        ).scalar_one()
        == 1
    )
    assert (
        connection.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'pull_requests' "
                "AND column_name IN ('merged_at', 'merge_commit_sha')"
            )
        ).scalar_one()
        == 2
    )


def test_postgres_migration_accepts_merge_columns_already_present(
    postgres_migration_database,
):
    connection, config = postgres_migration_database

    with connection.begin():
        connection.exec_driver_sql(
            "ALTER TABLE pull_requests ADD COLUMN merged_at TIMESTAMP WITHOUT TIME ZONE"
        )
        connection.exec_driver_sql(
            "ALTER TABLE pull_requests ADD COLUMN merge_commit_sha VARCHAR(64)"
        )
        connection.execute(
            text(
                "UPDATE pull_requests "
                "SET merged_at = TIMESTAMP '2026-11-20 12:00:00', "
                "merge_commit_sha = 'preserved-merge-sha' "
                "WHERE id = 'existing-pr'"
            )
        )

    command.upgrade(config, "head")

    assert (
        connection.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'pull_requests' "
                "AND column_name IN ('merged_at', 'merge_commit_sha')"
            )
        ).scalar_one()
        == 2
    )
    assert (
        connection.execute(
            text("SELECT id FROM pull_requests WHERE id = 'existing-pr'")
        ).scalar_one()
        == "existing-pr"
    )
    assert (
        connection.execute(
            text("SELECT merge_commit_sha FROM pull_requests WHERE id = 'existing-pr'")
        ).scalar_one()
        == "preserved-merge-sha"
    )


def test_pull_request_model_is_usable_after_schema_creation(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'schema.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    try:
        repo = m.Repository(
            github_repo_id="schema-test",
            owner_login="octo",
            name="widgets",
            full_name="octo/widgets",
        )
        session.add(repo)
        session.flush()
        pr = m.PullRequest(
            repository_id=repo.id,
            github_pr_number=1,
            head_sha="current-sha",
            merged_at=None,
            merge_commit_sha=None,
        )
        session.add(pr)
        session.commit()

        assert (
            session.query(m.PullRequest).filter_by(head_sha="current-sha").one() == pr
        )
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()
        engine.dispose()
