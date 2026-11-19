import asyncio
import hashlib
import hmac
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.api import webhooks
from app.core.db import Base
from app.models import models as m


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'webhooks.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    yield session
    session.close()


def _request_with_body(body: bytes) -> Request:
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.request", "body": b"", "more_body": False}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/webhooks/github",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )


def _signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _payload(action="opened", head_sha="abc123"):
    return {
        "action": action,
        "number": 7,
        "repository": {
            "id": 1,
            "name": "widgets",
            "full_name": "octo/widgets",
            "default_branch": "main",
            "owner": {"login": "octo"},
        },
        "installation": {"id": 999},
        "pull_request": {
            "title": "Add feature",
            "state": "closed" if action == "closed" else "open",
            "user": {"login": "octocat"},
            "head": {"sha": head_sha, "ref": "feature"},
            "base": {"ref": "main"},
        },
    }


def _deliver(payload, delivery_id, db_session):
    secret = "test-webhook-secret"
    body = json.dumps(payload).encode("utf-8")
    request = _request_with_body(body)
    return asyncio.run(
        webhooks.github_webhook(
            request,
            x_github_event="pull_request",
            x_github_delivery=delivery_id,
            x_hub_signature_256=_signature(secret, body),
            db=db_session,
        )
    )


def test_invalid_and_missing_webhook_signatures_are_rejected(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "GITHUB_WEBHOOK_SECRET", "test-secret")

    with pytest.raises(HTTPException) as missing:
        webhooks._verify_signature(b"{}", None)
    with pytest.raises(HTTPException) as invalid:
        webhooks._verify_signature(b"{}", "sha256=invalid")

    assert missing.value.status_code == 401
    assert invalid.value.status_code == 401


def test_pull_request_events_queue_waiting_comment_idempotently_and_close_pr(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        webhooks.settings, "GITHUB_WEBHOOK_SECRET", "test-webhook-secret"
    )

    first = _deliver(_payload(), "delivery-1", db_session)
    duplicate = _deliver(_payload(), "delivery-1", db_session)

    assert first["status"] == duplicate["status"] == "accepted"
    assert (
        db_session.query(m.BackgroundJob)
        .filter(m.BackgroundJob.job_type == "REVIEW_STATUS")
        .count()
        == 1
    )
    assert (
        db_session.query(m.BackgroundJob)
        .filter(m.BackgroundJob.job_type == "REVIEW")
        .count()
        == 1
    )
    assert (
        db_session.query(m.BackgroundJob)
        .filter(m.BackgroundJob.job_type == "CI_REFRESH")
        .count()
        == 1
    )

    synchronized = _deliver(
        _payload(action="synchronize", head_sha="new-sha"),
        "delivery-2",
        db_session,
    )
    assert synchronized["status"] == "accepted"
    pr = db_session.query(m.PullRequest).one()
    assert pr.head_sha == "new-sha"
    assert (
        db_session.query(m.BackgroundJob)
        .filter(m.BackgroundJob.job_type == "REVIEW_STATUS")
        .count()
        == 2
    )

    closed = _deliver(
        _payload(action="closed", head_sha="new-sha"),
        "delivery-3",
        db_session,
    )
    assert closed["status"] == "accepted"
    assert pr.state == "closed"


def test_missing_installation_information_is_rejected(db_session, monkeypatch):
    monkeypatch.setattr(
        webhooks.settings, "GITHUB_WEBHOOK_SECRET", "test-webhook-secret"
    )
    payload = _payload()
    payload.pop("installation")

    with pytest.raises(HTTPException) as missing_installation:
        _deliver(payload, "delivery-missing-installation", db_session)

    assert missing_installation.value.status_code == 400
    assert db_session.query(m.Repository).count() == 0
