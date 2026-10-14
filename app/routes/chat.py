import os
import json
import hmac
import hashlib
import tempfile
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Query, UploadFile, File, Form, Body
from pydantic import BaseModel
from app.db.database import database
from app.utils.security import get_current_user
from app.services.transcription_service import transcribe_audio
from app.services.cloudinary_service import upload_video

router = APIRouter(prefix="/chat", tags=["Chat"])

EDIT_WINDOW_MINUTES = 15
DELETE_FOR_EVERYONE_WINDOW_MINUTES = 60

_JITSI_SECRET = os.getenv("JITSI_SECRET", os.getenv("JWT_SECRET", "change-me")).encode()
JITSI_BASE_URL = os.getenv("JITSI_BASE_URL", "https://meet.jit.si")

# ── In-memory typing state ─────────────────────────────────────
# Fine for a single Render worker. If you ever set WEB_CONCURRENCY>1,
# move this to Redis or a small Postgres table.
_typing_state: dict[str, dict[str, float]] = {}
TYPING_TTL_SECONDS = 5


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

class TypingRequest(BaseModel):
    conversation_id: str


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
    out = []
    for row in rows:
        d = dict(row)

        if d.get("deleted_for_everyone"):
            d["text"] = None
            d["image_url"] = None
            d["audio_url"] = None

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
    now = datetime.now(timezone.utc)

    reply_to_id = req.reply_to_id
    if reply_to_id is not None:
        parent = await database.fetch_one(
            "SELECT id FROM messages WHERE id = :pid AND conversation_id = :cid",
            {"pid": reply_to_id, "cid": conversation_id},
        )
        if not parent:
            reply_to_id = None

    sender = await database.fetch_one(
        "SELECT COALESCE(nickname, phone, id) AS name FROM users WHERE id = :uid",
        {"uid": sender_id},
    )
    sender_name = sender["name"] if sender else sender_id

    # ✅ FIX — use fetch_val so RETURNING id actually returns the id.
    new_id = await database.fetch_val(
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

    # Clear my typing state — I just sent.
    conv_state = _typing_state.get(conversation_id)
    if conv_state:
        conv_state.pop(sender_id, None)

    return {
        "id": new_id,
        "conversation_id": conversation_id,
        "sender_id": sender_id,
        "receiver_id": receiver_id,
        "text": req.text,
        "image_url": req.image_url,
        "reply_to_id": reply_to_id,
        "created_at": now,
        "message": "Message sent",
    }


# ---------- Typing indicator ----------
@router.post("/typing")
async def set_typing(
    req: TypingRequest,
    current_user: dict = Depends(get_current_user),
):
    """Client calls this on every keystroke, throttled to ~2s."""
    user_id = current_user["id"]
    valid = _validate_conversation_id(req.conversation_id, user_id)
    if not valid:
        raise HTTPException(status_code=403, detail="Invalid conversation ID")
    _typing_state.setdefault(req.conversation_id, {})[user_id] = time.time()
    return {"ok": True}


@router.get("/typing/{conversation_id}")
async def get_typing(
    conversation_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Client polls this every ~2s while the chat screen is open."""
    user_id = current_user["id"]
    valid = _validate_conversation_id(conversation_id, user_id)
    if not valid:
        raise HTTPException(status_code=403, detail="Invalid conversation ID")

    now = time.time()
    users = _typing_state.get(conversation_id, {})
    for uid in list(users.keys()):
        if now - users[uid] > TYPING_TTL_SECONDS:
            del users[uid]

    other_typing = any(uid != user_id for uid in users.keys())
    return {"typing": other_typing}


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
    if created_at and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if created_at and datetime.now(timezone.utc) - created_at > timedelta(minutes=EDIT_WINDOW_MINUTES):
        raise HTTPException(
            status_code=403,
            detail=f"Edit window closed ({EDIT_WINDOW_MINUTES} min)",
        )

    now = datetime.now(timezone.utc)
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

    now = datetime.now(timezone.utc)

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

    if row["sender_id"] != user_id:
        raise HTTPException(
            status_code=403,
            detail="Only the sender can delete for everyone",
        )
    if row["deleted_for_everyone"]:
        return {"id": message_id, "scope": "all", "message": "Already deleted"}

    created_at = row["created_at"]
    if created_at and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if created_at and datetime.now(timezone.utc) - created_at > timedelta(
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
    reply_to_id: Optional[int] = Form(None),
    current_user: dict = Depends(get_current_user),
):
    sender_id = current_user["id"]
    receiver_id = await _resolve_receiver_id(receiver_id, sender_id)
    if sender_id == receiver_id:
        raise HTTPException(status_code=400, detail="Cannot message yourself")

    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio file")

    ext = os.path.splitext(audio.filename or "")[1].lower() or ".webm"

    transcription = ""
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        transcription = transcribe_audio(tmp_path) or ""
    except Exception as e:
        print(f"⚠️  Transcription failed: {e!r}", flush=True)
        transcription = ""
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    try:
        audio_url = upload_video(audio_bytes, folder="voice_notes")
    except Exception as e:
        print(f"❌ Voice upload failed: {e!r}", flush=True)
        raise HTTPException(status_code=500, detail="Voice note upload failed")

    conversation_id = await _get_conversation_id(sender_id, receiver_id)
    now = datetime.now(timezone.utc)

    if reply_to_id is not None:
        parent = await database.fetch_one(
            "SELECT id FROM messages WHERE id = :pid AND conversation_id = :cid",
            {"pid": reply_to_id, "cid": conversation_id},
        )
        if not parent:
            reply_to_id = None

    sender = await database.fetch_one(
        "SELECT COALESCE(nickname, phone, id) AS name FROM users WHERE id = :uid",
        {"uid": sender_id},
    )
    sender_name = sender["name"] if sender else sender_id

    # ✅ FIX — fetch_val so RETURNING id returns the new id.
    new_id = await database.fetch_val(
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

    conv_state = _typing_state.get(conversation_id)
    if conv_state:
        conv_state.pop(sender_id, None)

    return {
        "id": new_id,
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

        # ✅ FIX — pull the same columns as presence.py so the fallback
        # chain matches everywhere. Previously this only looked at
        # nickname + phone, so providers/storekeepers showed as "Unknown".
        other_user = await database.fetch_one(
            """
            SELECT u.nickname, u.real_name, u.first_name, u.last_name,
                   u.phone, u.business_name, u.avatar_url,
                   u.business_image_url, u.role,
                   s.name            AS own_store_name,
                   s.store_image_url AS own_store_image
            FROM users u
            LEFT JOIN stores s ON s.owner_id = u.id
            WHERE u.id = :uid
            """,
            {"uid": other_id},
        )

        if other_user:
            ou = dict(other_user)
            role = (ou.get("role") or "").lower()

            name = (
                ou.get("business_name")
                or ou.get("own_store_name")
                or ou.get("nickname")
                or ou.get("real_name")
            )
            if not name and (ou.get("first_name") or ou.get("last_name")):
                name = " ".join(
                    p for p in [ou.get("first_name"), ou.get("last_name")] if p
                ).strip() or None
            if not name and ou.get("phone"):
                name = ou["phone"]
            if not name:
                name = other_id[:8]

            if role == "service_provider" and ou.get("business_image_url"):
                avatar = ou["business_image_url"]
            elif role == "storekeeper" and ou.get("own_store_image"):
                avatar = ou["own_store_image"]
            else:
                avatar = ou.get("avatar_url")
        else:
            name = "Unknown"
            avatar = None

        conversations.append({
            "conversation_id": row["conversation_id"],
            "other_user_id": other_id,
            "other_user_name": name,
            "other_user_avatar": avatar,
            "last_message": row["last_message"],
            "last_sender_id": row["last_sender"],
            "last_time": row["last_time"],
            "unread_count": row["unread_count"],
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


# ---------- Legacy WebRTC signaling ----------
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
    now = datetime.now(timezone.utc)
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
    now = datetime.now(timezone.utc)

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