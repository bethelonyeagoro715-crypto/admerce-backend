"""
Auth primitives — now a thin re-export of `app.routes.auth`.

Historically this file defined `get_current_user` itself, which created a
circular import with `auth.py`. All auth primitives now live in `auth.py`;
this file re-exports them so nothing else needs to change its imports,
and keeps `get_current_admin` as a small local wrapper.
"""
from fastapi import Depends, HTTPException, status

from app.routes.auth import (  # noqa: F401
    SECRET_KEY,
    ALGORITHM,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    oauth2_scheme,
    create_access_token,
    hash_password,
    verify_password,
    get_current_user,
    get_optional_user,
)

# Fields never to expose on the "current user" object returned by dependency
# helpers. Mirrors `_SENSITIVE_USER_KEYS` in auth.py so both sides stay in sync.
_SENSITIVE_USER_KEYS = {
    "hashed_password",
    "password_hash",
    "pin_hash",
    "withdrawal_pin",
    "kyc_bvn",
    "kyc_document_url",
    "kyc_document_number",
    "kyc_selfie_url",
    "kyc_id_url",
    "api_key",
    "api_secret",
}


def _public_user(user: dict) -> dict:
    """Strip sensitive columns. Safe to return to the client."""
    return {k: v for k, v in user.items() if k not in _SENSITIVE_USER_KEYS}


async def get_current_admin(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    # Return a sanitized copy. Callers that need the raw row for internal
    # logic can still call get_current_user directly — this dependency
    # exists to gate admin endpoints, and its return value often lands in
    # a response body. Never expose hashed_password.
    return _public_user(current_user)