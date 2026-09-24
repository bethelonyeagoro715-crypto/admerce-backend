from fastapi import APIRouter, Depends, HTTPException
from datetime import datetime
from typing import Optional
from app.db.database import database
from app.routes.auth import get_current_user, get_optional_user

router = APIRouter(prefix="/presence", tags=["Presence"])

# A user is considered "online" if their last heartbeat was within this window.
ONLINE_THRESHOLD_MINUTES = 5


def _parse_dt(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(
                value.replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except ValueError:
            return None
    return None


@router.post("/heartbeat")
async def heartbeat(current_user: dict = Depends(get_current_user)):
    """Update the caller's last-seen timestamp. Called by the frontend
    every ~60s while the app is open."""
    await database.execute(
        "UPDATE users SET last_seen_at = :now WHERE id = :uid",
        {"now": datetime.utcnow(), "uid": current_user["id"]},
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
        SELECT u.id, u.nickname, u.username, u.avatar_url, u.role,
               u.business_name, u.business_image_url, u.last_seen_at,
               s.store_id       AS own_store_id,
               s.name           AS own_store_name,
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
        delta = datetime.utcnow() - last_seen
        online = delta.total_seconds() < ONLINE_THRESHOLD_MINUTES * 60

    role = (row["role"] or "").lower()

    # Public profile URL, if we know where the user's profile lives.
    public_url: Optional[str] = None
    if role == "storekeeper" and row["own_store_id"]:
        public_url = f"/store-detail/{row['own_store_id']}"
    elif role == "service_provider":
        public_url = f"/provider-services/{row['id']}"

    # Display name preference — business name first for seller roles.
    if role == "storekeeper" and row["own_store_name"]:
        display_name = row["own_store_name"]
    elif role == "service_provider" and row["business_name"]:
        display_name = row["business_name"]
    else:
        display_name = (
            row["nickname"]
            or row["username"]
            or (row["id"][:8] if row["id"] else "User")
        )

    # Avatar preference — store image for storekeepers, business image
    # for providers, then fall back to avatar_url.
    if role == "storekeeper" and row["own_store_image"]:
        avatar_url = row["own_store_image"]
    elif role == "service_provider" and row["business_image_url"]:
        avatar_url = row["business_image_url"]
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