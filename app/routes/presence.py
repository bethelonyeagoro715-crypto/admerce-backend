from fastapi import APIRouter, Depends, HTTPException
from datetime import datetime, timezone
from typing import Optional
from app.db.database import database
from app.routes.auth import get_current_user, get_optional_user

router = APIRouter(prefix="/presence", tags=["Presence"])

ONLINE_THRESHOLD_MINUTES = 5


def _parse_dt(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


@router.post("/heartbeat")
async def heartbeat(current_user: dict = Depends(get_current_user)):
    """Update the caller's last-seen timestamp. Called by the frontend
    every ~60s while the app is open."""
    await database.execute(
        "UPDATE users SET last_seen_at = :now WHERE id = :uid",
        {"now": datetime.now(timezone.utc), "uid": current_user["id"]},
    )
    return {"ok": True}


@router.get("/{user_id}")
async def get_presence(
    user_id: str,
    _: Optional[dict] = Depends(get_optional_user),
):
    """Return presence + public profile summary for the given user.
    Public route — no auth required so anonymous chat works too."""
    row = await database.fetch_one(
        """
        SELECT u.id,
               u.nickname,
               u.real_name,
               u.first_name,
               u.last_name,
               u.phone,
               u.role,
               u.avatar_url,
               u.business_name,
               u.business_image_url,
               u.last_seen_at,
               s.store_id        AS own_store_id,
               s.name            AS own_store_name,
               s.store_image_url AS own_store_image
        FROM users u
        LEFT JOIN stores s ON s.owner_id = u.id
        WHERE u.id = :uid
        """,
        {"uid": user_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    last_seen = _parse_dt(row["last_seen_at"])
    online = False
    if last_seen:
        delta = datetime.now(timezone.utc) - last_seen
        online = delta.total_seconds() < ONLINE_THRESHOLD_MINUTES * 60

    role = (row["role"] or "").lower()

    public_url: Optional[str] = None
    if role == "storekeeper" and row["own_store_id"]:
        public_url = f"/store-detail/{row['own_store_id']}"
    elif role == "service_provider":
        public_url = f"/service-provider/{row['id']}"

    display_name: Optional[str] = None
    if row["business_name"]:
        display_name = row["business_name"]
    elif row["own_store_name"]:
        display_name = row["own_store_name"]
    elif row["nickname"]:
        display_name = row["nickname"]
    elif row["real_name"]:
        display_name = row["real_name"]
    elif row["first_name"] or row["last_name"]:
        display_name = " ".join(
            p for p in [row["first_name"], row["last_name"]] if p
        ).strip() or None
    elif row["phone"]:
        display_name = row["phone"]
    elif row["id"]:
        display_name = row["id"][:8]

    if not display_name:
        display_name = "User"

    if role == "service_provider" and row["business_image_url"]:
        avatar_url = row["business_image_url"]
    elif role == "storekeeper" and row["own_store_image"]:
        avatar_url = row["own_store_image"]
    else:
        avatar_url = row["avatar_url"]

    return {
        "user_id": row["id"],
        "name": display_name,
        "avatar_url": avatar_url,
        "role": row["role"] or "user",
        "online": online,
        "last_seen": last_seen.isoformat() if last_seen else None,
        "public_url": public_url,
    }