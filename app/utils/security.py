"""
Auth primitives — now a thin re-export of `app.routes.auth`.

Historically this file defined `get_current_user` itself, which created a
circular import with `auth.py` (this file imports SECRET_KEY from auth, and
auth imports get_current_user from here). All auth primitives now live in
`auth.py`; this file re-exports them so nothing else needs to change its
imports, and keeps `get_current_admin` as a small local wrapper.
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


async def get_current_admin(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return current_user