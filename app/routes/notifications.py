# app/routes/notifications.py
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from app.db.database import database
from app.utils.security import get_current_user
import os
import json
from typing import Optional

router = APIRouter(prefix="/notifications", tags=["Notifications"])

# Firebase initialisation
try:
    import firebase_admin
    from firebase_admin import credentials, messaging
    firebase_creds = os.getenv("FIREBASE_CREDENTIALS")
    if firebase_creds:
        cred = credentials.Certificate(json.loads(firebase_creds))
        firebase_admin.initialize_app(cred)
        print("🔥 Firebase initialized – push notifications enabled")
    else:
        firebase_admin = None
        messaging = None
        print("⚠️ FIREBASE_CREDENTIALS not set. Push notifications disabled.")
except ImportError:
    firebase_admin = None
    messaging = None
    print("⚠️ firebase-admin not installed. Push notifications disabled.")

class RegisterDeviceRequest(BaseModel):
    fcm_token: str
    user_id: Optional[str] = None

async def get_current_user_optional(request: Request):
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.replace("Bearer ", "")
        try:
            return await get_current_user(token)
        except Exception:
            return None
    return None

@router.post("/register-device")
async def register_device(
    req: RegisterDeviceRequest,
    current_user: Optional[dict] = Depends(get_current_user_optional)
):
    user_id = req.user_id or (current_user["id"] if current_user else None)

    if user_id:
        await database.execute(
            "UPDATE user_devices SET is_active = false WHERE user_id = :uid",
            {"uid": user_id}
        )

    await database.execute(
        "INSERT INTO user_devices (user_id, fcm_token, is_active) VALUES (:uid, :token, true)",
        {"uid": user_id, "token": req.fcm_token}
    )
    return {"message": "Device registered for push notifications"}

def flatten_data_for_fcm(data: dict) -> dict:
    """FCM requires all data payload values to be strings."""
    if not data:
        return {}
    flat = {}
    for k, v in data.items():
        if isinstance(v, (dict, list)):
            flat[k] = json.dumps(v)
        elif v is not None:
            flat[k] = str(v)
    return flat

async def send_push_to_user(user_id: str, title: str, body: str, data: dict = None):
    """Send a push notification and store it in the history table."""
    # 1. Store in history
    try:
        await database.execute(
            "INSERT INTO user_notifications (user_id, title, body, data) "
            "VALUES (:uid, :title, :body, :data)",
            {
                "uid": user_id,
                "title": title,
                "body": body,
                "data": json.dumps(data) if data else None
            }
        )
    except Exception as e:
        print(f"⚠️ Failed to store notification history: {e}")

    # 2. Send push via Firebase
    if firebase_admin is None or messaging is None:
        print(f"⚠️ Push skipped (Firebase not available) – {title}")
        return
        
    rows = await database.fetch_all(
        "SELECT fcm_token FROM user_devices WHERE user_id = :uid AND is_active = true",
        {"uid": user_id}
    )
    
    fcm_data = flatten_data_for_fcm(data)
    
    for row in rows:
        token = row["fcm_token"]
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data=fcm_data,
            token=token,
        )
        try:
            messaging.send(message)
            print(f"✅ Push sent to {user_id}")
        except Exception as e:
            print(f"❌ Push failed: {e}")
            if "invalid-argument" in str(e) or "registration-token-not-registered" in str(e):
                await database.execute(
                    "UPDATE user_devices SET is_active = false WHERE fcm_token = :token",
                    {"token": token}
                )

@router.get("/")
async def get_notifications(
    limit: int = 50,
    offset: int = 0,
    current_user: dict = Depends(get_current_user)
):
    user_id = current_user["id"]
    rows = await database.fetch_all(
        """
        SELECT id, title, body, data, is_read, created_at
        FROM user_notifications
        WHERE user_id = :uid
        ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset
        """,
        {"uid": user_id, "limit": limit, "offset": offset}
    )
    return [dict(row) for row in rows]

@router.post("/read/{notification_id}")
async def mark_as_read(notification_id: int, current_user: dict = Depends(get_current_user)):
    await database.execute(
        "UPDATE user_notifications SET is_read = true WHERE id = :id AND user_id = :uid",
        {"id": notification_id, "uid": current_user["id"]}
    )
    return {"message": "Marked as read"}