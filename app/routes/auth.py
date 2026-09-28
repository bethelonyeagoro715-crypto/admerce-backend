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
import phonenumbers

from app.db.database import database
from app.services.email_service import send_otp_email
from app.services.sms_service import send_otp_sms, is_sms_configured

logger = logging.getLogger("auth")

router = APIRouter(prefix="/auth", tags=["Authentication"])

# ── SECRET_KEY from environment ─────────────────────────────────────
SECRET_KEY = os.getenv("JWT_SECRET")
if not SECRET_KEY:
    _env = os.getenv("ENVIRONMENT", "development").lower()
    if _env in ("production", "prod"):
        raise RuntimeError(
            "JWT_SECRET environment variable is required in production."
        )
    SECRET_KEY = "dev-only-insecure-secret-change-me"
    logger.warning("JWT_SECRET not set — insecure development fallback active.")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

# Default region used when a number has no leading '+'. Nigeria is the
# launch market; override per-request via `phone_region`.
DEFAULT_PHONE_REGION = os.getenv("DEFAULT_PHONE_REGION", "NG")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

# ── Rate limiting (in-memory; fine on 1 worker) ─────────────────────
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


# ── Phone canonicalization (any country) ────────────────────────────
def _normalize_phone(raw: Optional[str], region: Optional[str] = None) -> str:
    """
    Parse any phone number from any country into E.164 (+CCC...).
    Returns "" if unparseable or not a valid number for its region.

    `region` is an ISO 3166-1 alpha-2 code ("NG", "US", "GB"…). When
    omitted, DEFAULT_PHONE_REGION is used for numbers lacking a `+`.
    Numbers that already start with `+` ignore the region entirely.
    """
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
    return phonenumbers.format_number(
        parsed, phonenumbers.PhoneNumberFormat.E164
    )


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
    await database.execute(
        "UPDATE otp_codes SET used = 1 "
        "WHERE phone = :ph AND purpose = :pur AND used = 0",
        {"ph": phone, "pur": purpose},
    )


async def _dispatch_otp(phone: str, email: Optional[str], code: str, purpose: str) -> dict:
    """
    Send the OTP over every configured channel. Both run concurrently
    (email in a threadpool because SMTP is sync; SMS is already async).
    Returns a small dict describing which channels were attempted.
    """
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

    # Await sequentially — email via threadpool, SMS via httpx. Both
    # non-blocking, so total wall time is one call's worth.
    await send_email()
    await send_sms()
    return delivered


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
    phone_region: Optional[str] = None  # ISO-3166 alpha-2, e.g. "US"


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
async def signup(req: SignupRequest):
    phone = _normalize_phone(req.phone, req.phone_region)
    if not phone:
        raise HTTPException(
            status_code=400,
            detail="Enter a valid phone number for your country.",
        )
    email = (req.email or "").strip().lower()

    _rate_limit(f"signup:{phone}", max_calls=3, window_sec=3600)

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
            "ph": phone,
            "em": email,
            "pw": hashed,
            "nn": req.username,
            "verified": False,
        },
    )
    await database.execute(
        "INSERT INTO wallets (user_id, balance) VALUES (:uid, 0.0)",
        {"uid": user_id},
    )

    await invalidate_old_otps(phone, purpose="signup_verify")
    code = generate_otp()
    expires_at = datetime.utcnow() + timedelta(minutes=10)

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
async def verify_account(req: VerifyAccountRequest):
    phone = _normalize_phone(req.phone, req.phone_region)
    if not phone:
        raise HTTPException(status_code=400, detail="Invalid phone number")
    _rate_limit(f"verify:{phone}", max_calls=10, window_sec=900)

    otp_record = await database.fetch_one(
        "SELECT * FROM otp_codes "
        "WHERE phone = :ph AND used = 0 "
        "AND purpose IN ('signup_verify', 'reset_password') "
        "ORDER BY id DESC LIMIT 1",
        {"ph": phone},
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
            {"ph": phone},
        )
        user = await database.fetch_one(
            "SELECT * FROM users WHERE phone = :ph", {"ph": phone}
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        token = create_access_token({"sub": user["id"], "phone": user["phone"]})
        return {
            "access_token": token,
            "token_type": "bearer",
            "user_id": user["id"],
            "purpose": "signup_verify",
        }

    return {
        "verified": True,
        "purpose": "reset_password",
        "message": "OTP verified. Continue to set a new password.",
    }


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
    expires_at = datetime.utcnow() + timedelta(minutes=10)

    await database.execute(
        "INSERT INTO otp_codes (phone, code, purpose, expires_at, used) "
        "VALUES (:ph, :code, 'signup_verify', :exp, 0)",
        {"ph": phone, "code": code, "exp": expires_at},
    )

    email = user["email"]
    await _dispatch_otp(phone, email, code, "signup_verify")

    return {"message": "If this phone is registered, a new code has been sent."}


@router.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    raw_id = (form_data.username or "").strip()
    if "@" in raw_id:
        identifier = raw_id.lower()
    else:
        identifier = _normalize_phone(raw_id)
        if not identifier:
            raise HTTPException(status_code=401, detail="Invalid credentials")

    _rate_limit(f"login:{identifier}", max_calls=10, window_sec=900)

    user = await database.fetch_one(
        "SELECT * FROM users WHERE phone = :login OR LOWER(email) = :login",
        {"login": identifier},
    )
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user["verified"]:
        raise HTTPException(
            status_code=403,
            detail="Account not verified. Check your messages for the code.",
        )
    if not verify_password(form_data.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({"sub": user["id"], "phone": user["phone"]})
    return {"access_token": token, "token_type": "bearer", "user_id": user["id"]}


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
    expires_at = datetime.utcnow() + timedelta(minutes=10)

    await database.execute(
        "INSERT INTO otp_codes (phone, code, purpose, expires_at, used) "
        "VALUES (:ph, :code, 'reset_password', :exp, 0)",
        {"ph": phone, "code": code, "exp": expires_at},
    )

    email = user["email"]
    await _dispatch_otp(phone, email, code, "reset_password")

    return {"message": "If this phone is registered, an OTP has been sent."}


@router.post("/reset-password")
async def reset_password(req: ResetPasswordRequest):
    phone = _normalize_phone(req.phone, req.phone_region)
    if not phone:
        raise HTTPException(status_code=400, detail="Invalid phone number")
    _rate_limit(f"reset:{phone}", max_calls=5, window_sec=900)

    if len(req.new_password) < 6:
        raise HTTPException(
            status_code=400,
            detail="New password must be at least 6 characters",
        )

    otp_record = await database.fetch_one(
        "SELECT * FROM otp_codes WHERE phone = :ph AND purpose = 'reset_password' "
        "AND used = 0 ORDER BY id DESC LIMIT 1",
        {"ph": phone},
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
        {"pw": new_hashed, "ph": phone},
    )
    return {"message": "Password has been reset successfully."}


# ── /auth/reset-password-direct — REMOVED (account-takeover vuln) ────


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