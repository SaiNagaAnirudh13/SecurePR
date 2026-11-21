import pytest
from app.core.config import (
    Settings,
    get_dashboard_origins,
    validate_production_settings,
)

OPERATOR_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def production_settings(**overrides):
    values = {
        "SECUREPR_ENV": "production",
        "DATABASE_URL": "postgresql://securepr@db.example.invalid/postgres?sslmode=require",
        "SUPABASE_URL": "https://project.example.invalid",
        "SUPABASE_JWT_ALGORITHM": "RS256",
        "GITHUB_APP_ID": "12345",
        "GITHUB_PRIVATE_KEY": "test-only-private-key",
        "GITHUB_PRIVATE_KEY_PATH": "secrets/github-app.pem",
        "GITHUB_WEBHOOK_SECRET": "test-webhook-secret",
        "SECUREPR_BASE_URL": "https://api.example.invalid",
        "DASHBOARD_ORIGIN": "https://dashboard.example.invalid",
        "DASHBOARD_ORIGINS": "https://dashboard.example.invalid",
        "SECUREPR_OPERATOR_IDS": OPERATOR_ID,
        "SECUREPR_ADMIN_IDS": OPERATOR_ID,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_production_requires_webhook_secret():
    settings = production_settings(GITHUB_WEBHOOK_SECRET="")

    with pytest.raises(
        RuntimeError, match="requires configuration for: GITHUB_WEBHOOK_SECRET"
    ):
        validate_production_settings(settings)


def test_production_requires_inline_or_existing_github_private_key():
    settings = production_settings(
        GITHUB_PRIVATE_KEY="",
        GITHUB_PRIVATE_KEY_PATH="missing-github-private-key.pem",
    )

    with pytest.raises(RuntimeError, match="GITHUB_PRIVATE_KEY"):
        validate_production_settings(settings)


def test_production_requires_supabase_auth_audience():
    settings = production_settings(SUPABASE_AUTH_AUDIENCE="")

    with pytest.raises(
        RuntimeError, match="requires configuration for: SUPABASE_AUTH_AUDIENCE"
    ):
        validate_production_settings(settings)


def test_production_requires_operator_allowlists():
    settings = production_settings(SECUREPR_OPERATOR_IDS="", SECUREPR_ADMIN_IDS="")

    with pytest.raises(RuntimeError, match="requires configuration"):
        validate_production_settings(settings)


def test_production_rejects_database_without_ssl():
    settings = production_settings(
        DATABASE_URL="postgresql://securepr@db.example.invalid/postgres"
    )

    with pytest.raises(RuntimeError, match="DATABASE_URL must use SSL"):
        validate_production_settings(settings)


def test_hs256_production_requires_server_side_signing_secret():
    settings = production_settings(
        SUPABASE_JWT_ALGORITHM="HS256", SUPABASE_JWT_SECRET=""
    )

    with pytest.raises(RuntimeError, match="requires a SUPABASE_JWT_SECRET"):
        validate_production_settings(settings)


def test_hs256_production_rejects_short_server_side_signing_secret():
    settings = production_settings(
        SUPABASE_JWT_ALGORITHM="HS256",
        SUPABASE_JWT_SECRET="short",
    )

    with pytest.raises(RuntimeError, match="at least 32 characters"):
        validate_production_settings(settings)


def test_valid_production_configuration_passes():
    assert validate_production_settings(production_settings()) is None


def test_platform_port_activates_production_validation(monkeypatch):
    monkeypatch.setenv("PORT", "8000")

    with pytest.raises(RuntimeError, match="requires configuration"):
        validate_production_settings(
            Settings(_env_file=None, SECUREPR_ENV="development")
        )


def test_production_cors_uses_only_configured_dashboard_origins():
    settings = production_settings(
        DASHBOARD_ORIGINS="https://dashboard.example.invalid, https://admin.example.invalid/",
    )

    assert get_dashboard_origins(settings) == [
        "https://dashboard.example.invalid",
        "https://admin.example.invalid",
    ]


def test_production_canonical_dashboard_origin_must_be_in_cors_allowlist():
    settings = production_settings(DASHBOARD_ORIGIN="https://other.example.invalid")

    with pytest.raises(RuntimeError, match="DASHBOARD_ORIGIN must also be included"):
        validate_production_settings(settings)


def test_cors_rejects_wildcard_origins():
    settings = production_settings(DASHBOARD_ORIGINS="*")

    with pytest.raises(ValueError, match="explicit"):
        get_dashboard_origins(settings)


def test_production_cors_rejects_default_local_origin():
    settings = production_settings(
        DASHBOARD_ORIGINS="", DASHBOARD_ORIGIN="http://localhost:5173"
    )

    with pytest.raises(ValueError, match="non-local"):
        get_dashboard_origins(settings)


def test_run_uses_platform_port(monkeypatch):
    import run

    calls = []
    monkeypatch.setenv("PORT", "12345")
    monkeypatch.setattr(run, "get_settings", lambda: object())
    monkeypatch.setattr(run, "is_production_runtime", lambda _: True)
    monkeypatch.setattr(
        run, "_upgrade_production_schema", lambda: calls.append("migrate")
    )
    monkeypatch.setattr(
        run.uvicorn, "run", lambda *args, **kwargs: calls.append(("serve", kwargs))
    )

    run.main()

    assert calls == [
        "migrate",
        ("serve", {"host": "0.0.0.0", "port": 12345}),
    ]
