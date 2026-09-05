from fastapi import APIRouter, HTTPException, Depends, Request, Header
from pydantic import BaseModel
from app.db.database import database
from app.utils.security import get_current_user
import os
import json
from datetime import datetime
from typing import Optional, List

router = APIRouter(prefix="/notifications", tags=["Notifications"])

# Firebase initialisation (unchanged)
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
    user_id: Optional[str] = None   # Optional, can be provided by frontend if known


# New optional user dependency – tries to get user from Bearer token, else returns None
async def get_current_user_optional(request: Request):
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.replace("Bearer ", "")
        try:
            # Assume get_current_user accepts a token string (adjust if needed)
            return await get_current_user(token)
        except Exception:
            return None
    return None


@router.post("/register-device")
async def register_device(
    req: RegisterDeviceRequest,
    current_user: Optional[dict] = Depends(get_current_user_optional)
):
    """Register or update the FCM token. If authenticated, ties token to user_id; otherwise stores anonymously."""
    # Determine user_id: from request body, else from auth, else None
    user_id = req.user_id or (current_user["id"] if current_user else None)

    # Deactivate previous tokens for this user (if user_id exists)
    if user_id:
        await database.execute(
            "UPDATE user_devices SET is_active = false WHERE user_id = :uid",
            {"uid": user_id}
        )

    # Insert new token
    await database.execute(
        "INSERT INTO user_devices (user_id, fcm_token, is_active) VALUES (:uid, :token, true)",
        {"uid": user_id, "token": req.fcm_token}
    )
    return {"message": "Device registered for push notifications"}


# Helper function – used by wallet.py and other modules
async def send_push_to_user(user_id: str, title: str, body: str, data: dict = None):
    """Send a push notification and also store it in the history table."""
    # 1. Store the notification in history (always, even if push fails)
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

    # 2. Send push via Firebase (if available)
    if firebase_admin is None or messaging is None:
        print(f"⚠️ Push skipped (Firebase not available) – {title}")
        return
    rows = await database.fetch_all(
        "SELECT fcm_token FROM user_devices WHERE user_id = :uid AND is_active = true",
        {"uid": user_id}
    )
    for row in rows:
        token = row["fcm_token"]
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data=data,
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


# ── Get notification history ──────────────────────────────────
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


# ── Optional: Mark as read ────────────────────────────────────────
@router.post("/read/{notification_id}")
async def mark_as_read(notification_id: int, current_user: dict = Depends(get_current_user)):
    await database.execute(
        "UPDATE user_notifications SET is_read = true WHERE id = :id AND user_id = :uid",
        {"id": notification_id, "uid": current_user["id"]}
    )
    return {"message": "Marked as read"}