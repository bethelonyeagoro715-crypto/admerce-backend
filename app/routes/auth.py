import hashlib
import hmac
import secrets
import random  # kept only for non-security uses elsewhere; not used here anymore
import os
import time
import logging
from fastapi import APIRouter, HTTPException, Depends, status, Request
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from jose import jwt, JWTError
from datetime import datetime, timedelta, timezone
import uuid
from typing import Optional
import phonenumbers
import bcrypt

from app.db.database import database
from app.services.email_service import send_otp_email
from app.services.sms_service import send_otp_sms, is_sms_configured

logger = logging.getLogger("auth")

router = APIRouter(prefix="/auth", tags=["Authentication"])

# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("JWT_SECRET")
if not SECRET_KEY:
    _env = os.getenv("ENVIRONMENT", "development").lower()
    _is_prod = (
        _env in ("production", "prod")
        or os.getenv("RENDER") == "true"
        or os.getenv("RENDER_SERVICE_ID") is not None
    )
    if _is_prod:
        raise RuntimeError("JWT_SECRET environment variable is required in production.")
    SECRET_KEY = "dev-only-insecure-secret-change-me"
    logger.warning("JWT_SECRET not set — insecure development fallback active.")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24
DEFAULT_PHONE_REGION = os.getenv("DEFAULT_PHONE_REGION", "NG")

# Password policy
_MIN_PASSWORD_LEN = 8
_MAX_PASSWORD_LEN = 128

# Fields never returned by /auth/me
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

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

_rate_buckets: dict[str, list[float]] = {}


def _rate_limit(key: str, max_calls: int, window_sec: int) -> None:
    now = time.time()
    bucket = _rate_buckets.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < window_sec]
    if len(bucket) >= max_calls:
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Please try again in a few minutes.",
        )
    bucket.append(now)
    if len(_rate_buckets) > 10_000:
        cutoff = now - 3600
        for k in list(_rate_buckets.keys()):
            _rate_buckets[k] = [t for t in _rate_buckets[k] if t > cutoff]
            if not _rate_buckets[k]:
                del _rate_buckets[k]


def _request_meta(request: Optional[Request]) -> dict:
    if request is None:
        return {"device_kind": None, "device_name": None, "ip": None,
                "browser": None, "os": None}
    ua = request.headers.get("user-agent", "") or ""
    ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else None)
    )
    low = ua.lower()
    kind = "other"
    if "mobile" in low or "iphone" in low or "android" in low:
        kind = "mobile"
    elif "ipad" in low or "tablet" in low:
        kind = "tablet"
    elif ua:
        kind = "desktop"

    browser = None
    for token, name in [("edg", "Edge"), ("chrome", "Chrome"),
                        ("firefox", "Firefox"), ("safari", "Safari"),
                        ("opera", "Opera")]:
        if token in low:
            browser = name
            break

    os_name = None
    for token, name in [("windows", "Windows"), ("iphone", "iOS"),
                        ("ipad", "iPadOS"), ("mac os", "macOS"),
                        ("android", "Android"), ("linux", "Linux")]:
        if token in low:
            os_name = name
            break

    return {"device_kind": kind, "device_name": ua[:120] if ua else None,
            "ip": ip, "browser": browser, "os": os_name}


async def _log_activity(user_id: str, action: str,
                        request: Optional[Request] = None,
                        success: bool = True) -> None:
    try:
        meta = _request_meta(request)
        await database.execute(
            """
            INSERT INTO login_activity (
                user_id, action, device_kind, device_name, browser, os,
                ip, success, created_at
            ) VALUES (:uid, :action, :dk, :dn, :browser, :os_, :ip, :ok, :now)
            """,
            {"uid": user_id, "action": action,
             "dk": meta["device_kind"], "dn": meta["device_name"],
             "browser": meta["browser"], "os_": meta["os"],
             "ip": meta["ip"], "ok": success,
             "now": datetime.now(timezone.utc)},
        )
    except Exception as e:
        logger.warning("login_activity write failed: %s", e)


async def _register_session(user_id: str, request: Optional[Request] = None) -> str:
    session_id = str(uuid.uuid4())
    try:
        meta = _request_meta(request)
        now = datetime.now(timezone.utc)
        await database.execute(
            """
            INSERT INTO user_sessions (
                id, user_id, device_kind, device_name, browser, os,
                ip, last_active_at, created_at
            ) VALUES (:id, :uid, :dk, :dn, :browser, :os_, :ip, :now, :now)
            """,
            {"id": session_id, "uid": user_id,
             "dk": meta["device_kind"], "dn": meta["device_name"],
             "browser": meta["browser"], "os_": meta["os"],
             "ip": meta["ip"], "now": now},
        )
    except Exception as e:
        logger.warning("user_sessions write failed: %s", e)
    return session_id


def _normalize_phone(raw: Optional[str], region: Optional[str] = None) -> str:
    if not raw:
        return ""
    cleaned = raw.strip()
    if not cleaned:
        return ""
    effective_region = (region or DEFAULT_PHONE_REGION).upper()
    try:
        parsed = phonenumbers.parse(cleaned, effective_region)
    except phonenumbers.NumberParseException:
        return ""
    if not phonenumbers.is_valid_number(parsed):
        return ""
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _prehash(password: str) -> bytes:
    return hashlib.sha256(password.encode("utf-8")).digest()


def hash_password(password: str) -> str:
    """Synchronous bcrypt hash. Call via run_in_threadpool from async routes."""
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed) -> bool:
    """Synchronous bcrypt verify. Never raises — returns False on any error.

    A malformed or missing hash (None, non-bcrypt string, wrong scheme) must
    return False, not throw. Otherwise any corrupt row in `users` turns the
    login endpoint into a 500 machine.
    """
    if not isinstance(plain_password, str):
        return False
    if not isinstance(hashed, str) or not hashed:
        return False
    try:
        return bcrypt.checkpw(_prehash(plain_password), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _validate_password(pw: str, field_name: str = "Password") -> None:
    if not isinstance(pw, str):
        raise HTTPException(status_code=400, detail=f"{field_name} must be a string")
    if len(pw) < _MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be at least {_MIN_PASSWORD_LEN} characters",
        )
    if len(pw) > _MAX_PASSWORD_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be at most {_MAX_PASSWORD_LEN} characters",
        )


def create_access_token(data: dict, session_id: Optional[str] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    if session_id:
        to_encode["sid"] = session_id
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def generate_otp() -> str:
    """Cryptographically secure 6-digit OTP."""
    return str(secrets.randbelow(900_000) + 100_000)


def _is_otp_expired(expires_at_value) -> bool:
    if expires_at_value is None:
        return True
    if isinstance(expires_at_value, datetime):
        exp = expires_at_value
    else:
        try:
            exp = datetime.fromisoformat(str(expires_at_value))
        except (ValueError, TypeError):
            return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) > exp


async def invalidate_old_otps(phone: str, purpose: str) -> None:
    await database.execute(
        "UPDATE otp_codes SET used = 1 "
        "WHERE phone = :ph AND purpose = :pur AND used = 0",
        {"ph": phone, "pur": purpose},
    )


async def _dispatch_otp(phone: str, email: Optional[str], code: str, purpose: str) -> dict:
    delivered = {"email": False, "sms": False}

    async def send_email():
        if not email:
            return
        try:
            await run_in_threadpool(send_otp_email, email, code, purpose)
            delivered["email"] = True
        except Exception as e:
            logger.error("send_otp_email failed for %s: %s", purpose, e)

    async def send_sms():
        if not is_sms_configured():
            return
        try:
            ok = await send_otp_sms(phone, code, purpose)
            delivered["sms"] = ok
        except Exception as e:
            logger.error("send_otp_sms failed for %s: %s", purpose, e)

    await send_email()
    await send_sms()
    return delivered


def _public_user(user) -> dict:
    """Strip sensitive fields before returning to the client."""
    d = dict(user)
    return {k: v for k, v in d.items() if k not in _SENSITIVE_USER_KEYS}


# ═════════════════════════════════════════════════════════════
# Dependencies
# ═════════════════════════════════════════════════════════════
async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_exception

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        sid: str = payload.get("sid")
        if user_id is None or sid is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    # Session must still exist. Logout deletes the row; reset-password
    # deletes all rows for the user. Without this check, both are
    # cosmetic and a stolen token works until `exp` (24h).
    user = await database.fetch_one(
        """
        SELECT u.*
          FROM users u
          JOIN user_sessions s ON s.user_id = u.id
         WHERE u.id = :uid AND s.id = :sid
        """,
        {"uid": user_id, "sid": sid},
    )
    if user is None:
        raise credentials_exception

    # Best-effort last_active_at touch. Skip on error.
    try:
        await database.execute(
            """
            UPDATE user_sessions
               SET last_active_at = :now
             WHERE id = :sid
               AND (last_active_at IS NULL OR last_active_at < :stale)
            """,
            {
                "now": datetime.now(timezone.utc),
                "sid": sid,
                "stale": datetime.now(timezone.utc) - timedelta(minutes=5),
            },
        )
    except Exception:
        pass

    return dict(user)


async def get_optional_user(token: str = Depends(oauth2_scheme)) -> Optional[dict]:
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        sid: str = payload.get("sid")
        if user_id is None or sid is None:
            return None
        user = await database.fetch_one(
            """
            SELECT u.*
              FROM users u
              JOIN user_sessions s ON s.user_id = u.id
             WHERE u.id = :uid AND s.id = :sid
            """,
            {"uid": user_id, "sid": sid},
        )
        if user is None:
            return None
        return dict(user)
    except JWTError:
        return None


# ---------- Models ----------
class SignupRequest(BaseModel):
    phone: str
    password: str
    email: str
    username: Optional[str] = None
    phone_region: Optional[str] = None

class ForgotPasswordRequest(BaseModel):
    phone: str
    phone_region: Optional[str] = None

class ResetPasswordRequest(BaseModel):
    phone: str
    otp: str
    new_password: str
    phone_region: Optional[str] = None

class VerifyAccountRequest(BaseModel):
    phone: str
    otp: str
    phone_region: Optional[str] = None

class ResendVerificationRequest(BaseModel):
    phone: str
    phone_region: Optional[str] = None

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


# ---------- Routes ----------
@router.post("/signup")
async def signup(req: SignupRequest, request: Request):
    phone = _normalize_phone(req.phone, req.phone_region)
    if not phone:
        raise HTTPException(status_code=400, detail="Enter a valid phone number for your country.")

    _validate_password(req.password)

    email = (req.email or "").strip().lower()
    _rate_limit(f"signup:{phone}", max_calls=3, window_sec=3600)
    if request.client:
        _rate_limit(f"signup_ip:{request.client.host}", max_calls=10, window_sec=3600)

    existing = await database.fetch_one(
        "SELECT id FROM users WHERE phone = :ph", {"ph": phone}
    )
    if existing:
        raise HTTPException(status_code=400, detail="Phone already registered")

    if email:
        existing_email = await database.fetch_one(
            "SELECT id FROM users WHERE LOWER(email) = :em", {"em": email}
        )
        if existing_email:
            raise HTTPException(status_code=400, detail="Email already registered")

    user_id = uuid.uuid4().hex
    hashed = await run_in_threadpool(hash_password, req.password)

    await database.execute(
        "INSERT INTO users (id, phone, email, hashed_password, nickname, verified) "
        "VALUES (:id, :ph, :em, :pw, :nn, :verified)",
        {"id": user_id, "ph": phone, "em": email, "pw": hashed,
         "nn": req.username, "verified": False},
    )
    await database.execute(
        "INSERT INTO wallets (user_id, balance) VALUES (:uid, 0.0)",
        {"uid": user_id},
    )

    await invalidate_old_otps(phone, purpose="signup_verify")
    code = generate_otp()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    await database.execute(
        "INSERT INTO otp_codes (phone, code, purpose, expires_at, used) "
        "VALUES (:ph, :code, 'signup_verify', :exp, 0)",
        {"ph": phone, "code": code, "exp": expires_at},
    )

    delivered = await _dispatch_otp(phone, email, code, "signup_verify")
    if not delivered["email"] and not delivered["sms"]:
        logger.warning(
            "signup: no OTP channel succeeded for %s (email=%s sms=%s)",
            phone, delivered["email"], delivered["sms"],
        )
    return {
        "message": "Account created. Check your messages for the verification code.",
        "delivery": delivered,
    }


@router.post("/verify")
async def verify_account(req: VerifyAccountRequest, request: Request):
    phone = _normalize_phone(req.phone, req.phone_region)
    if not phone:
        raise HTTPException(status_code=400, detail="Invalid phone number")
    _rate_limit(f"verify:{phone}", max_calls=10, window_sec=900)

    otp_record = await database.fetch_one(
        "SELECT * FROM otp_codes WHERE phone = :ph AND used = 0 "
        "AND purpose IN ('signup_verify', 'reset_password') "
        "ORDER BY id DESC LIMIT 1",
        {"ph": phone},
    )
    if not otp_record:
        raise HTTPException(status_code=400, detail="No verification code requested")
    if _is_otp_expired(otp_record["expires_at"]):
        raise HTTPException(status_code=400, detail="Verification code expired")

    stored_code = otp_record["code"] or ""
    provided_code = req.otp or ""
    if not hmac.compare_digest(stored_code, provided_code):
        raise HTTPException(status_code=400, detail="Invalid verification code")

    purpose = otp_record["purpose"]
    if purpose == "signup_verify":
        await database.execute(
            "UPDATE otp_codes SET used = 1 WHERE id = :id",
            {"id": otp_record["id"]},
        )
        await database.execute(
            "UPDATE users SET verified = True WHERE phone = :ph",
            {"ph": phone},
        )
        user = await database.fetch_one(
            "SELECT * FROM users WHERE phone = :ph", {"ph": phone}
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        await _log_activity(user["id"], "login", request, success=True)
        session_id = await _register_session(user["id"], request)
        token = create_access_token(
            {"sub": user["id"], "phone": user["phone"]},
            session_id=session_id,
        )
        return {"access_token": token, "token_type": "bearer",
                "user_id": user["id"], "purpose": "signup_verify"}

    return {"verified": True, "purpose": "reset_password",
            "message": "OTP verified. Continue to set a new password."}


@router.post("/resend-verification")
async def resend_verification(req: ResendVerificationRequest):
    phone = _normalize_phone(req.phone, req.phone_region)
    if not phone:
        raise HTTPException(status_code=400, detail="Invalid phone number")
    _rate_limit(f"resend:{phone}", max_calls=3, window_sec=900)

    user = await database.fetch_one(
        "SELECT * FROM users WHERE phone = :ph", {"ph": phone}
    )
    if not user:
        return {"message": "If this phone is registered, a new code has been sent."}
    if user["verified"]:
        return {"message": "Account already verified. Please log in."}

    await invalidate_old_otps(phone, purpose="signup_verify")
    code = generate_otp()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    await database.execute(
        "INSERT INTO otp_codes (phone, code, purpose, expires_at, used) "
        "VALUES (:ph, :code, 'signup_verify', :exp, 0)",
        {"ph": phone, "code": code, "exp": expires_at},
    )
    await _dispatch_otp(phone, user["email"], code, "signup_verify")
    return {"message": "If this phone is registered, a new code has been sent."}


@router.post("/login")
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    raw_id = (form_data.username or "").strip()
    if "@" in raw_id:
        identifier = raw_id.lower()
    else:
        identifier = _normalize_phone(raw_id)
        if not identifier:
            raise HTTPException(status_code=401, detail="Invalid credentials")

    _rate_limit(f"login:{identifier}", max_calls=10, window_sec=900)
    if request.client:
        _rate_limit(f"login_ip:{request.client.host}", max_calls=50, window_sec=900)

    user = await database.fetch_one(
        "SELECT * FROM users WHERE phone = :login OR LOWER(email) = :login",
        {"login": identifier},
    )
    if not user:
        # Same 401 as bad password — no enumeration oracle.
        raise HTTPException(status_code=401, detail="Invalid credentials")

    ok = await run_in_threadpool(verify_password, form_data.password, user["hashed_password"])
    if not ok:
        await _log_activity(user["id"], "login", request, success=False)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not user["verified"]:
        # Only reveal unverified AFTER a correct password.
        raise HTTPException(
            status_code=403,
            detail="Account not verified. Check your messages for the code.",
        )

    await _log_activity(user["id"], "login", request, success=True)
    session_id = await _register_session(user["id"], request)
    token = create_access_token(
        {"sub": user["id"], "phone": user["phone"]},
        session_id=session_id,
    )
    return {"access_token": token, "token_type": "bearer", "user_id": user["id"]}


@router.post("/logout")
async def logout(request: Request, current_user: dict = Depends(get_current_user)):
    sid = None
    try:
        auth_header = request.headers.get("authorization", "")
        token = auth_header.split(" ", 1)[1] if " " in auth_header else ""
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sid = payload.get("sid")
    except Exception:
        sid = None

    if sid:
        try:
            await database.execute(
                "DELETE FROM user_sessions WHERE id = :sid AND user_id = :uid",
                {"sid": sid, "uid": current_user["id"]},
            )
        except Exception as e:
            logger.warning("logout session delete failed: %s", e)
    else:
        # No sid → do NOT guess. Deleting the "most recent" session would
        # kill a different device's login.
        logger.warning("logout called without sid for user %s", current_user["id"])

    await _log_activity(current_user["id"], "logout", request, success=True)
    return {"message": "Logged out"}


@router.post("/forgot-password")
async def forgot_password(req: ForgotPasswordRequest):
    phone = _normalize_phone(req.phone, req.phone_region)
    if not phone:
        raise HTTPException(status_code=400, detail="Invalid phone number")
    _rate_limit(f"forgot:{phone}", max_calls=3, window_sec=900)

    user = await database.fetch_one(
        "SELECT * FROM users WHERE phone = :ph", {"ph": phone}
    )
    if not user:
        return {"message": "If this phone is registered, an OTP has been sent."}

    await invalidate_old_otps(phone, purpose="reset_password")
    code = generate_otp()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    await database.execute(
        "INSERT INTO otp_codes (phone, code, purpose, expires_at, used) "
        "VALUES (:ph, :code, 'reset_password', :exp, 0)",
        {"ph": phone, "code": code, "exp": expires_at},
    )
    await _dispatch_otp(phone, user["email"], code, "reset_password")
    return {"message": "If this phone is registered, an OTP has been sent."}


@router.post("/reset-password")
async def reset_password(req: ResetPasswordRequest, request: Request):
    phone = _normalize_phone(req.phone, req.phone_region)
    if not phone:
        raise HTTPException(status_code=400, detail="Invalid phone number")
    _rate_limit(f"reset:{phone}", max_calls=5, window_sec=900)

    _validate_password(req.new_password, "New password")

    otp_record = await database.fetch_one(
        "SELECT * FROM otp_codes WHERE phone = :ph AND purpose = 'reset_password' "
        "AND used = 0 ORDER BY id DESC LIMIT 1",
        {"ph": phone},
    )
    if not otp_record:
        raise HTTPException(status_code=400, detail="No OTP requested")
    if _is_otp_expired(otp_record["expires_at"]):
        raise HTTPException(status_code=400, detail="OTP expired")

    stored_code = otp_record["code"] or ""
    provided_code = req.otp or ""
    if not hmac.compare_digest(stored_code, provided_code):
        raise HTTPException(status_code=400, detail="Invalid OTP")

    await database.execute(
        "UPDATE otp_codes SET used = 1 WHERE id = :id",
        {"id": otp_record["id"]},
    )

    new_hashed = await run_in_threadpool(hash_password, req.new_password)
    await database.execute(
        "UPDATE users SET hashed_password = :pw WHERE phone = :ph",
        {"pw": new_hashed, "ph": phone},
    )

    user = await database.fetch_one(
        "SELECT id FROM users WHERE phone = :ph", {"ph": phone}
    )
    if user:
        await _log_activity(user["id"], "password_change", request, success=True)
        # Purge every session for this user. Requires get_current_user
        # to actually check user_sessions — it now does.
        try:
            await database.execute(
                "DELETE FROM user_sessions WHERE user_id = :uid",
                {"uid": user["id"]},
            )
        except Exception as e:
            logger.warning("session purge after reset failed: %s", e)

    return {"message": "Password has been reset successfully."}


@router.post("/change-password")
async def change_password(
    req: ChangePasswordRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user["id"]
    _validate_password(req.new_password, "New password")

    if req.current_password == req.new_password:
        raise HTTPException(
            status_code=400,
            detail="New password must be different from current password",
        )

    user = await database.fetch_one(
        "SELECT id, hashed_password FROM users WHERE id = :uid", {"uid": user_id}
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    ok = await run_in_threadpool(verify_password, req.current_password, user["hashed_password"])
    if not ok:
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    new_hashed = await run_in_threadpool(hash_password, req.new_password)
    await database.execute(
        "UPDATE users SET hashed_password = :pw WHERE id = :uid",
        {"pw": new_hashed, "uid": user_id},
    )
    await _log_activity(user_id, "password_change", request, success=True)

    # Change-password is not a compromise event the way reset is — but
    # optionally you'd purge other sessions here too. Left as-is for now
    # to preserve existing UX (user stays logged in on this device).
    return {"message": "Password updated successfully"}


@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    return _public_user(current_user)