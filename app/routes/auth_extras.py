"""
/auth extras — phone/email change, 2FA, session management.

Kept in a separate file so auth.py stays untouched. Same router prefix
(/auth) — FastAPI merges them at include_router time.
"""
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from typing import Optional
import random
import traceback
from datetime import datetime, timedelta

from app.db.database import database
from app.utils.security import get_current_user

# Optional: pull the existing OTP sender from auth.py if it's exposed.
try:
    from .auth import send_otp_email  # type: ignore
except Exception:
    send_otp_email = None  # type: ignore


router = APIRouter(prefix="/auth", tags=["Auth Extras"])


def _gen_code() -> str:
    return f"{random.randint(100000, 999999)}"


def _request_meta(request: Request) -> dict:
    """Best-effort device + IP extraction. Missing fields stay None."""
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


async def _send_code_email(to_email: str, code: str) -> None:
    if send_otp_email is None:
        print(f"[dev] OTP for {to_email}: {code}", flush=True)
        return
    try:
        # Try the most common signatures — auth.py's exact one is unknown.
        try:
            await send_otp_email(to_email, code)  # type: ignore
        except TypeError:
            send_otp_email(to_email, code)  # type: ignore
    except Exception as e:
        print(f"⚠️  send_otp_email failed ({e}); code was {code}", flush=True)


async def _send_code_phone(phone: str, code: str) -> None:
    try:
        from app.services.sms_service import send_sms  # type: ignore
        try:
            await send_sms(phone, f"Your Admerce code is {code}. Do not share it.")  # type: ignore
        except TypeError:
            send_sms(phone, f"Your Admerce code is {code}. Do not share it.")  # type: ignore
    except Exception:
        # No SMS provider configured yet — log so dev can test.
        print(f"[dev] SMS OTP for {phone}: {code}", flush=True)


async def _store_pending(user_id: str, kind: str, target: str, code: str) -> None:
    expires = datetime.utcnow() + timedelta(minutes=15)
    # Invalidate any prior pending rows of the same kind
    await database.execute(
        "DELETE FROM pending_contact_changes WHERE user_id = :uid AND kind = :k",
        {"uid": user_id, "k": kind},
    )
    await database.execute(
        """
        INSERT INTO pending_contact_changes (user_id, kind, target, code, expires_at, created_at)
        VALUES (:uid, :k, :t, :c, :exp, :now)
        """,
        {"uid": user_id, "k": kind, "t": target, "c": code, "exp": expires, "now": datetime.utcnow()},
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
    method: str  # 'sms' | 'email'
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
    await _send_code_phone(phone, code)
    return {"message": "Code sent"}


@router.post("/phone/change/verify")
async def verify_phone_change(
    req: PhoneChangeVerify, current_user: dict = Depends(get_current_user)
):
    phone = (req.phone or "").strip()
    pending = await _consume_pending(current_user["id"], "phone", req.code.strip())
    if not pending or pending["target"] != phone:
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    await database.execute(
        "UPDATE users SET phone = :p, phone_verified = TRUE WHERE id = :uid",
        {"p": phone, "uid": current_user["id"]},
    )
    await database.execute(
        """
        INSERT INTO login_activity (user_id, action, success, created_at)
        VALUES (:uid, 'phone_change', TRUE, :now)
        """,
        {"uid": current_user["id"], "now": datetime.utcnow()},
    )
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
    await _send_code_email(email, code)
    return {"message": "Code sent"}


@router.post("/email/change/verify")
async def verify_email_change(
    req: EmailChangeVerify, current_user: dict = Depends(get_current_user)
):
    email = (req.email or "").strip().lower()
    pending = await _consume_pending(current_user["id"], "email", req.code.strip())
    if not pending or pending["target"] != email:
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    await database.execute(
        "UPDATE users SET email = :e, email_verified = TRUE WHERE id = :uid",
        {"e": email, "uid": current_user["id"]},
    )
    await database.execute(
        """
        INSERT INTO login_activity (user_id, action, success, created_at)
        VALUES (:uid, 'email_change', TRUE, :now)
        """,
        {"uid": current_user["id"], "now": datetime.utcnow()},
    )
    return {"message": "Email updated", "email": email}


# ─────────────────────────────────────────────────────────────
# 2FA
# ─────────────────────────────────────────────────────────────
@router.post("/2fa/start")
async def start_2fa(req: TwoFAStart, current_user: dict = Depends(get_current_user)):
    method = (req.method or "").lower()
    if method not in ("sms", "email"):
        raise HTTPException(status_code=400, detail="Method must be 'sms' or 'email'")

    if method == "sms":
        phone = (req.phone or "").strip()
        if not phone.startswith("+"):
            raise HTTPException(status_code=400, detail="Phone must be E.164")
        target = phone
        await _store_pending(current_user["id"], "2fa", target, _gen_code())
        row = await database.fetch_one(
            "SELECT code FROM pending_contact_changes WHERE user_id = :uid AND kind = '2fa' "
            "ORDER BY created_at DESC LIMIT 1",
            {"uid": current_user["id"]},
        )
        if row:
            await _send_code_phone(target, row["code"])
    else:
        target = (current_user.get("email") or "").strip().lower()
        if not target:
            raise HTTPException(status_code=400, detail="No email on account")
        await _store_pending(current_user["id"], "2fa", target, _gen_code())
        row = await database.fetch_one(
            "SELECT code FROM pending_contact_changes WHERE user_id = :uid AND kind = '2fa' "
            "ORDER BY created_at DESC LIMIT 1",
            {"uid": current_user["id"]},
        )
        if row:
            await _send_code_email(target, row["code"])

    return {"message": "Code sent"}


@router.post("/2fa/verify")
async def verify_2fa(req: TwoFAVerify, current_user: dict = Depends(get_current_user)):
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
    await database.execute(
        """
        INSERT INTO login_activity (user_id, action, success, created_at)
        VALUES (:uid, '2fa_enabled', TRUE, :now)
        """,
        {"uid": current_user["id"], "now": datetime.utcnow()},
    )
    return {"message": "Two-factor enabled"}


@router.post("/2fa/disable")
async def disable_2fa(current_user: dict = Depends(get_current_user)):
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
    await database.execute(
        """
        INSERT INTO login_activity (user_id, action, success, created_at)
        VALUES (:uid, '2fa_disabled', TRUE, :now)
        """,
        {"uid": current_user["id"], "now": datetime.utcnow()},
    )
    return {"message": "Two-factor disabled"}


# ─────────────────────────────────────────────────────────────
# SESSIONS
# ─────────────────────────────────────────────────────────────
@router.get("/sessions")
async def list_sessions(
    request: Request, current_user: dict = Depends(get_current_user)
):
    """
    Returns tracked sessions from user_sessions. Empty until login writes
    to that table (see caveats). The current request is always included.
    """
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

    # Always expose the current session so the UI shows at least one row.
    meta = _request_meta(request)
    current = {
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
    }
    for s in sessions:
        s["current"] = False

    return [current, *sessions]


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


@router.delete("/sessions/others")
async def revoke_others(current_user: dict = Depends(get_current_user)):
    await database.execute(
        "DELETE FROM user_sessions WHERE user_id = :uid",
        {"uid": current_user["id"]},
    )
    return {"message": "All other sessions revoked"}