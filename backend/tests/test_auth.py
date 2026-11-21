import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import dashboard, webhooks
from app.core import auth
from app.core.config import Settings
from app.core.db import get_db

OPERATOR_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ADMIN_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OTHER_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
JWT_SECRET = "test-only-signing-key-not-for-production"
WEBHOOK_SECRET = "test-only-webhook-secret"
SUPABASE_URL = "https://project.example.invalid"


class EmptyQuery:
    def order_by(self, *args):
        return self

    def all(self):
        return []


class EmptyDatabase:
    def query(self, *args):
        return EmptyQuery()


def make_settings(**overrides):
    values = {
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_JWT_ALGORITHM": "HS256",
        "SUPABASE_JWT_SECRET": JWT_SECRET,
        "SUPABASE_AUTH_AUDIENCE": "authenticated",
        "SECUREPR_OPERATOR_IDS": f"{OPERATOR_ID},{ADMIN_ID}",
        "SECUREPR_ADMIN_IDS": ADMIN_ID,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_token(user_id=OPERATOR_ID, key=JWT_SECRET, algorithm="HS256", exp_offset=300):
    now = datetime.now(timezone.utc)
    claims = {
        "sub": user_id,
        "aud": "authenticated",
        "iss": f"{SUPABASE_URL}/auth/v1",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=exp_offset)).timestamp()),
    }
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": "test-key"})


def make_client(monkeypatch, settings=None):
    monkeypatch.setattr(auth, "get_settings", lambda: settings or make_settings())
    monkeypatch.setattr(webhooks.settings, "GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)

    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(webhooks.router)
    app.dependency_overrides[get_db] = lambda: EmptyDatabase()
    return TestClient(app)


def test_dashboard_route_rejects_missing_bearer_token(monkeypatch):
    response = make_client(monkeypatch).get("/api/repositories")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        make_token(key="another-test-signing-key-not-the-right-one"),
        make_token(exp_offset=-60),
    ],
)
def test_dashboard_route_rejects_invalid_or_expired_tokens(monkeypatch, token):
    response = make_client(monkeypatch).get(
        "/api/repositories",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401


def test_valid_supabase_token_accesses_protected_read_route(monkeypatch):
    response = make_client(monkeypatch).get(
        "/api/repositories",
        headers={"Authorization": f"Bearer {make_token()}"},
    )

    assert response.status_code == 200
    assert response.json() == []


def test_authenticated_but_unlisted_user_is_forbidden(monkeypatch):
    response = make_client(monkeypatch).get(
        "/api/repositories",
        headers={"Authorization": f"Bearer {make_token(user_id=OTHER_ID)}"},
    )

    assert response.status_code == 403


def test_operator_cannot_use_admin_action(monkeypatch):
    response = make_client(monkeypatch).post(
        "/api/circuit-breakers/trip",
        json={"scope": "GLOBAL", "reason": "test"},
        headers={"Authorization": f"Bearer {make_token()}"},
    )

    assert response.status_code == 403


def test_admin_can_use_admin_action(monkeypatch):
    monkeypatch.setattr(
        dashboard.cb_service,
        "trip",
        lambda *args: SimpleNamespace(id="breaker-id", is_tripped=True),
    )
    response = make_client(monkeypatch).post(
        "/api/circuit-breakers/trip",
        json={"scope": "GLOBAL", "reason": "test"},
        headers={"Authorization": f"Bearer {make_token(user_id=ADMIN_ID)}"},
    )

    assert response.status_code == 200
    assert response.json() == {"id": "breaker-id", "is_tripped": True}


def test_asymmetric_supabase_token_uses_jwks_signing_key(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class FakeJwksClient:
        def get_signing_key_from_jwt(self, token):
            return SimpleNamespace(key=private_key.public_key())

    monkeypatch.setattr(auth, "_jwks_client", lambda url: FakeJwksClient())
    token = make_token(key=private_key, algorithm="RS256")
    response = make_client(
        monkeypatch,
        make_settings(SUPABASE_JWT_ALGORITHM="RS256", SUPABASE_JWT_SECRET=""),
    ).get("/api/repositories", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize("signature", [None, "sha256=invalid"])
def test_webhook_rejects_missing_or_invalid_hmac_without_bearer(monkeypatch, signature):
    headers = {"x-github-event": "ping"}
    if signature:
        headers["x-hub-signature-256"] = signature
    response = make_client(monkeypatch).post(
        "/webhooks/github",
        content=b'{"zen":"test"}',
        headers=headers,
    )

    assert response.status_code == 401


def test_valid_hmac_webhook_does_not_require_supabase_bearer(monkeypatch):
    body = b'{"zen":"test"}'
    signature = (
        "sha256="
        + hmac.new(
            WEBHOOK_SECRET.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
    )
    response = make_client(monkeypatch).post(
        "/webhooks/github",
        content=body,
        headers={
            "x-github-event": "ping",
            "x-hub-signature-256": signature,
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_webhook_fails_closed_when_hmac_secret_is_missing(monkeypatch):
    client = make_client(monkeypatch)
    monkeypatch.setattr(webhooks.settings, "GITHUB_WEBHOOK_SECRET", "")
    response = client.post(
        "/webhooks/github",
        content=b'{"zen":"test"}',
        headers={"x-github-event": "ping"},
    )

    assert response.status_code == 503
