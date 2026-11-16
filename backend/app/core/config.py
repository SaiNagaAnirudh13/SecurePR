"""
Centralized application configuration.

All values are loaded from environment variables (see .env.example).
Nothing secret is ever hardcoded here.
"""

import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_SUPABASE_JWT_ALGORITHMS = frozenset(
    {"HS256", "ES256", "ES384", "RS256", "EdDSA"}
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

    # Database
    DATABASE_URL: str = ""

    # GitHub App
    GITHUB_APP_ID: str = ""
    GITHUB_PRIVATE_KEY: str = ""
    GITHUB_PRIVATE_KEY_PATH: str = "./secrets/github-app-private-key.pem"
    GITHUB_WEBHOOK_SECRET: str = ""

    # Gemini
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.8-flash"
    GEMINI_FALLBACK_MODEL: str = "gemini-2.0-flash"
    GEMINI_REQUEST_TIMEOUT_SECONDS: int = 20

    # App
    SECUREPR_ENV: str = "development"
    SECUREPR_BASE_URL: str = "http://localhost:8000"
    DASHBOARD_ORIGIN: str = "http://localhost:5173"
    DASHBOARD_ORIGINS: str = ""

    # Supabase
    SUPABASE_URL: str = ""
    SUPABASE_JWT_ALGORITHM: str = ""
    SUPABASE_JWT_SECRET: str = ""
    SUPABASE_AUTH_AUDIENCE: str = "authenticated"

    # Access control
    SECUREPR_OPERATOR_IDS: str = ""
    SECUREPR_ADMIN_IDS: str = ""

    # Notifications
    NOTIFY_WEBHOOK_URL: str = ""

    # Risk weights
    RISK_WEIGHT_CHANGE_SURFACE: float = 0.20
    RISK_WEIGHT_REVERSIBILITY: float = 0.15
    RISK_WEIGHT_DATA_SECURITY: float = 0.25
    RISK_WEIGHT_OPERATIONAL: float = 0.15
    RISK_WEIGHT_VERIFICATION: float = 0.15
    RISK_WEIGHT_POLICY_COMPLIANCE: float = 0.10

    # CI wait settings
    CI_WAIT_POLL_INTERVAL_SECONDS: int = 20
    CI_WAIT_TIMEOUT_SECONDS: int = 900
    CI_DISCOVERY_STABILIZATION_SECONDS: int = 30
    POST_CI_WAITING_COMMENT: bool = True

    # Auto-merge settings
    AUTO_MERGE_ENABLED: bool = False
    AUTO_MERGE_LOW_RISK_ONLY: bool = True
    AUTO_MERGE_REQUIRE_ALL_CI: bool = True


def _is_production_runtime(settings: Settings) -> bool:
    return (
        settings.SECUREPR_ENV.casefold() == "production"
        or bool(os.getenv("PORT"))
    )


def is_production_runtime(settings: Settings) -> bool:
    return _is_production_runtime(settings)


def parse_user_ids(value: str, setting_name: str) -> set[str]:
    user_ids = set()

    for raw_id in value.split(","):
        raw_id = raw_id.strip()

        if not raw_id:
            continue

        try:
            user_ids.add(str(UUID(raw_id)))
        except ValueError as exc:
            raise ValueError(
                f"{setting_name} must contain comma-separated Supabase user UUIDs."
            ) from exc

    return user_ids


def _require_https_url(setting_name: str, value: str) -> None:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise RuntimeError(
            f"{setting_name} contains an invalid URL."
        ) from exc

    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path not in ("", "/")
    ):
        raise RuntimeError(
            f"{setting_name} must be an HTTPS origin without a path in production."
        )


def _validate_database_url(value: str) -> None:
    """
    Validate the production PostgreSQL connection URL.

    Hostnames are allowed. The database server does NOT need to be
    represented by an IPv4 or IPv6 address.
    """

    try:
        database_url = urlsplit(value)
        hostname = database_url.hostname
    except ValueError as exc:
        raise RuntimeError(
            "DATABASE_URL is malformed. Copy the PostgreSQL connection "
            "string directly from Supabase and do not add square brackets "
            "around the hostname."
        ) from exc

    if not database_url.scheme.startswith("postgresql"):
        raise RuntimeError(
            "Production DATABASE_URL must use a PostgreSQL connection string."
        )

    if not hostname:
        raise RuntimeError(
            "DATABASE_URL must include a valid database hostname."
        )

    if not database_url.username:
        raise RuntimeError(
            "DATABASE_URL must include a database username."
        )

    if not database_url.path or database_url.path == "/":
        raise RuntimeError(
            "DATABASE_URL must include a database name."
        )

    ssl_modes = parse_qs(database_url.query).get("sslmode", [])

    if not any(
        mode in {"require", "verify-ca", "verify-full"}
        for mode in ssl_modes
    ):
        raise RuntimeError(
            "Production DATABASE_URL must use SSL. "
            "Add ?sslmode=require to the PostgreSQL connection string."
        )


def validate_production_settings(settings: Settings) -> None:
    if not _is_production_runtime(settings):
        return

    required = {
        "DATABASE_URL": settings.DATABASE_URL,
        "SUPABASE_URL": settings.SUPABASE_URL,
        "SUPABASE_JWT_ALGORITHM": settings.SUPABASE_JWT_ALGORITHM,
        "SUPABASE_AUTH_AUDIENCE": settings.SUPABASE_AUTH_AUDIENCE,
        "GITHUB_APP_ID": settings.GITHUB_APP_ID,
        "GITHUB_PRIVATE_KEY": (
            settings.GITHUB_PRIVATE_KEY
            or (
                settings.GITHUB_PRIVATE_KEY_PATH
                if Path(settings.GITHUB_PRIVATE_KEY_PATH).is_file()
                else ""
            )
        ),
        "GITHUB_WEBHOOK_SECRET": settings.GITHUB_WEBHOOK_SECRET,
        "SECUREPR_BASE_URL": settings.SECUREPR_BASE_URL,
        "DASHBOARD_ORIGIN": settings.DASHBOARD_ORIGIN,
        "SECUREPR_OPERATOR_IDS": settings.SECUREPR_OPERATOR_IDS,
        "SECUREPR_ADMIN_IDS": settings.SECUREPR_ADMIN_IDS,
    }

    missing = [
        name
        for name, value in required.items()
        if not str(value).strip()
    ]

    if missing:
        raise RuntimeError(
            "Production startup requires configuration for: "
            + ", ".join(missing)
        )

    _require_https_url("SUPABASE_URL", settings.SUPABASE_URL)
    _require_https_url("SECUREPR_BASE_URL", settings.SECUREPR_BASE_URL)
    _require_https_url("DASHBOARD_ORIGIN", settings.DASHBOARD_ORIGIN)

    if settings.SUPABASE_JWT_ALGORITHM not in SUPPORTED_SUPABASE_JWT_ALGORITHMS:
        raise RuntimeError(
            "SUPABASE_JWT_ALGORITHM is not supported."
        )

    if (
        settings.SUPABASE_JWT_ALGORITHM == "HS256"
        and len(settings.SUPABASE_JWT_SECRET) < 32
    ):
        raise RuntimeError(
            "HS256 production authentication requires a "
            "SUPABASE_JWT_SECRET of at least 32 characters."
        )

    _validate_database_url(settings.DATABASE_URL)

    try:
        operator_ids = parse_user_ids(
            settings.SECUREPR_OPERATOR_IDS,
            "SECUREPR_OPERATOR_IDS",
        )
        admin_ids = parse_user_ids(
            settings.SECUREPR_ADMIN_IDS,
            "SECUREPR_ADMIN_IDS",
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc

    if (
        not operator_ids
        or not admin_ids
        or not admin_ids.issubset(operator_ids)
    ):
        raise RuntimeError(
            "Production requires operator and admin IDs; "
            "every admin ID must also be an operator ID."
        )

    origins = get_dashboard_origins(settings)
    canonical_origin = settings.DASHBOARD_ORIGIN.strip().rstrip("/")

    if canonical_origin not in origins:
        raise RuntimeError(
            "DASHBOARD_ORIGIN must also be included in the configured "
            "CORS origins."
        )

    for origin in origins:
        try:
            parsed_origin = urlsplit(origin)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid dashboard origin: {origin}"
            ) from exc

        if (
            parsed_origin.scheme != "https"
            or not parsed_origin.hostname
            or parsed_origin.path not in ("", "/")
        ):
            raise RuntimeError(
                "Production dashboard origins must be explicit HTTPS origins."
            )


def get_dashboard_origins(settings: Settings) -> list[str]:
    origins = [
        origin.strip().rstrip("/")
        for origin in settings.DASHBOARD_ORIGINS.split(",")
        if origin.strip()
    ]

    is_production = _is_production_runtime(settings)

    if not origins and settings.DASHBOARD_ORIGIN:
        default_origin = settings.DASHBOARD_ORIGIN.strip().rstrip("/")

        if is_production and default_origin.startswith(
            (
                "http://localhost",
                "http://127.0.0.1",
            )
        ):
            raise ValueError(
                "Production requires an explicit non-local dashboard origin."
            )

        origins = [default_origin]

    if is_production and not origins:
        raise ValueError(
            "Production requires at least one configured dashboard origin."
        )

    if not is_production:
        origins.extend(
            [
                "http://localhost:5173",
                "http://localhost:3000",
                "http://127.0.0.1:5173",
                "http://127.0.0.1:3000",
            ]
        )

    if "*" in origins:
        raise ValueError(
            "CORS origins must be explicit; wildcard origins are not allowed."
        )

    return list(dict.fromkeys(origins))


@lru_cache
def get_settings() -> Settings:
    return Settings()
