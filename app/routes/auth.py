import hashlib
import bcrypt
import random
import os
import time
import logging
from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from jose import jwt, JWTError
from datetime import datetime, timedelta
import uuid
from typing import Optional
from app.db.database import database
from app.services.email_service import send_otp_email

logger = logging.getLogger("auth")

router = APIRouter(prefix="/auth", tags=["Authentication"])

# ── SECRET_KEY from environment ─────────────────────────────────────
# Never hardcode. Production refuses to boot without JWT_SECRET.
SECRET_KEY = os.getenv("JWT_SECRET")
if not SECRET_KEY:
    _env = os.getenv("ENVIRONMENT", "development").lower()
    if _env in ("production", "prod"):
        raise RuntimeError(
            "JWT_SECRET environment variable is required in production. "
            "Generate one with `openssl rand -hex 32` and set it on Render."
        )
    SECRET_KEY = "dev-only-insecure-secret-change-me"
    logger.warning(
        "JWT_SECRET not set — using insecure development fallback. "
        "Do not deploy this configuration to production."
    )

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

# ── Naive in-memory rate limiting ────────────────────────────────────
# Fine for a single-worker deploy. Does NOT survive restarts and does
# NOT work across multiple workers. Replace with Redis or a Postgres-
# backed limiter when scaling past 1 instance.
_rate_buckets: dict[str, list[float]] = {}

def _rate_limit(key: str, max_calls: int, window_sec: int) -> None:
    """Raise 429 if `key` has exceeded `max_calls` within `window_sec`."""
    now = time.time()
    bucket = _rate_buckets.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < window_sec]
    if len(bucket) >= max_calls:
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Please try again in a few minutes.",
        )
    bucket.append(now)
    # Opportunistic cleanup so the dict can't grow without bound.
    if len(_rate_buckets) > 10_000:
        cutoff = now - 3600
        for k in list(_rate_buckets.keys()):
            _rate_buckets[k] = [t for t in _rate_buckets[k] if t > cutoff]
            if not _rate_buckets[k]:
                del _rate_buckets[k]


def _prehash(password: str) -> bytes:
    return hashlib.sha256(password.encode("utf-8")).digest()


def hash_password(password: str) -> str:
    pwhash = bcrypt.hashpw(_prehash(password), bcrypt.gensalt())
    return pwhash.decode("utf-8")


def verify_password(plain_password: str, hashed: str) -> bool:
    return bcrypt.checkpw(_prehash(plain_password), hashed.encode("utf-8"))


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def generate_otp() -> str:
    return str(random.randint(100000, 999999))


def _is_otp_expired(expires_at_value) -> bool:
    """
    Determine if an OTP has expired. Handles both str (ISO 8601) and
    datetime values — the column has been populated both ways over time.
    Falls back to "expired" if the value can't be parsed.
    """
    if expires_at_value is None:
        return True
    if isinstance(expires_at_value, datetime):
        exp = expires_at_value
    else:
        try:
            exp = datetime.fromisoformat(str(expires_at_value))
        except (ValueError, TypeError):
            return True
    if exp.tzinfo is not None:
        exp = exp.replace(tzinfo=None)
    return datetime.utcnow() > exp


async def invalidate_old_otps(phone: str, purpose: str) -> None:
    """Mark all unused OTPs for (phone, purpose) as used."""
    await database.execute(
        "UPDATE otp_codes SET used = 1 WHERE phone = :ph AND purpose = :pur AND used = 0",
        {"ph": phone, "pur": purpose},
    )


async def _send_otp_async(email: str, code: str, purpose: str) -> None:
    """
    Fire-and-forget OTP email. Wrapped in a threadpool because the
    underlying SMTP client is synchronous and would otherwise block the
    event loop for 1-3 seconds per send.
    """
    try:
        await run_in_threadpool(send_otp_email, email, code, purpose)
    except Exception as e:
        # Log the failure but do not leak the OTP.
        logger.error("send_otp_email failed for %s: %s", purpose, e)


# ---------- Dependencies ----------
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
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = await database.fetch_one(
        "SELECT id, phone, email, nickname, verified, role, avatar_url, "
        "business_image_url, business_name FROM users WHERE id = :uid",
        {"uid": user_id},
    )
    if user is None:
        raise credentials_exception
    return dict(user)


async def get_optional_user(
    token: str = Depends(oauth2_scheme),
) -> Optional[dict]:
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            return None
        user = await database.fetch_one(
            "SELECT id, phone, email, nickname, verified, role, avatar_url, "
            "business_image_url, business_name FROM users WHERE id = :uid",
            {"uid": user_id},
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


class ForgotPasswordRequest(BaseModel):
    phone: str


class ResetPasswordRequest(BaseModel):
    phone: str
    otp: str
    new_password: str


class VerifyAccountRequest(BaseModel):
    phone: str
    otp: str


class ResendVerificationRequest(BaseModel):
    phone: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


# ---------- Routes ----------
@router.post("/signup")
async def signup(req: SignupRequest):
    # 3 signups per phone per hour.
    _rate_limit(f"signup:{req.phone}", max_calls=3, window_sec=3600)

    existing = await database.fetch_one(
        "SELECT id FROM users WHERE phone = :ph", {"ph": req.phone}
    )
    if existing:
        raise HTTPException(status_code=400, detail="Phone already registered")

    if req.email:
        existing_email = await database.fetch_one(
            "SELECT id FROM users WHERE email = :em", {"em": req.email}
        )
        if existing_email:
            raise HTTPException(
                status_code=400, detail="Email already registered"
            )

    user_id = uuid.uuid4().hex
    hashed = hash_password(req.password)

    await database.execute(
        "INSERT INTO users (id, phone, email, hashed_password, nickname, verified) "
        "VALUES (:id, :ph, :em, :pw, :nn, :verified)",
        {
            "id": user_id,
            "ph": req.phone,
            "em": req.email,
            "pw": hashed,
            "nn": req.username,
            "verified": False,
        },
    )
    await database.execute(
        "INSERT INTO wallets (user_id, balance) VALUES (:uid, 0.0)",
        {"uid": user_id},
    )

    await invalidate_old_otps(req.phone, purpose="signup_verify")
    code = generate_otp()
    expires_at = datetime.utcnow() + timedelta(minutes=10)

    await database.execute(
        "INSERT INTO otp_codes (phone, code, purpose, expires_at, used) "
        "VALUES (:ph, :code, 'signup_verify', :exp, 0)",
        {"ph": req.phone, "code": code, "exp": expires_at},
    )

    if req.email:
        await _send_otp_async(req.email, code, "signup_verify")
    else:
        logger.warning(
            "signup: no email for %s — OTP created but not deliverable",
            req.phone,
        )

    return {"message": "Account created. Check your email for the verification code."}


@router.post("/verify")
async def verify_account(req: VerifyAccountRequest):
    # 10 verification attempts per phone per 15 min.
    _rate_limit(f"verify:{req.phone}", max_calls=10, window_sec=900)

    otp_record = await database.fetch_one(
        "SELECT * FROM otp_codes "
        "WHERE phone = :ph AND used = 0 "
        "AND purpose IN ('signup_verify', 'reset_password') "
        "ORDER BY id DESC LIMIT 1",
        {"ph": req.phone},
    )
    if not otp_record:
        raise HTTPException(status_code=400, detail="No verification code requested")
    if _is_otp_expired(otp_record["expires_at"]):
        raise HTTPException(status_code=400, detail="Verification code expired")
    if otp_record["code"] != req.otp:
        raise HTTPException(status_code=400, detail="Invalid verification code")

    purpose = otp_record["purpose"]

    if purpose == "signup_verify":
        await database.execute(
            "UPDATE otp_codes SET used = 1 WHERE id = :id",
            {"id": otp_record["id"]},
        )
        await database.execute(
            "UPDATE users SET verified = True WHERE phone = :ph",
            {"ph": req.phone},
        )
        user = await database.fetch_one(
            "SELECT * FROM users WHERE phone = :ph", {"ph": req.phone}
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        token = create_access_token(
            {"sub": user["id"], "phone": user["phone"]}
        )
        return {
            "access_token": token,
            "token_type": "bearer",
            "user_id": user["id"],
            "purpose": "signup_verify",
        }

    # Reset-password flow: leave the OTP unused so /auth/reset-password
    # can consume it in the next step. Do not issue a JWT.
    return {
        "verified": True,
        "purpose": "reset_password",
        "message": "OTP verified. Continue to set a new password.",
    }


@router.post("/resend-verification")
async def resend_verification(req: ResendVerificationRequest):
    # 3 resends per phone per 15 min.
    _rate_limit(f"resend:{req.phone}", max_calls=3, window_sec=900)

    user = await database.fetch_one(
        "SELECT * FROM users WHERE phone = :ph", {"ph": req.phone}
    )
    if not user:
        return {"message": "If this phone is registered, a new code has been sent."}

    if user["verified"]:
        return {"message": "Account already verified. Please log in."}

    await invalidate_old_otps(req.phone, purpose="signup_verify")
    code = generate_otp()
    expires_at = datetime.utcnow() + timedelta(minutes=10)

    await database.execute(
        "INSERT INTO otp_codes (phone, code, purpose, expires_at, used) "
        "VALUES (:ph, :code, 'signup_verify', :exp, 0)",
        {"ph": req.phone, "code": code, "exp": expires_at},
    )

    email = user["email"]
    if email:
        await _send_otp_async(email, code, "signup_verify")
    else:
        logger.warning(
            "resend: no email on file for %s — OTP created but not deliverable",
            req.phone,
        )

    return {"message": "If this phone is registered, a new code has been sent."}


@router.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    # 10 login attempts per identifier per 15 min.
    _rate_limit(f"login:{form_data.username}", max_calls=10, window_sec=900)

    user = await database.fetch_one(
        "SELECT * FROM users WHERE phone = :login OR email = :login",
        {"login": form_data.username},
    )
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user["verified"]:
        raise HTTPException(
            status_code=403,
            detail="Account not verified. Check your email for the code.",
        )
    if not verify_password(form_data.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({"sub": user["id"], "phone": user["phone"]})
    return {"access_token": token, "token_type": "bearer", "user_id": user["id"]}


@router.post("/forgot-password")
async def forgot_password(req: ForgotPasswordRequest):
    # 3 requests per phone per 15 min.
    _rate_limit(f"forgot:{req.phone}", max_calls=3, window_sec=900)

    user = await database.fetch_one(
        "SELECT * FROM users WHERE phone = :ph", {"ph": req.phone}
    )
    if not user:
        return {"message": "If this phone is registered, an OTP has been sent."}

    await invalidate_old_otps(req.phone, purpose="reset_password")
    code = generate_otp()
    expires_at = datetime.utcnow() + timedelta(minutes=10)

    await database.execute(
        "INSERT INTO otp_codes (phone, code, purpose, expires_at, used) "
        "VALUES (:ph, :code, 'reset_password', :exp, 0)",
        {"ph": req.phone, "code": code, "exp": expires_at},
    )

    email = user["email"]
    if email:
        await _send_otp_async(email, code, "reset_password")
    else:
        logger.warning(
            "forgot-password: no email on file for %s — OTP created but not deliverable",
            req.phone,
        )

    return {"message": "If this phone is registered, an OTP has been sent."}


@router.post("/reset-password")
async def reset_password(req: ResetPasswordRequest):
    # 5 attempts per phone per 15 min.
    _rate_limit(f"reset:{req.phone}", max_calls=5, window_sec=900)

    if len(req.new_password) < 6:
        raise HTTPException(
            status_code=400,
            detail="New password must be at least 6 characters",
        )

    otp_record = await database.fetch_one(
        "SELECT * FROM otp_codes WHERE phone = :ph AND purpose = 'reset_password' "
        "AND used = 0 ORDER BY id DESC LIMIT 1",
        {"ph": req.phone},
    )
    if not otp_record:
        raise HTTPException(status_code=400, detail="No OTP requested")
    if _is_otp_expired(otp_record["expires_at"]):
        raise HTTPException(status_code=400, detail="OTP expired")
    if otp_record["code"] != req.otp:
        raise HTTPException(status_code=400, detail="Invalid OTP")

    await database.execute(
        "UPDATE otp_codes SET used = 1 WHERE id = :id",
        {"id": otp_record["id"]},
    )
    new_hashed = hash_password(req.new_password)
    await database.execute(
        "UPDATE users SET hashed_password = :pw WHERE phone = :ph",
        {"pw": new_hashed, "ph": req.phone},
    )
    return {"message": "Password has been reset successfully."}


# ── /auth/reset-password-direct — REMOVED ───────────────────────────
# This endpoint accepted { phone, new_password } with no OTP and
# allowed unauthenticated password reset for any phone number in the
# system. That is a full account-takeover vulnerability. It has been
# deleted deliberately, not by accident. If a frontend caller needs a
# fast-path reset, it must be built behind an admin-authenticated
# dependency, not phone-only.


@router.post("/change-password")
async def change_password(
    req: ChangePasswordRequest,
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user["id"]

    if len(req.new_password) < 6:
        raise HTTPException(
            status_code=400,
            detail="New password must be at least 6 characters",
        )
    if req.current_password == req.new_password:
        raise HTTPException(
            status_code=400,
            detail="New password must be different from current password",
        )

    user = await database.fetch_one(
        "SELECT id, hashed_password FROM users WHERE id = :uid",
        {"uid": user_id},
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not verify_password(req.current_password, user["hashed_password"]):
        raise HTTPException(
            status_code=400,
            detail="Current password is incorrect",
        )

    new_hashed = hash_password(req.new_password)
    await database.execute(
        "UPDATE users SET hashed_password = :pw WHERE id = :uid",
        {"pw": new_hashed, "uid": user_id},
    )

    return {"message": "Password updated successfully"}


@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    return current_user