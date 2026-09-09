from fastapi import APIRouter, HTTPException, Depends, File, UploadFile, Request
from pydantic import BaseModel
from typing import Dict, Any, Optional
import json
import os
import uuid
import asyncio
from app.db.database import database
from app.utils.security import get_current_user
from app.services.cloudinary_service import upload_image   # ✅ Cloudinary helper

router = APIRouter(prefix="/profile", tags=["Profile"])

# ---------- Models ----------
class OnboardingCompleteRequest(BaseModel):
    role: str

class SettingsUpdateRequest(BaseModel):
    role: str
    settings: Dict[str, Any]

class UpdateProviderProfileRequest(BaseModel):
    display_name: Optional[str] = None
    business_name: Optional[str] = None

# ---------- Mark a role as onboarded ----------
@router.post("/complete-onboarding")
async def complete_onboarding(
    req: OnboardingCompleteRequest,
    current_user: dict = Depends(get_current_user)
):
    exists = await database.fetch_one(
        "SELECT * FROM role_onboardings WHERE user_id = :uid AND role = :role",
        {"uid": current_user["id"], "role": req.role}
    )
    if not exists:
        await database.execute(
            "INSERT INTO role_onboardings (user_id, role) VALUES (:uid, :role)",
            {"uid": current_user["id"], "role": req.role}
        )
    return {"message": f"Onboarding for role '{req.role}' recorded"}

# ---------- Get onboarding status ----------
@router.get("/onboarding-status")
async def onboarding_status(current_user: dict = Depends(get_current_user)):
    rows = await database.fetch_all(
        "SELECT role FROM role_onboardings WHERE user_id = :uid",
        {"uid": current_user["id"]}
    )
    roles = [row["role"] for row in rows]
    return {"roles": roles}

# ---------- Get settings for a specific role ----------
@router.get("/settings/{role}")
async def get_settings(role: str, current_user: dict = Depends(get_current_user)):
    rows = await database.fetch_all(
        "SELECT key, value FROM user_settings WHERE user_id = :uid AND role = :role",
        {"uid": current_user["id"], "role": role}
    )
    settings = {}
    for row in rows:
        try:
            settings[row["key"]] = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            settings[row["key"]] = row["value"]
    return {"settings": settings}

# ---------- Save settings for a specific role ----------
@router.put("/settings")
async def update_settings(
    req: SettingsUpdateRequest,
    current_user: dict = Depends(get_current_user)
):
    for key, value in req.settings.items():
        serialized = json.dumps(value) if not isinstance(value, str) else value
        await database.execute(
            "DELETE FROM user_settings WHERE user_id = :uid AND role = :role AND key = :key",
            {"uid": current_user["id"], "role": req.role, "key": key}
        )
        await database.execute(
            "INSERT INTO user_settings (user_id, role, key, value) VALUES (:uid, :role, :key, :val)",
            {"uid": current_user["id"], "role": req.role, "key": key, "val": serialized}
        )
    return {"message": "Settings saved"}

# ---------- UPLOAD AVATAR (Cloudinary) ----------
@router.post("/upload-avatar")
async def upload_avatar(
    avatar: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    allowed_types = ["image/jpeg", "image/png", "image/webp"]
    if avatar.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Invalid file type")

    image_bytes = await avatar.read()
    avatar_url = upload_image(image_bytes, folder="avatars")   # ✅ Cloudinary

    # Update user's avatar_url with retry
    for attempt in range(3):
        try:
            await database.execute(
                "UPDATE users SET avatar_url = :url WHERE id = :uid",
                {"url": avatar_url, "uid": current_user["id"]}
            )
            break
        except Exception as e:
            if "database is locked" in str(e) and attempt < 2:
                await asyncio.sleep(0.5)
            else:
                raise

    return {"avatar_url": avatar_url}

# ---------- UPLOAD BUSINESS IMAGE (Cloudinary) ----------
@router.post("/upload-business-image")
async def upload_business_image(
    image: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    allowed_types = ["image/jpeg", "image/png", "image/webp"]
    if image.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Invalid file type")

    image_bytes = await image.read()
    business_image_url = upload_image(image_bytes, folder="business_images")   # ✅ Cloudinary

    # Update user's business_image_url with retry
    for attempt in range(3):
        try:
            await database.execute(
                "UPDATE users SET business_image_url = :url WHERE id = :uid",
                {"url": business_image_url, "uid": current_user["id"]}
            )
            break
        except Exception as e:
            if "database is locked" in str(e) and attempt < 2:
                await asyncio.sleep(0.5)
            else:
                raise

    return {"business_image_url": business_image_url}

# ---------- UPDATE PROVIDER PROFILE (display name & business name) ----------
@router.put("/update-provider-profile")
async def update_provider_profile(
    req: UpdateProviderProfileRequest,
    current_user: dict = Depends(get_current_user)
):
    updates = []
    values = {"uid": current_user["id"]}

    if req.display_name is not None:
        updates.append("nickname = :display_name")
        values["display_name"] = req.display_name.strip()

    if req.business_name is not None:
        updates.append("business_name = :business_name")
        values["business_name"] = req.business_name.strip()

    if updates:
        await database.execute(
            f"UPDATE users SET {', '.join(updates)} WHERE id = :uid",
            values
        )

    return {"message": "Profile updated"}