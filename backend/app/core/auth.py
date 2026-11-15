"""Supabase access-token validation and SecurePR operator authorization."""
from dataclasses import dataclass
from functools import lru_cache
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError

from app.core.config import (
    SUPPORTED_SUPABASE_JWT_ALGORITHMS,
    get_settings,
    parse_user_ids,
)

_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class OperatorIdentity:
    user_id: str
    role: str


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(
        f"{url.rstrip('/')}/auth/v1/.well-known/jwks.json",
        timeout=5,
    )


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="A valid Supabase access token is required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _decode_access_token(token: str) -> dict:
    settings = get_settings()
    if not settings.SUPABASE_URL:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Supabase authentication is not configured.",
        )

    issuer = f"{settings.SUPABASE_URL.rstrip('/')}/auth/v1"
    try:
        algorithm = jwt.get_unverified_header(token).get("alg")
    except jwt.PyJWTError as exc:
        raise _unauthorized() from exc

    configured_algorithm = settings.SUPABASE_JWT_ALGORITHM
    if not configured_algorithm or configured_algorithm not in SUPPORTED_SUPABASE_JWT_ALGORITHMS:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Supabase JWT verification is not configured correctly.",
        )
    if algorithm != configured_algorithm:
        raise _unauthorized()

    if configured_algorithm == "HS256":
        if not settings.SUPABASE_JWT_SECRET:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Supabase HS256 verification requires a server-side signing secret.",
            )
        signing_key = settings.SUPABASE_JWT_SECRET
    else:
        try:
            signing_key = _jwks_client(settings.SUPABASE_URL).get_signing_key_from_jwt(token).key
        except PyJWKClientConnectionError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Supabase signing keys are temporarily unavailable.",
            ) from exc
        except PyJWKClientError as exc:
            raise _unauthorized() from exc

    try:
        return jwt.decode(
            token,
            signing_key,
            algorithms=[configured_algorithm],
            audience=settings.SUPABASE_AUTH_AUDIENCE,
            issuer=issuer,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise _unauthorized() from exc


def get_current_operator(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> OperatorIdentity:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise _unauthorized()

    claims = _decode_access_token(credentials.credentials)
    subject = claims.get("sub")
    if not isinstance(subject, str):
        raise _unauthorized()

    try:
        user_id = str(UUID(subject))
        operator_ids = parse_user_ids(
            get_settings().SECUREPR_OPERATOR_IDS,
            "SECUREPR_OPERATOR_IDS",
        )
        admin_ids = parse_user_ids(
            get_settings().SECUREPR_ADMIN_IDS,
            "SECUREPR_ADMIN_IDS",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SecurePR operator access is not configured correctly.",
        ) from exc

    if user_id not in operator_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This Supabase user is not authorized to access SecurePR.",
        )
    return OperatorIdentity(user_id=user_id, role="admin" if user_id in admin_ids else "operator")


def require_admin(
    operator: OperatorIdentity = Depends(get_current_operator),
) -> OperatorIdentity:
    if operator.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires a SecurePR administrator.",
        )
    return operator
