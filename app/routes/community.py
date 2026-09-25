from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime, timedelta, timezone as _tz
from app.db.database import database
from app.routes.auth import get_current_user

router = APIRouter(prefix="/community", tags=["Community"])

# Only these roles can read or write the seller community.
# Shoppers, couriers, flippers are excluded on purpose.
ALLOWED_ROLES = {"storekeeper", "service_provider", "admin"}

# How long after posting a message can the sender still edit it.
EDIT_WINDOW_MINUTES = 15

DEFAULT_ROOM = "global"
MAX_TEXT_LENGTH = 2000


# ---------- Models ----------
class PostMessageRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_TEXT_LENGTH)
    image_url: Optional[str] = None
    reply_to_id: Optional[int] = None
    room: str = DEFAULT_ROOM


class EditMessageRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_TEXT_LENGTH)


# ---------- Guard ----------
async def community_required(current_user: dict = Depends(get_current_user)):
    """Only sellers (and admin) may use the community endpoints."""
    role = (current_user.get("role") or "").lower()
    if role not in ALLOWED_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Only storekeepers and service providers can use the community.",
        )
    return current_user


# ---------- Helpers ----------
def _to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Normalize a datetime to naive-UTC so it can be safely compared with
    datetime.utcnow(). Handles both naive values (TIMESTAMP columns) and
    tz-aware values (TIMESTAMPTZ columns, which asyncpg returns aware).
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(_tz.utc).replace(tzinfo=None)
    return dt


async def _resolve_sender_identity(user_id: str) -> tuple[str, Optional[str]]:
    """
    Return (display_name, avatar_url) for the given user.
    Storekeepers → store name + store image.
    Providers   → business name + business image.
    Fallback    → nickname / phone / short id, avatar_url.
    """
    row = await database.fetch_one(
        """
        SELECT u.id, u.nickname, u.phone, u.role,
               u.business_name, u.business_image_url, u.avatar_url,
               s.name            AS store_name,
               s.store_image_url AS store_image
        FROM users u
        LEFT JOIN stores s ON s.owner_id = u.id
        WHERE u.id = :uid
        """,
        {"uid": user_id},
    )
    if not row:
        return (user_id, None)

    role = (row["role"] or "").lower()
    short_id = (row["id"] or "")[:8]

    if role == "storekeeper":
        name = row["store_name"] or row["nickname"] or row["phone"] or short_id
        avatar = row["store_image"] or row["avatar_url"]
    elif role == "service_provider":
        name = row["business_name"] or row["nickname"] or row["phone"] or short_id
        avatar = row["business_image_url"] or row["avatar_url"]
    else:
        name = row["nickname"] or row["phone"] or short_id
        avatar = row["avatar_url"]

    return (name or "Seller", avatar)


# ---------- Endpoints ----------
@router.get("/messages")
async def list_messages(
    room: str = Query(DEFAULT_ROOM),
    limit: int = Query(50, ge=1, le=200),
    before_id: Optional[int] = Query(None),
    current_user: dict = Depends(community_required),
):
    """
    Return recent messages in the room, oldest first.
    Soft-deleted rows are excluded. `before_id` enables backwards pagination.
    """
    params: dict = {"room": room, "lim": limit}
    query = """
        SELECT id, sender_id, sender_name, sender_avatar, sender_role,
               room, text, image_url, reply_to_id,
               created_at, edited_at, deleted_at
        FROM community_messages
        WHERE room = :room AND deleted_at IS NULL
    """
    if before_id:
        query += " AND id < :bid"
        params["bid"] = before_id
    query += " ORDER BY created_at DESC LIMIT :lim"

    rows = await database.fetch_all(query, params)

    # Reverse to oldest-first so the frontend can append without sorting.
    return [dict(r) for r in reversed(rows)]


@router.post("/messages", status_code=201)
async def post_message(
    req: PostMessageRequest,
    current_user: dict = Depends(community_required),
):
    sender_id = current_user["id"]
    sender_name, sender_avatar = await _resolve_sender_identity(sender_id)
    sender_role = (current_user.get("role") or "").lower()

    # Validate the reply target — must be a real, non-deleted message in the
    # same room.
    reply_to_id = req.reply_to_id
    if reply_to_id is not None:
        parent = await database.fetch_one(
            "SELECT id FROM community_messages "
            "WHERE id = :pid AND room = :room AND deleted_at IS NULL",
            {"pid": reply_to_id, "room": req.room},
        )
        if not parent:
            reply_to_id = None  # silently drop invalid reply target

    now = datetime.utcnow()
    new_id = await database.execute(
        """
        INSERT INTO community_messages
            (sender_id, sender_name, sender_avatar, sender_role,
             room, text, image_url, reply_to_id, created_at)
        VALUES
            (:sid, :sname, :savatar, :srole,
             :room, :text, :img, :reply, :now)
        RETURNING id
        """,
        {
            "sid": sender_id,
            "sname": sender_name,
            "savatar": sender_avatar,
            "srole": sender_role,
            "room": req.room,
            "text": req.text.strip(),
            "img": req.image_url,
            "reply": reply_to_id,
            "now": now,
        },
    )

    return {
        "id": new_id,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "sender_avatar": sender_avatar,
        "sender_role": sender_role,
        "room": req.room,
        "text": req.text.strip(),
        "image_url": req.image_url,
        "reply_to_id": reply_to_id,
        "created_at": now,
        "edited_at": None,
        "deleted_at": None,
    }


@router.put("/messages/{message_id}")
async def edit_message(
    message_id: int,
    req: EditMessageRequest,
    current_user: dict = Depends(community_required),
):
    user_id = current_user["id"]

    row = await database.fetch_one(
        "SELECT sender_id, created_at, deleted_at "
        "FROM community_messages WHERE id = :id",
        {"id": message_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Message not found")
    if row["sender_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not your message")
    if row["deleted_at"]:
        raise HTTPException(status_code=400, detail="Cannot edit a deleted message")

    # Compare in naive-UTC on both sides. Handles both TIMESTAMP and
    # TIMESTAMPTZ columns — the latter returns tz-aware from asyncpg.
    created_at_naive = _to_naive_utc(row["created_at"])
    if created_at_naive and datetime.utcnow() - created_at_naive > timedelta(
        minutes=EDIT_WINDOW_MINUTES
    ):
        raise HTTPException(
            status_code=403,
            detail=f"Edit window closed ({EDIT_WINDOW_MINUTES} min)",
        )

    now = datetime.utcnow()
    await database.execute(
        "UPDATE community_messages SET text = :t, edited_at = :now WHERE id = :id",
        {"t": req.text.strip(), "now": now, "id": message_id},
    )
    return {"id": message_id, "text": req.text.strip(), "edited_at": now}


@router.delete("/messages/{message_id}")
async def delete_message(
    message_id: int,
    current_user: dict = Depends(community_required),
):
    """Soft-delete. Sender can delete their own; admin can delete any."""
    user_id = current_user["id"]
    is_admin = (current_user.get("role") or "").lower() == "admin"

    row = await database.fetch_one(
        "SELECT sender_id, deleted_at FROM community_messages WHERE id = :id",
        {"id": message_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Message not found")
    if row["sender_id"] != user_id and not is_admin:
        raise HTTPException(status_code=403, detail="Not authorized")
    if row["deleted_at"]:
        return {"id": message_id, "message": "Already deleted"}

    now = datetime.utcnow()
    await database.execute(
        """
        UPDATE community_messages
        SET deleted_at = :now,
            text = '[deleted]',
            image_url = NULL
        WHERE id = :id
        """,
        {"now": now, "id": message_id},
    )
    return {"id": message_id, "deleted_at": now}


@router.get("/stats")
async def community_stats(
    room: str = Query(DEFAULT_ROOM),
    current_user: dict = Depends(community_required),
):
    """
    Small numbers for the community header.
    `active_senders_7d` counts distinct senders in the last 7 days.
    """
    total_messages = await database.fetch_val(
        """
        SELECT COUNT(*) FROM community_messages
        WHERE room = :room AND deleted_at IS NULL
        """,
        {"room": room},
    ) or 0

    active_senders = await database.fetch_val(
        """
        SELECT COUNT(DISTINCT sender_id)
        FROM community_messages
        WHERE room = :room
          AND deleted_at IS NULL
          AND created_at > NOW() - INTERVAL '7 days'
        """,
        {"room": room},
    ) or 0

    return {
        "room": room,
        "total_messages": total_messages,
        "active_senders_7d": active_senders,
    }