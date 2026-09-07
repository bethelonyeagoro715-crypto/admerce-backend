import hashlib
import bcrypt
import random
from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
from jose import jwt, JWTError
from datetime import datetime, timedelta
import uuid
from typing import Optional
from app.db.database import database
from app.services.email_service import send_otp_email

router = APIRouter(prefix="/auth", tags=["Authentication"])

SECRET_KEY = "your-secret-key-keep-it-safe"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

def _prehash(password: str) -> bytes:
    return hashlib.sha256(password.encode("utf-8")).digest()

def hash_password(password: str) -> str:
    pwhash = bcrypt.hashpw(_prehash(password), bcrypt.gensalt())
    return pwhash.decode("utf-8")

def verify_password(plain_password: str, hashed: str) -> bool:
    return bcrypt.checkpw(_prehash(plain_password), hashed.encode("utf-8"))

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def generate_otp() -> str:
    return str(random.randint(100000, 999999))

async def invalidate_old_otps(phone: str, purpose: str = "reset_password"):
    await database.execute(
        "UPDATE otp_codes SET used = 1 WHERE phone = :ph AND purpose = :pur AND used = 0",
        {"ph": phone, "pur": purpose}
    )

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
        "SELECT id, phone, email, nickname, verified, role, avatar_url, business_image_url, business_name FROM users WHERE id = :uid",
        {"uid": user_id}
    )
    if user is None:
        raise credentials_exception
    return dict(user)

async def get_optional_user(token: str = Depends(oauth2_scheme)) -> Optional[dict]:
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            return None
        user = await database.fetch_one(
            "SELECT id, phone, email, nickname, verified, role, avatar_url, business_image_url, business_name FROM users WHERE id = :uid",
            {"uid": user_id}
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
    username: str = None

class DirectResetRequest(BaseModel):
    phone: str
    new_password: str

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

# ---------- Routes ----------
@router.post("/signup")
async def signup(req: SignupRequest):
    existing = await database.fetch_one("SELECT * FROM users WHERE phone = :ph", {"ph": req.phone})
    if existing:
        raise HTTPException(status_code=400, detail="Phone already registered")

    if req.email:
        existing_email = await database.fetch_one("SELECT * FROM users WHERE email = :em", {"em": req.email})
        if existing_email:
            raise HTTPException(status_code=400, detail="Email already registered")

    user_id = uuid.uuid4().hex
    hashed = hash_password(req.password)

    # ✅ Use False instead of 0 for boolean column
    await database.execute(
        "INSERT INTO users (id, phone, email, hashed_password, nickname, verified) "
        "VALUES (:id, :ph, :em, :pw, :nn, :verified)",
        {"id": user_id, "ph": req.phone, "em": req.email, "pw": hashed, "nn": req.username, "verified": False}
    )
    await database.execute("INSERT INTO wallets (user_id, balance) VALUES (:uid, 0.0)", {"uid": user_id})

    await invalidate_old_otps(req.phone, purpose="signup_verify")
    code = generate_otp()
    expires_at = datetime.utcnow() + timedelta(minutes=10)

    await database.execute(
        "INSERT INTO otp_codes (phone, code, purpose, expires_at, used) "
        "VALUES (:ph, :code, 'signup_verify', :exp, 0)",
        {"ph": req.phone, "code": code, "exp": expires_at.isoformat()}
    )

    if req.email:
        try:
            send_otp_email(req.email, code, "signup_verify")
        except Exception as e:
            print(f"⚠️ Failed to send OTP email: {e}")
            print(f"📱 OTP for {req.phone}: {code}")
    else:
        print(f"⚠️ No email for {req.phone}, OTP is {code}")

    return {"message": "Account created. Check your email for the verification code."}

@router.post("/verify")
async def verify_account(req: VerifyAccountRequest):
    otp_record = await database.fetch_one(
        "SELECT * FROM otp_codes WHERE phone = :ph AND purpose = 'signup_verify' AND used = 0 "
        "ORDER BY id DESC LIMIT 1",
        {"ph": req.phone}
    )
    if not otp_record:
        raise HTTPException(status_code=400, detail="No verification code requested")
    if otp_record["expires_at"] < datetime.utcnow().isoformat():
        raise HTTPException(status_code=400, detail="Verification code expired")
    if otp_record["code"] != req.otp:
        raise HTTPException(status_code=400, detail="Invalid verification code")

    await database.execute("UPDATE otp_codes SET used = 1 WHERE id = :id", {"id": otp_record["id"]})
    # ✅ Use True instead of 1 for boolean column
    await database.execute("UPDATE users SET verified = True WHERE phone = :ph", {"ph": req.phone})

    user = await database.fetch_one("SELECT * FROM users WHERE phone = :ph", {"ph": req.phone})
    token = create_access_token({"sub": user["id"], "phone": user["phone"]})
    return {"access_token": token, "token_type": "bearer", "user_id": user["id"]}

@router.post("/resend-verification")
async def resend_verification(req: ResendVerificationRequest):
    user = await database.fetch_one("SELECT * FROM users WHERE phone = :ph", {"ph": req.phone})
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
        {"ph": req.phone, "code": code, "exp": expires_at.isoformat()}
    )

    email = user.get("email")
    if email:
        try:
            send_otp_email(email, code, "signup_verify")
        except Exception as e:
            print(f"⚠️ Failed to resend OTP email: {e}")
            print(f"📱 OTP for {req.phone}: {code}")
    else:
        print(f"⚠️ No email for {req.phone}, OTP is {code}")

    return {"message": "If this phone is registered, a new code has been sent."}

@router.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = await database.fetch_one(
        "SELECT * FROM users WHERE phone = :login OR email = :login",
        {"login": form_data.username}
    )
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user["verified"]:
        raise HTTPException(status_code=403, detail="Account not verified. Check your email for the code.")
    if not verify_password(form_data.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({"sub": user["id"], "phone": user["phone"]})
    return {"access_token": token, "token_type": "bearer", "user_id": user["id"]}

@router.post("/forgot-password")
async def forgot_password(req: ForgotPasswordRequest):
    user = await database.fetch_one("SELECT * FROM users WHERE phone = :ph", {"ph": req.phone})
    if not user:
        return {"message": "If this phone is registered, an OTP has been sent."}

    await invalidate_old_otps(req.phone, purpose="reset_password")
    code = generate_otp()
    expires_at = datetime.utcnow() + timedelta(minutes=10)

    await database.execute(
        "INSERT INTO otp_codes (phone, code, purpose, expires_at, used) "
        "VALUES (:ph, :code, 'reset_password', :exp, 0)",
        {"ph": req.phone, "code": code, "exp": expires_at.isoformat()}
    )

    email = user.get("email")
    if email:
        try:
            send_otp_email(email, code, "reset_password")
        except Exception as e:
            print(f"⚠️ Failed to send OTP email: {e}")
            print(f"📱 OTP for {req.phone}: {code}")
    else:
        print(f"⚠️ No email for {req.phone}, OTP is {code}")

    return {"message": "If this phone is registered, an OTP has been sent."}

@router.post("/reset-password")
async def reset_password(req: ResetPasswordRequest):
    otp_record = await database.fetch_one(
        "SELECT * FROM otp_codes WHERE phone = :ph AND purpose = 'reset_password' AND used = 0 "
        "ORDER BY id DESC LIMIT 1",
        {"ph": req.phone}
    )
    if not otp_record:
        raise HTTPException(status_code=400, detail="No OTP requested")
    if otp_record["expires_at"] < datetime.utcnow().isoformat():
        raise HTTPException(status_code=400, detail="OTP expired")
    if otp_record["code"] != req.otp:
        raise HTTPException(status_code=400, detail="Invalid OTP")

    await database.execute("UPDATE otp_codes SET used = 1 WHERE id = :id", {"id": otp_record["id"]})
    new_hashed = hash_password(req.new_password)
    await database.execute("UPDATE users SET hashed_password = :pw WHERE phone = :ph", {"pw": new_hashed, "ph": req.phone})
    return {"message": "Password has been reset successfully."}

@router.post("/reset-password-direct")
async def reset_password_direct(req: DirectResetRequest):
    user = await database.fetch_one("SELECT * FROM users WHERE phone = :ph", {"ph": req.phone})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    new_hashed = hash_password(req.new_password)
    await database.execute("UPDATE users SET hashed_password = :pw WHERE phone = :ph", {"pw": new_hashed, "ph": req.phone})
    return {"message": "Password updated successfully"}

@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    return current_user