import os
import uuid
import json
import hmac
import hashlib
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Query, UploadFile, File, Form, Body
from pydantic import BaseModel
from app.db.database import database
from app.utils.security import get_current_user
from app.services.transcription_service import transcribe_audio

router = APIRouter(prefix="/chat", tags=["Chat"])

UPLOAD_DIR = "uploads/voice"

# ✅ WhatsApp parity limits
EDIT_WINDOW_MINUTES = 15
DELETE_FOR_EVERYONE_WINDOW_MINUTES = 60

# ✅ Jitsi room derivation — HMAC of conversation_id with server secret.
#    Deterministic (both users get the same room) but not guessable.
_JITSI_SECRET = os.getenv("JITSI_SECRET", os.getenv("JWT_SECRET", "change-me")).encode()
JITSI_BASE_URL = os.getenv("JITSI_BASE_URL", "https://meet.jit.si")


# ---------- Models ----------
class SendMessageRequest(BaseModel):
    receiver_id: str
    text: str = ""
    image_url: Optional[str] = None
    reply_to_id: Optional[int] = None

class EditMessageRequest(BaseModel):
    text: str

class CallSignalRequest(BaseModel):
    conversation_id: str
    receiver_id: str
    data: str


# ---------- Helpers ----------
async def _resolve_receiver_id(receiver_id: str, current_user_id: str) -> str:
    if "_" not in receiver_id:
        store = await database.fetch_one(
            "SELECT owner_id FROM stores WHERE store_id = :sid",
            {"sid": receiver_id}
        )
        if store:
            return store["owner_id"]
    return receiver_id


async def _get_conversation_id(user_id: str, other_id: str) -> str:
    if other_id == "seai":
        return f"{user_id}_seai"
    ids = sorted([user_id, other_id])
    return f"{ids[0]}_{ids[1]}"


def _validate_conversation_id(conversation_id: str, user_id: str) -> Optional[str]:
    parts = conversation_id.split("_")
    if len(parts) != 2:
        return None
    if user_id not in parts:
        return None
    return conversation_id


async def _hydrate_messages(rows, user_id: str) -> list[dict]:
    """
    Convert raw message rows to API dicts, adding reply context and
    masking content for deleted-for-everyone messages.
    """
    out = []
    for row in rows:
        d = dict(row)

        # Mask deleted-for-everyone content
        if d.get("deleted_for_everyone"):
            d["text"] = None
            d["image_url"] = None
            d["audio_url"] = None

        # Attach parent reply context if present
        if d.get("reply_to_id"):
            parent = await database.fetch_one(
                """
                SELECT m.text, m.deleted_for_everyone,
                       COALESCE(u.nickname, u.phone, m.sender_id) AS sender_name
                FROM messages m
                LEFT JOIN users u ON m.sender_id = u.id
                WHERE m.id = :pid
                """,
                {"pid": d["reply_to_id"]},
            )
            if parent:
                p = dict(parent)
                d["reply_to_text"] = None if p["deleted_for_everyone"] else p["text"]
                d["reply_to_sender_name"] = p["sender_name"]
                d["reply_to_deleted"] = p["deleted_for_everyone"]
            else:
                d["reply_to_text"] = None
                d["reply_to_sender_name"] = None
                d["reply_to_deleted"] = False

        out.append(d)
    return out


# ---------- Send text/image message ----------
@router.post("/send")
async def send_message(
    req: SendMessageRequest,
    current_user: dict = Depends(get_current_user)
):
    sender_id = current_user["id"]
    receiver_id = await _resolve_receiver_id(req.receiver_id, sender_id)
    if sender_id == receiver_id:
        raise HTTPException(status_code=400, detail="Cannot message yourself")

    conversation_id = await _get_conversation_id(sender_id, receiver_id)
    now = datetime.utcnow()

    # ✅ Validate reply_to_id if present — must be in the same conversation
    reply_to_id = req.reply_to_id
    if reply_to_id is not None:
        parent = await database.fetch_one(
            "SELECT id FROM messages WHERE id = :pid AND conversation_id = :cid",
            {"pid": reply_to_id, "cid": conversation_id},
        )
        if not parent:
            reply_to_id = None  # silently drop invalid reply target

    sender = await database.fetch_one(
        "SELECT COALESCE(nickname, phone, id) AS name FROM users WHERE id = :uid",
        {"uid": sender_id},
    )
    sender_name = sender["name"] if sender else sender_id

    result = await database.execute(
        """
        INSERT INTO messages
            (conversation_id, sender_id, receiver_id, sender_name, text,
             image_url, reply_to_id, created_at)
        VALUES
            (:cid, :sid, :rid, :sname, :text, :img, :reply, :now)
        RETURNING id
        """,
        {
            "cid": conversation_id,
            "sid": sender_id,
            "rid": receiver_id,
            "sname": sender_name,
            "text": req.text,
            "img": req.image_url,
            "reply": reply_to_id,
            "now": now,
        },
    )

    return {
        "id": result,
        "conversation_id": conversation_id,
        "sender_id": sender_id,
        "receiver_id": receiver_id,
        "text": req.text,
        "image_url": req.image_url,
        "reply_to_id": reply_to_id,
        "created_at": now,
        "message": "Message sent",
    }


# ---------- Edit message ----------
@router.put("/message/{message_id}")
async def edit_message(
    message_id: int,
    req: EditMessageRequest,
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user["id"]
    new_text = (req.text or "").strip()
    if not new_text:
        raise HTTPException(status_code=400, detail="Empty text")

    row = await database.fetch_one(
        "SELECT sender_id, created_at, deleted_for_everyone "
        "FROM messages WHERE id = :id",
        {"id": message_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Message not found")
    if row["sender_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not your message")
    if row["deleted_for_everyone"]:
        raise HTTPException(status_code=400, detail="Cannot edit a deleted message")

    created_at = row["created_at"]
    if created_at and datetime.utcnow() - created_at > timedelta(minutes=EDIT_WINDOW_MINUTES):
        raise HTTPException(
            status_code=403,
            detail=f"Edit window closed ({EDIT_WINDOW_MINUTES} min)",
        )

    now = datetime.utcnow()
    await database.execute(
        "UPDATE messages SET text = :t, edited_at = :now WHERE id = :id",
        {"t": new_text, "now": now, "id": message_id},
    )

    return {
        "id": message_id,
        "text": new_text,
        "edited_at": now,
        "message": "Message edited",
    }


# ---------- Delete message (for me / for everyone) ----------
@router.delete("/message/{message_id}")
async def delete_message(
    message_id: int,
    scope: str = Query("me", pattern="^(me|all)$"),
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user["id"]

    row = await database.fetch_one(
        "SELECT sender_id, receiver_id, created_at, deleted_for_everyone "
        "FROM messages WHERE id = :id",
        {"id": message_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Message not found")

    if user_id not in (row["sender_id"], row["receiver_id"]):
        raise HTTPException(status_code=403, detail="Access denied")

    now = datetime.utcnow()

    if scope == "me":
        await database.execute(
            """
            INSERT INTO message_deletions (user_id, message_id, deleted_at)
            VALUES (:uid, :mid, :now)
            ON CONFLICT (user_id, message_id) DO NOTHING
            """,
            {"uid": user_id, "mid": message_id, "now": now},
        )
        return {"id": message_id, "scope": "me", "message": "Deleted for you"}

    # scope == "all"
    if row["sender_id"] != user_id:
        raise HTTPException(
            status_code=403,
            detail="Only the sender can delete for everyone",
        )
    if row["deleted_for_everyone"]:
        return {"id": message_id, "scope": "all", "message": "Already deleted"}

    created_at = row["created_at"]
    if created_at and datetime.utcnow() - created_at > timedelta(
        minutes=DELETE_FOR_EVERYONE_WINDOW_MINUTES
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Delete-for-everyone window closed "
                f"({DELETE_FOR_EVERYONE_WINDOW_MINUTES} min)"
            ),
        )

    await database.execute(
        """
        UPDATE messages
        SET deleted_at = :now,
            deleted_for_everyone = TRUE,
            text = NULL,
            image_url = NULL,
            audio_url = NULL
        WHERE id = :id
        """,
        {"now": now, "id": message_id},
    )
    return {"id": message_id, "scope": "all", "message": "Deleted for everyone"}


# ---------- Send voice note ----------
@router.post("/send-voice")
async def send_voice(
    receiver_id: str = Form(...),
    audio: UploadFile = File(...),
    reply_to_id: Optional[int] = Form(None),   # ✅ NEW — voice notes can be replies
    current_user: dict = Depends(get_current_user),
):
    sender_id = current_user["id"]
    receiver_id = await _resolve_receiver_id(receiver_id, sender_id)
    if sender_id == receiver_id:
        raise HTTPException(status_code=400, detail="Cannot message yourself")

    # ── Save audio file ──────────────────────────────────────
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    ext = os.path.splitext(audio.filename)[1] or ".webm"
    audio_filename = f"{uuid.uuid4().hex}{ext}"
    audio_path = os.path.join(UPLOAD_DIR, audio_filename)
    with open(audio_path, "wb") as f:
        f.write(await audio.read())
    audio_url = f"/uploads/voice/{audio_filename}"

    # ── Transcribe (best-effort; never blocks the send) ──────
    transcription = ""
    try:
        transcription = transcribe_audio(audio_path)
    except Exception as e:
        print(f"Transcription failed: {e}")

    conversation_id = await _get_conversation_id(sender_id, receiver_id)
    now = datetime.utcnow()

    # ✅ Validate reply target if present
    if reply_to_id is not None:
        parent = await database.fetch_one(
            "SELECT id FROM messages WHERE id = :pid AND conversation_id = :cid",
            {"pid": reply_to_id, "cid": conversation_id},
        )
        if not parent:
            reply_to_id = None

    # ✅ Populate sender_name for consistency with text messages
    sender = await database.fetch_one(
        "SELECT COALESCE(nickname, phone, id) AS name FROM users WHERE id = :uid",
        {"uid": sender_id},
    )
    sender_name = sender["name"] if sender else sender_id

    result = await database.execute(
        """
        INSERT INTO messages
            (conversation_id, sender_id, receiver_id, sender_name,
             text, audio_url, reply_to_id, created_at)
        VALUES
            (:cid, :sid, :rid, :sname,
             :text, :audio, :reply, :now)
        RETURNING id
        """,
        {
            "cid": conversation_id,
            "sid": sender_id,
            "rid": receiver_id,
            "sname": sender_name,
            "text": transcription,
            "audio": audio_url,
            "reply": reply_to_id,
            "now": now,
        },
    )

    return {
        "id": result,
        "conversation_id": conversation_id,
        "sender_id": sender_id,
        "receiver_id": receiver_id,
        "audio_url": audio_url,
        "transcription": transcription,
        "reply_to_id": reply_to_id,
        "created_at": now,
        "message": "Voice note sent",
    }


# ---------- Get conversations ----------
@router.get("/conversations")
async def get_conversations(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    rows = await database.fetch_all(
        """
        SELECT m.conversation_id,
               MAX(m.created_at) AS last_time,
               (SELECT text FROM messages
                WHERE conversation_id = m.conversation_id
                ORDER BY created_at DESC LIMIT 1) AS last_message,
               (SELECT sender_id FROM messages
                WHERE conversation_id = m.conversation_id
                ORDER BY created_at DESC LIMIT 1) AS last_sender,
               (SELECT COUNT(*) FROM messages
                WHERE conversation_id = m.conversation_id
                AND receiver_id = :uid AND read = false) AS unread_count
        FROM messages m
        WHERE m.conversation_id LIKE '%%' || :uid || '%%'
        GROUP BY m.conversation_id
        ORDER BY last_time DESC
        """,
        {"uid": user_id}
    )

    conversations = []
    for row in rows:
        parts = row["conversation_id"].split("_")
        if len(parts) != 2:
            continue
        other_id = parts[0] if parts[1] == user_id else parts[1]
        other_user = await database.fetch_one(
            "SELECT nickname, phone, avatar_url FROM users WHERE id = :uid",
            {"uid": other_id}
        )
        other_name = other_user["nickname"] or other_user["phone"] if other_user else "Unknown"
        conversations.append({
            "conversation_id": row["conversation_id"],
            "other_user_id": other_id,
            "other_user_name": other_name,
            "other_user_avatar": other_user["avatar_url"] if other_user else None,
            "last_message": row["last_message"],
            "last_sender_id": row["last_sender"],
            "last_time": row["last_time"],
            "unread_count": row["unread_count"]
        })

    return conversations


# ---------- Get messages by other user ID ----------
@router.get("/messages/user/{other_user_id}")
async def get_messages_by_user(
    other_user_id: str,
    limit: int = 50,
    before_id: Optional[int] = Query(None),
    current_user: dict = Depends(get_current_user)
):
    user_id = current_user["id"]

    resolved_id = await _resolve_receiver_id(other_user_id, user_id)
    if resolved_id != other_user_id:
        other_user_id = resolved_id

    if user_id == other_user_id:
        raise HTTPException(status_code=400, detail="Cannot message yourself")

    conversation_id = await _get_conversation_id(user_id, other_user_id)

    params: dict = {"cid": conversation_id, "lim": limit, "uid": user_id}
    query = """
        SELECT m.*
        FROM messages m
        LEFT JOIN message_deletions md
          ON md.message_id = m.id AND md.user_id = :uid
        WHERE m.conversation_id = :cid
          AND md.user_id IS NULL
    """
    if before_id:
        query += " AND m.id < :bid"
        params["bid"] = before_id
    query += " ORDER BY m.created_at DESC LIMIT :lim"

    rows = await database.fetch_all(query, params)

    await database.execute(
        "UPDATE messages SET read = true WHERE conversation_id = :cid AND receiver_id = :uid AND read = false",
        {"cid": conversation_id, "uid": user_id}
    )

    hydrated = await _hydrate_messages(reversed(rows), user_id)
    return {
        "messages": hydrated,
        "conversation_id": conversation_id,
        "other_user_id": other_user_id,
    }


# ---------- Get messages by conversation ID ----------
@router.get("/messages/{conversation_id}")
async def get_messages(
    conversation_id: str,
    limit: int = 50,
    before_id: Optional[int] = Query(None),
    current_user: dict = Depends(get_current_user)
):
    user_id = current_user["id"]

    valid = _validate_conversation_id(conversation_id, user_id)
    if not valid:
        if "_" not in conversation_id:
            store = await database.fetch_one(
                "SELECT owner_id FROM stores WHERE store_id = :sid",
                {"sid": conversation_id}
            )
            if store:
                owner_id = store["owner_id"]
                actual_cid = await _get_conversation_id(user_id, owner_id)
                if _validate_conversation_id(actual_cid, user_id):
                    conversation_id = actual_cid
                else:
                    raise HTTPException(status_code=403, detail="Access denied")
            else:
                raise HTTPException(status_code=404, detail="Store not found")
        else:
            raise HTTPException(status_code=403, detail="Invalid conversation ID")

    params: dict = {"cid": conversation_id, "lim": limit, "uid": user_id}
    query = """
        SELECT m.*
        FROM messages m
        LEFT JOIN message_deletions md
          ON md.message_id = m.id AND md.user_id = :uid
        WHERE m.conversation_id = :cid
          AND md.user_id IS NULL
    """
    if before_id:
        query += " AND m.id < :bid"
        params["bid"] = before_id
    query += " ORDER BY m.created_at DESC LIMIT :lim"

    rows = await database.fetch_all(query, params)

    await database.execute(
        "UPDATE messages SET read = true WHERE conversation_id = :cid AND receiver_id = :uid AND read = false",
        {"cid": conversation_id, "uid": user_id}
    )

    hydrated = await _hydrate_messages(reversed(rows), user_id)
    return {"messages": hydrated, "conversation_id": conversation_id}


# ---------- Call room (Jitsi) ----------
@router.get("/call/room/{conversation_id}")
async def get_call_room(
    conversation_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Return a Jitsi room name/URL for this conversation.
    Room name is an HMAC of conversation_id → same for both participants,
    not guessable without the server secret. Access gated by the same
    validation as fetching messages.
    """
    user_id = current_user["id"]

    valid = _validate_conversation_id(conversation_id, user_id)
    if not valid:
        raise HTTPException(status_code=403, detail="Invalid conversation ID")

    digest = hmac.new(
        _JITSI_SECRET, conversation_id.encode(), hashlib.sha256
    ).hexdigest()[:24]
    room_name = f"admerce-{digest}"

    return {
        "room_name": room_name,
        "url": f"{JITSI_BASE_URL}/{room_name}",
        "conversation_id": conversation_id,
    }


# ---------- Legacy WebRTC signaling (kept for compatibility) ----------
@router.post("/call/offer")
async def send_offer(req: CallSignalRequest, current_user: dict = Depends(get_current_user)):
    await _store_signal(req.conversation_id, current_user["id"], req.receiver_id, "offer", req.data)
    return {"status": "offer sent"}


@router.post("/call/answer")
async def send_answer(req: CallSignalRequest, current_user: dict = Depends(get_current_user)):
    await _store_signal(req.conversation_id, current_user["id"], req.receiver_id, "answer", req.data)
    return {"status": "answer sent"}


@router.post("/call/ice-candidate")
async def send_ice_candidate(req: CallSignalRequest, current_user: dict = Depends(get_current_user)):
    await _store_signal(req.conversation_id, current_user["id"], req.receiver_id, "ice-candidate", req.data)
    return {"status": "ice-candidate sent"}


@router.get("/call/signals/{conversation_id}")
async def poll_signals(conversation_id: str, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    rows = await database.fetch_all(
        "SELECT * FROM call_signals WHERE conversation_id = :cid AND receiver_id = :uid ORDER BY id ASC",
        {"cid": conversation_id, "uid": user_id}
    )
    await database.execute(
        "DELETE FROM call_signals WHERE conversation_id = :cid AND receiver_id = :uid",
        {"cid": conversation_id, "uid": user_id}
    )
    return [{"type": row["type"], "data": json.loads(row["data"]), "sender_id": row["sender_id"]} for row in rows]


async def _store_signal(conv_id: str, sender: str, receiver: str, signal_type: str, data: str):
    now = datetime.utcnow()
    await database.execute(
        "INSERT INTO call_signals (conversation_id, sender_id, receiver_id, type, data, created_at) "
        "VALUES (:cid, :sid, :rid, :type, :data, :now)",
        {"cid": conv_id, "sid": sender, "rid": receiver, "type": signal_type, "data": data, "now": now}
    )


# ---------- SEAI specific endpoints ----------
@router.post("/seai/save")
async def save_seai_exchange(
    data: dict = Body(...),
    current_user: dict = Depends(get_current_user)
):
    user_id = current_user["id"]
    user_text = data.get("user_text", "")
    ai_text = data.get("ai_text", "")
    if not user_text or not ai_text:
        raise HTTPException(status_code=400, detail="Missing text")

    conversation_id = f"{user_id}_seai"
    now = datetime.utcnow()

    await database.execute(
        """
        INSERT INTO messages (conversation_id, sender_id, receiver_id, text, created_at)
        VALUES (:cid, :sid, :rid, :text, :now)
        """,
        {"cid": conversation_id, "sid": user_id, "rid": "seai", "text": user_text, "now": now}
    )
    await database.execute(
        """
        INSERT INTO messages (conversation_id, sender_id, receiver_id, text, created_at)
        VALUES (:cid, :sid, :rid, :text, :now)
        """,
        {"cid": conversation_id, "sid": "seai", "rid": user_id, "text": ai_text, "now": now}
    )
    return {"status": "ok"}


@router.get("/seai/conversations")
async def get_seai_conversations(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    conversation_id = f"{user_id}_seai"
    rows = await database.fetch_all(
        """
        SELECT id, text, created_at
        FROM messages
        WHERE conversation_id = :cid AND sender_id = :sid
        ORDER BY created_at DESC
        """,
        {"cid": conversation_id, "sid": "seai"}
    )
    if rows:
        title = rows[0]["text"][:50] + "..." if len(rows[0]["text"]) > 50 else rows[0]["text"]
        return [{"conversation_id": conversation_id, "title": title, "created_at": rows[0]["created_at"]}]
    return []