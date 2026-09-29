"""
/auth extras — phone/email change, 2FA, session management.

Same router prefix (/auth) so FastAPI merges endpoints with auth.py.
"""
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from typing import Optional
import random
import logging
from datetime import datetime, timedelta

from app.db.database import database
from app.utils.security import get_current_user
from app.services.email_service import send_otp_email
from app.services.sms_service import send_otp_sms, is_sms_configured


logger = logging.getLogger("auth_extras")

router = APIRouter(prefix="/auth", tags=["Auth Extras"])


def _gen_code() -> str:
    return f"{random.randint(100000, 999999)}"


def _request_meta(request: Optional[Request]) -> dict:
    if request is None:
        return {"device_kind": None, "device_name": None, "ip": None}
    ua = request.headers.get("user-agent", "") or ""
    ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else None)
    )
    kind = "other"
    low = ua.lower()
    if "mobile" in low or "iphone" in low or "android" in low:
        kind = "mobile"
    elif "ipad" in low or "tablet" in low:
        kind = "tablet"
    elif ua:
        kind = "desktop"
    return {
        "device_kind": kind,
        "device_name": ua[:120] if ua else None,
        "ip": ip,
    }


async def _send_code_email(to_email: str, code: str, purpose: str) -> None:
    try:
        await send_otp_email(to_email, code, purpose)
    except Exception as e:
        logger.error("send_otp_email failed for %s: %s", purpose, e)


async def _send_code_phone(phone: str, code: str, purpose: str) -> None:
    if not is_sms_configured():
        logger.warning("[dev] SMS OTP for %s: %s (provider not configured)", phone, code)
        return
    try:
        await send_otp_sms(phone, code, purpose)
    except Exception as e:
        logger.error("send_otp_sms failed for %s: %s", purpose, e)


async def _log_activity(
    user_id: str,
    action: str,
    request: Optional[Request] = None,
    success: bool = True,
) -> None:
    try:
        meta = _request_meta(request)
        await database.execute(
            """
            INSERT INTO login_activity (
                user_id, action, device_kind, device_name, ip, success, created_at
            ) VALUES (
                :uid, :action, :dk, :dn, :ip, :ok, :now
            )
            """,
            {
                "uid": user_id,
                "action": action,
                "dk": meta["device_kind"],
                "dn": meta["device_name"],
                "ip": meta["ip"],
                "ok": success,
                "now": datetime.utcnow(),
            },
        )
    except Exception as e:
        logger.warning("login_activity write failed: %s", e)


async def _store_pending(user_id: str, kind: str, target: str, code: str) -> None:
    expires = datetime.utcnow() + timedelta(minutes=15)
    await database.execute(
        "DELETE FROM pending_contact_changes WHERE user_id = :uid AND kind = :k",
        {"uid": user_id, "k": kind},
    )
    await database.execute(
        """
        INSERT INTO pending_contact_changes (user_id, kind, target, code, expires_at, created_at)
        VALUES (:uid, :k, :t, :c, :exp, :now)
        """,
        {
            "uid": user_id,
            "k": kind,
            "t": target,
            "c": code,
            "exp": expires,
            "now": datetime.utcnow(),
        },
    )


async def _consume_pending(user_id: str, kind: str, code: str) -> Optional[dict]:
    row = await database.fetch_one(
        """
        SELECT id, target, code, expires_at FROM pending_contact_changes
        WHERE user_id = :uid AND kind = :k AND code = :c
        ORDER BY created_at DESC LIMIT 1
        """,
        {"uid": user_id, "k": kind, "c": code},
    )
    if not row:
        return None
    if row["expires_at"] < datetime.utcnow():
        return None
    await database.execute(
        "DELETE FROM pending_contact_changes WHERE id = :id", {"id": row["id"]}
    )
    return dict(row)


# ─────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────
class PhoneChangeRequest(BaseModel):
    phone: str

class PhoneChangeVerify(BaseModel):
    phone: str
    code: str

class EmailChangeRequest(BaseModel):
    email: str

class EmailChangeVerify(BaseModel):
    email: str
    code: str

class TwoFAStart(BaseModel):
    method: str
    phone: Optional[str] = None

class TwoFAVerify(BaseModel):
    method: str
    code: str
    phone: Optional[str] = None


# ─────────────────────────────────────────────────────────────
# PHONE CHANGE
# ─────────────────────────────────────────────────────────────
@router.post("/phone/change/request")
async def request_phone_change(
    req: PhoneChangeRequest, current_user: dict = Depends(get_current_user)
):
    phone = (req.phone or "").strip()
    if not phone.startswith("+"):
        raise HTTPException(status_code=400, detail="Phone must be E.164 (start with +)")

    taken = await database.fetch_one(
        "SELECT id FROM users WHERE phone = :p AND id <> :uid",
        {"p": phone, "uid": current_user["id"]},
    )
    if taken:
        raise HTTPException(status_code=400, detail="That phone is already in use")

    code = _gen_code()
    await _store_pending(current_user["id"], "phone", phone, code)
    await _send_code_phone(phone, code, "phone_change")
    return {"message": "Code sent"}


@router.post("/phone/change/verify")
async def verify_phone_change(
    req: PhoneChangeVerify,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    phone = (req.phone or "").strip()
    pending = await _consume_pending(current_user["id"], "phone", req.code.strip())
    if not pending or pending["target"] != phone:
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    await database.execute(
        "UPDATE users SET phone = :p, phone_verified = TRUE WHERE id = :uid",
        {"p": phone, "uid": current_user["id"]},
    )
    await _log_activity(current_user["id"], "phone_change", request, success=True)
    return {"message": "Phone updated", "phone": phone}


# ─────────────────────────────────────────────────────────────
# EMAIL CHANGE
# ─────────────────────────────────────────────────────────────
@router.post("/email/change/request")
async def request_email_change(
    req: EmailChangeRequest, current_user: dict = Depends(get_current_user)
):
    email = (req.email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Enter a valid email address")

    taken = await database.fetch_one(
        "SELECT id FROM users WHERE LOWER(email) = :e AND id <> :uid",
        {"e": email, "uid": current_user["id"]},
    )
    if taken:
        raise HTTPException(status_code=400, detail="That email is already in use")

    code = _gen_code()
    await _store_pending(current_user["id"], "email", email, code)
    await _send_code_email(email, code, "email_change")
    return {"message": "Code sent"}


@router.post("/email/change/verify")
async def verify_email_change(
    req: EmailChangeVerify,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    email = (req.email or "").strip().lower()
    pending = await _consume_pending(current_user["id"], "email", req.code.strip())
    if not pending or pending["target"] != email:
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    await database.execute(
        "UPDATE users SET email = :e, email_verified = TRUE WHERE id = :uid",
        {"e": email, "uid": current_user["id"]},
    )
    await _log_activity(current_user["id"], "email_change", request, success=True)
    return {"message": "Email updated", "email": email}


# ─────────────────────────────────────────────────────────────
# 2FA
# ─────────────────────────────────────────────────────────────
@router.post("/2fa/start")
async def start_2fa(req: TwoFAStart, current_user: dict = Depends(get_current_user)):
    method = (req.method or "").lower()
    if method not in ("sms", "email"):
        raise HTTPException(status_code=400, detail="Method must be 'sms' or 'email'")

    code = _gen_code()

    if method == "sms":
        phone = (req.phone or "").strip()
        if not phone.startswith("+"):
            raise HTTPException(status_code=400, detail="Phone must be E.164")
        target = phone
        await _store_pending(current_user["id"], "2fa", target, code)
        await _send_code_phone(target, code, "2fa_setup")
    else:
        target = (current_user.get("email") or "").strip().lower()
        if not target:
            raise HTTPException(status_code=400, detail="No email on account")
        await _store_pending(current_user["id"], "2fa", target, code)
        await _send_code_email(target, code, "2fa_setup")

    return {"message": "Code sent"}


@router.post("/2fa/verify")
async def verify_2fa(
    req: TwoFAVerify,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    code = (req.code or "").strip()
    pending = await _consume_pending(current_user["id"], "2fa", code)
    if not pending:
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    target = pending["target"]
    masked = (
        f"{target[:4]}***{target[-4:]}" if len(target) > 8 else target
    )

    await database.execute(
        """
        UPDATE users
        SET two_factor_enabled = TRUE,
            two_factor_method = :m,
            two_factor_target = :t
        WHERE id = :uid
        """,
        {"m": req.method, "t": masked, "uid": current_user["id"]},
    )
    await _log_activity(current_user["id"], "2fa_enabled", request, success=True)
    return {"message": "Two-factor enabled"}


@router.post("/2fa/disable")
async def disable_2fa(
    request: Request, current_user: dict = Depends(get_current_user)
):
    await database.execute(
        """
        UPDATE users
        SET two_factor_enabled = FALSE,
            two_factor_method = NULL,
            two_factor_target = NULL
        WHERE id = :uid
        """,
        {"uid": current_user["id"]},
    )
    await _log_activity(current_user["id"], "2fa_disabled", request, success=True)
    return {"message": "Two-factor disabled"}


# ─────────────────────────────────────────────────────────────
# SESSIONS
# ─────────────────────────────────────────────────────────────
@router.get("/sessions")
async def list_sessions(
    request: Request, current_user: dict = Depends(get_current_user)
):
    rows = await database.fetch_all(
        """
        SELECT id, device_kind, device_name, browser, os, ip, city, country,
               last_active_at, created_at
        FROM user_sessions
        WHERE user_id = :uid
        ORDER BY last_active_at DESC
        LIMIT 50
        """,
        {"uid": current_user["id"]},
    )
    sessions = [dict(r) for r in rows]

    # Identify the current session from the JWT's `sid` claim
    current_sid: Optional[str] = None
    try:
        from jose import jwt
        import os as _os
        auth_header = request.headers.get("authorization", "")
        token = auth_header.split(" ", 1)[1] if " " in auth_header else ""
        if token:
            payload = jwt.decode(
                token,
                _os.getenv("JWT_SECRET") or "dev-only-insecure-secret-change-me",
                algorithms=["HS256"],
            )
            current_sid = payload.get("sid")
    except Exception:
        current_sid = None

    for s in sessions:
        s["current"] = str(s["id"]) == str(current_sid) if current_sid else False

    # If no row matches the current token (legacy token), prepend a synthetic one
    if not any(s["current"] for s in sessions):
        meta = _request_meta(request)
        sessions.insert(
            0,
            {
                "id": "current",
                "device_kind": meta["device_kind"],
                "device_name": meta["device_name"],
                "browser": None,
                "os": None,
                "ip": meta["ip"],
                "city": None,
                "country": None,
                "last_active_at": datetime.utcnow(),
                "created_at": datetime.utcnow(),
                "current": True,
            },
        )

    return sessions


@router.delete("/sessions/others")
async def revoke_others(
    request: Request, current_user: dict = Depends(get_current_user)
):
    """
    Delete all sessions except the current one. Requires a `sid` in the
    JWT — legacy tokens without it fall back to wiping all sessions
    (harmless: user just has to log in again on other devices).
    """
    current_sid: Optional[str] = None
    try:
        from jose import jwt
        import os as _os
        auth_header = request.headers.get("authorization", "")
        token = auth_header.split(" ", 1)[1] if " " in auth_header else ""
        if token:
            payload = jwt.decode(
                token,
                _os.getenv("JWT_SECRET") or "dev-only-insecure-secret-change-me",
                algorithms=["HS256"],
            )
            current_sid = payload.get("sid")
    except Exception:
        current_sid = None

    if current_sid:
        await database.execute(
            "DELETE FROM user_sessions WHERE user_id = :uid AND id <> :sid",
            {"uid": current_user["id"], "sid": current_sid},
        )
    else:
        await database.execute(
            "DELETE FROM user_sessions WHERE user_id = :uid",
            {"uid": current_user["id"]},
        )
    return {"message": "Other sessions revoked"}


@router.delete("/sessions/{session_id}")
async def revoke_session(
    session_id: str, current_user: dict = Depends(get_current_user)
):
    if session_id == "current":
        raise HTTPException(status_code=400, detail="Cannot revoke current session")
    result = await database.execute(
        "DELETE FROM user_sessions WHERE id = :sid AND user_id = :uid",
        {"sid": session_id, "uid": current_user["id"]},
    )
    if isinstance(result, str) and result.strip().endswith("0"):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"message": "Session revoked"}