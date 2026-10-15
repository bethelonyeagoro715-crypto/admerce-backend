"""
/profile extras — edit my profile, delete my account, addresses, blocked, activity.

Runs alongside profile.py under the same /profile prefix.
"""
from fastapi import APIRouter, HTTPException, Depends, Body
from pydantic import BaseModel
from typing import Optional
import uuid
from datetime import datetime

from app.db.database import database
from app.utils.security import get_current_user


router = APIRouter(prefix="/profile", tags=["Profile Extras"])


# ─────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────
class UpdateMeRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    nickname: Optional[str] = None
    real_name: Optional[str] = None
    bio: Optional[str] = None


class DeleteAccountRequest(BaseModel):
    reason: Optional[str] = None
    detail: Optional[str] = None


class AddressInput(BaseModel):
    label: Optional[str] = None
    recipient_name: Optional[str] = None
    phone: Optional[str] = None
    line1: str
    line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = "Nigeria"
    postal_code: Optional[str] = None
    is_default: Optional[bool] = False
    kind: Optional[str] = "home"
    latitude: Optional[float] = None
    longitude: Optional[float] = None


# ─────────────────────────────────────────────────────────────
# EDIT / DELETE ME
# ─────────────────────────────────────────────────────────────
@router.patch("/me")
async def update_me(
    req: UpdateMeRequest, current_user: dict = Depends(get_current_user)
):
    updates = []
    params: dict = {"uid": current_user["id"]}

    def _clean(v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        return s or None

    if req.first_name is not None:
        updates.append("first_name = :first_name")
        params["first_name"] = _clean(req.first_name)
    if req.last_name is not None:
        updates.append("last_name = :last_name")
        params["last_name"] = _clean(req.last_name)
    if req.nickname is not None:
        n = _clean(req.nickname)
        if n and len(n) > 30:
            raise HTTPException(status_code=400, detail="Nickname max 30 characters")
        updates.append("nickname = :nickname")
        params["nickname"] = n
    if req.real_name is not None:
        updates.append("real_name = :real_name")
        params["real_name"] = _clean(req.real_name)
    if req.bio is not None:
        b = _clean(req.bio)
        if b and len(b) > 200:
            raise HTTPException(status_code=400, detail="Bio max 200 characters")
        updates.append("bio = :bio")
        params["bio"] = b

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    await database.execute(
        f"UPDATE users SET {', '.join(updates)} WHERE id = :uid", params
    )
    return {"message": "Profile updated"}


@router.delete("/me")
async def delete_me(
    payload: Optional[DeleteAccountRequest] = Body(default=None),
    current_user: dict = Depends(get_current_user),
):
    """
    Deletes the user. Cascades to addresses, favorites, blocks, feedback,
    sessions, and everything else with an ON DELETE CASCADE FK to users.
    """
    uid = current_user["id"]
    # Optional: record reason before the row disappears
    if payload and (payload.reason or payload.detail):
        try:
            await database.execute(
                """
                INSERT INTO feedback (user_id, type, category, description, created_at)
                VALUES (:uid, 'account_deletion', :cat, :desc, :now)
                """,
                {
                    "uid": uid,
                    "cat": payload.reason,
                    "desc": payload.detail,
                    "now": datetime.utcnow(),
                },
            )
        except Exception:
            pass

    result = await database.execute("DELETE FROM users WHERE id = :uid", {"uid": uid})
    if isinstance(result, str) and result.strip().endswith("0"):
        raise HTTPException(status_code=404, detail="User not found")
    return {"message": "Account deleted"}


# ─────────────────────────────────────────────────────────────
# ADDRESSES
# ─────────────────────────────────────────────────────────────
@router.get("/addresses")
async def list_addresses(current_user: dict = Depends(get_current_user)):
    rows = await database.fetch_all(
        """
        SELECT id, label, recipient_name, phone, line1, line2, city, state,
               country, postal_code, is_default, kind, latitude, longitude
        FROM addresses
        WHERE user_id = :uid
        ORDER BY is_default DESC, created_at DESC
        """,
        {"uid": current_user["id"]},
    )
    return [dict(r) for r in rows]


async def _clear_other_defaults(user_id: str, keep_id: Optional[str] = None) -> None:
    if keep_id:
        await database.execute(
            "UPDATE addresses SET is_default = FALSE WHERE user_id = :uid AND id <> :kid",
            {"uid": user_id, "kid": keep_id},
        )
    else:
        await database.execute(
            "UPDATE addresses SET is_default = FALSE WHERE user_id = :uid",
            {"uid": user_id},
        )


@router.post("/addresses", status_code=201)
async def create_address(
    req: AddressInput, current_user: dict = Depends(get_current_user)
):
    if not req.line1 or not req.line1.strip():
        raise HTTPException(status_code=400, detail="line1 is required")

    addr_id = str(uuid.uuid4())
    is_default = bool(req.is_default)

    # If this is the user's first address, force it default.
    existing = await database.fetch_val(
        "SELECT COUNT(*) FROM addresses WHERE user_id = :uid",
        {"uid": current_user["id"]},
    )
    if existing == 0:
        is_default = True

    if is_default:
        await _clear_other_defaults(current_user["id"])

    now = datetime.utcnow()
    await database.execute(
        """
        INSERT INTO addresses (
            id, user_id, label, recipient_name, phone, line1, line2, city,
            state, country, postal_code, is_default, kind, latitude, longitude,
            created_at, updated_at
        ) VALUES (
            :id, :uid, :label, :recipient, :phone, :line1, :line2, :city,
            :state, :country, :postal, :def, :kind, :lat, :lng,
            :now, :now
        )
        """,
        {
            "id": addr_id,
            "uid": current_user["id"],
            "label": req.label,
            "recipient": req.recipient_name,
            "phone": req.phone,
            "line1": req.line1.strip(),
            "line2": req.line2,
            "city": req.city,
            "state": req.state,
            "country": req.country or "Nigeria",
            "postal": req.postal_code,
            "def": is_default,
            "kind": req.kind or "home",
            "lat": req.latitude,
            "lng": req.longitude,
            "now": now,
        },
    )
    return {"id": addr_id, "is_default": is_default, "message": "Address created"}


@router.patch("/addresses/{address_id}")
async def update_address(
    address_id: str,
    req: AddressInput,
    current_user: dict = Depends(get_current_user),
):
    row = await database.fetch_one(
        "SELECT id FROM addresses WHERE id = :id AND user_id = :uid",
        {"id": address_id, "uid": current_user["id"]},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Address not found")

    is_default = bool(req.is_default)
    if is_default:
        await _clear_other_defaults(current_user["id"], keep_id=address_id)

    await database.execute(
        """
        UPDATE addresses SET
            label = :label,
            recipient_name = :recipient,
            phone = :phone,
            line1 = :line1,
            line2 = :line2,
            city = :city,
            state = :state,
            country = :country,
            postal_code = :postal,
            is_default = :def,
            kind = :kind,
            latitude = :lat,
            longitude = :lng,
            updated_at = :now
        WHERE id = :id AND user_id = :uid
        """,
        {
            "id": address_id,
            "uid": current_user["id"],
            "label": req.label,
            "recipient": req.recipient_name,
            "phone": req.phone,
            "line1": req.line1.strip(),
            "line2": req.line2,
            "city": req.city,
            "state": req.state,
            "country": req.country or "Nigeria",
            "postal": req.postal_code,
            "def": is_default,
            "kind": req.kind or "home",
            "lat": req.latitude,
            "lng": req.longitude,
            "now": datetime.utcnow(),
        },
    )
    return {"id": address_id, "message": "Address updated"}


@router.delete("/addresses/{address_id}")
async def delete_address(
    address_id: str, current_user: dict = Depends(get_current_user)
):
    result = await database.execute(
        "DELETE FROM addresses WHERE id = :id AND user_id = :uid",
        {"id": address_id, "uid": current_user["id"]},
    )
    if isinstance(result, str) and result.strip().endswith("0"):
        raise HTTPException(status_code=404, detail="Address not found")
    return {"message": "Address deleted"}


# ─────────────────────────────────────────────────────────────
# BLOCKED USERS
# ─────────────────────────────────────────────────────────────
@router.get("/blocked")
async def list_blocked(current_user: dict = Depends(get_current_user)):
    rows = await database.fetch_all(
        """
        SELECT b.id, b.blocked_id AS user_id, b.created_at AS blocked_at,
               COALESCE(
                   NULLIF(CONCAT(u.first_name, ' ', u.last_name), ' '),
                   u.nickname, u.real_name, u.phone, 'Unknown'
               ) AS display_name,
               u.nickname, u.real_name, u.phone, u.avatar_url
        FROM user_blocks b
        JOIN users u ON u.id = b.blocked_id
        WHERE b.blocker_id = :uid
        ORDER BY b.created_at DESC
        """,
        {"uid": current_user["id"]},
    )
    return [dict(r) for r in rows]


@router.delete("/blocked/{user_id}")
async def unblock_user(
    user_id: str, current_user: dict = Depends(get_current_user)
):
    result = await database.execute(
        "DELETE FROM user_blocks WHERE blocker_id = :uid AND blocked_id = :target",
        {"uid": current_user["id"], "target": user_id},
    )
    if isinstance(result, str) and result.strip().endswith("0"):
        raise HTTPException(status_code=404, detail="Not blocked")
    return {"message": "User unblocked"}


# ─────────────────────────────────────────────────────────────
# LOGIN ACTIVITY
# ─────────────────────────────────────────────────────────────
@router.get("/login-activity")
async def login_activity(
    limit: int = 50, current_user: dict = Depends(get_current_user)
):
    limit = max(1, min(limit, 200))
    rows = await database.fetch_all(
        """
        SELECT id, action, device_kind, device_name, ip, city, country,
               success, created_at
        FROM login_activity
        WHERE user_id = :uid
        ORDER BY created_at DESC
        LIMIT :lim
        """,
        {"uid": current_user["id"], "lim": limit},
    )
    return [dict(r) for r in rows]

# ─────────────────────────────────────────────────────────────
# USER FOLLOWS
# ─────────────────────────────────────────────────────────────

class CreateHighlightRequest(BaseModel):
    emoji: str
    label: str
    route: str


@router.get("/{user_id}/follow-status")
async def get_follow_status(
    user_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Whether the current user follows `user_id`, plus both counts.
    Self-view returns is_self=True and zeros.
    """
    if user_id == current_user["id"]:
        return {
            "is_following": False,
            "followers_count": 0,
            "following_count": 0,
            "is_self": True,
        }

    row = await database.fetch_one(
        """
        SELECT 1 AS x FROM user_follows
         WHERE follower_id = :fid AND following_id = :tid
        """,
        {"fid": current_user["id"], "tid": user_id},
    )
    counts = await database.fetch_one(
        """
        SELECT
            (SELECT COUNT(*) FROM user_follows WHERE following_id = :uid) AS followers,
            (SELECT COUNT(*) FROM user_follows WHERE follower_id  = :uid) AS following
        """,
        {"uid": user_id},
    )
    return {
        "is_following": row is not None,
        "followers_count": int(counts["followers"]) if counts else 0,
        "following_count": int(counts["following"]) if counts else 0,
        "is_self": False,
    }


@router.post("/{user_id}/follow", status_code=201)
async def follow_user(
    user_id: str,
    current_user: dict = Depends(get_current_user),
):
    if user_id == current_user["id"]:
        raise HTTPException(status_code=400, detail="Cannot follow yourself")

    target = await database.fetch_one(
        "SELECT 1 AS x FROM users WHERE id = :uid", {"uid": user_id}
    )
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    await database.execute(
        """
        INSERT INTO user_follows (follower_id, following_id)
        VALUES (:fid, :tid)
        ON CONFLICT DO NOTHING
        """,
        {"fid": current_user["id"], "tid": user_id},
    )
    count = await database.fetch_val(
        "SELECT COUNT(*) FROM user_follows WHERE following_id = :uid",
        {"uid": user_id},
    )
    return {"following": True, "followers_count": int(count or 0)}


@router.delete("/{user_id}/follow")
async def unfollow_user(
    user_id: str,
    current_user: dict = Depends(get_current_user),
):
    await database.execute(
        """
        DELETE FROM user_follows
         WHERE follower_id = :fid AND following_id = :tid
        """,
        {"fid": current_user["id"], "tid": user_id},
    )
    count = await database.fetch_val(
        "SELECT COUNT(*) FROM user_follows WHERE following_id = :uid",
        {"uid": user_id},
    )
    return {"following": False, "followers_count": int(count or 0)}


# ─────────────────────────────────────────────────────────────
# PROFILE HIGHLIGHTS (user-curated shortcut strip)
# ─────────────────────────────────────────────────────────────

def _safe_route(route: str) -> bool:
    if not route or not route.startswith("/"):
        return False
    if route.startswith("//") or route.startswith("/\\"):
        return False
    if "http" in route.lower() or "://" in route:
        return False
    if len(route) > 300:
        return False
    return True


@router.get("/highlights")
async def list_my_highlights(current_user: dict = Depends(get_current_user)):
    rows = await database.fetch_all(
        """
        SELECT id, emoji, label, route, sort_order, created_at
          FROM profile_highlights
         WHERE user_id = :uid
         ORDER BY sort_order ASC, created_at ASC
        """,
        {"uid": current_user["id"]},
    )
    return [dict(r) for r in rows]


@router.post("/highlights", status_code=201)
async def create_highlight(
    req: CreateHighlightRequest,
    current_user: dict = Depends(get_current_user),
):
    emoji = (req.emoji or "").strip()[:8]
    label = (req.label or "").strip()
    route = (req.route or "").strip()

    if not emoji or not label or not route:
        raise HTTPException(status_code=400, detail="emoji, label and route are required")
    if len(label) > 20:
        raise HTTPException(status_code=400, detail="label max 20 chars")
    if not _safe_route(route):
        raise HTTPException(status_code=400, detail="Invalid route")

    # Enforce a small cap to prevent abuse
    count = await database.fetch_val(
        "SELECT COUNT(*) FROM profile_highlights WHERE user_id = :uid",
        {"uid": current_user["id"]},
    )
    if int(count or 0) >= 12:
        raise HTTPException(status_code=400, detail="Maximum 12 highlights")

    hid = str(uuid.uuid4())
    await database.execute(
        """
        INSERT INTO profile_highlights (id, user_id, emoji, label, route)
        VALUES (:id, :uid, :e, :l, :r)
        """,
        {"id": hid, "uid": current_user["id"], "e": emoji, "l": label, "r": route},
    )
    return {"id": hid, "emoji": emoji, "label": label, "route": route}


@router.delete("/highlights/{highlight_id}")
async def delete_highlight(
    highlight_id: str,
    current_user: dict = Depends(get_current_user),
):
    result = await database.execute(
        "DELETE FROM profile_highlights WHERE id = :id AND user_id = :uid",
        {"id": highlight_id, "uid": current_user["id"]},
    )
    if isinstance(result, str) and result.strip().endswith("0"):
        raise HTTPException(status_code=404, detail="Highlight not found")
    return {"message": "Highlight deleted"}


@router.get("/{user_id}/highlights")
async def get_user_highlights(
    user_id: str,
    _: dict = Depends(get_current_user),
):
    """Public highlight list for another user."""
    rows = await database.fetch_all(
        """
        SELECT id, emoji, label, route
          FROM profile_highlights
         WHERE user_id = :uid
         ORDER BY sort_order ASC, created_at ASC
        """,
        {"uid": user_id},
    )
    return {"highlights": [dict(r) for r in rows]}