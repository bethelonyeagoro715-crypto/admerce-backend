import os
import uuid
import json
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Query, UploadFile, File, Form, Body
from pydantic import BaseModel
from app.db.database import database
from app.utils.security import get_current_user
from app.services.transcription_service import transcribe_audio

router = APIRouter(prefix="/chat", tags=["Chat"])

UPLOAD_DIR = "uploads/voice"

# ---------- Models ----------
class SendMessageRequest(BaseModel):
    receiver_id: str
    text: str = ""
    image_url: Optional[str] = None

class CallSignalRequest(BaseModel):
    conversation_id: str
    receiver_id: str
    data: str

# ---------- Helpers ----------
async def _resolve_receiver_id(receiver_id: str, current_user_id: str) -> str:
    """
    If receiver_id is a store ID, return the store owner's user ID.
    Otherwise, return the receiver_id as-is.
    """
    if "_" not in receiver_id:
        store = await database.fetch_one(
            "SELECT owner_id FROM stores WHERE store_id = :sid",
            {"sid": receiver_id}
        )
        if store:
            return store["owner_id"]
    return receiver_id

async def _get_conversation_id(user_id: str, other_id: str) -> str:
    """
    Return a sorted composite key for a user-user conversation.
    Special case: if other_id is "seai", use f"{user_id}_seai".
    """
    if other_id == "seai":
        return f"{user_id}_seai"
    ids = sorted([user_id, other_id])
    return f"{ids[0]}_{ids[1]}"

def _validate_conversation_id(conversation_id: str, user_id: str) -> Optional[str]:
    """
    Return the valid conversation ID if it's a two‑part ID and the user is one part.
    Returns None if invalid.
    """
    parts = conversation_id.split("_")
    if len(parts) != 2:
        return None
    if user_id not in parts:
        return None
    return conversation_id

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
    now = datetime.utcnow().isoformat()

    await database.execute(
        """
        INSERT INTO messages (conversation_id, sender_id, receiver_id, text, image_url, created_at)
        VALUES (:cid, :sid, :rid, :text, :img, :now)
        """,
        {"cid": conversation_id, "sid": sender_id, "rid": receiver_id,
         "text": req.text, "img": req.image_url, "now": now}
    )

    return {
        "conversation_id": conversation_id,
        "sender_id": sender_id,
        "receiver_id": receiver_id,
        "text": req.text,
        "image_url": req.image_url,
        "created_at": now,
        "message": "Message sent"
    }

# ---------- Send voice note ----------
@router.post("/send-voice")
async def send_voice(
    receiver_id: str = Form(...),
    audio: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    sender_id = current_user["id"]
    receiver_id = await _resolve_receiver_id(receiver_id, sender_id)
    if sender_id == receiver_id:
        raise HTTPException(status_code=400, detail="Cannot message yourself")

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    ext = os.path.splitext(audio.filename)[1] or ".webm"
    audio_filename = f"{uuid.uuid4().hex}{ext}"
    audio_path = os.path.join(UPLOAD_DIR, audio_filename)
    with open(audio_path, "wb") as f:
        f.write(await audio.read())
    audio_url = f"/uploads/voice/{audio_filename}"

    transcription = ""
    try:
        transcription = transcribe_audio(audio_path)
    except Exception as e:
        print(f"Transcription failed: {e}")

    conversation_id = await _get_conversation_id(sender_id, receiver_id)
    now = datetime.utcnow().isoformat()

    await database.execute(
        """
        INSERT INTO messages (conversation_id, sender_id, receiver_id, text, audio_url, created_at)
        VALUES (:cid, :sid, :rid, :text, :audio, :now)
        """,
        {"cid": conversation_id, "sid": sender_id, "rid": receiver_id,
         "text": transcription, "audio": audio_url, "now": now}
    )

    return {
        "conversation_id": conversation_id,
        "sender_id": sender_id,
        "receiver_id": receiver_id,
        "audio_url": audio_url,
        "transcription": transcription,
        "created_at": now,
        "message": "Voice note sent"
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
            continue  # skip malformed IDs
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

# ---------- Get messages by other user ID (auto‑create conversation) ----------
@router.get("/messages/user/{other_user_id}")
async def get_messages_by_user(
    other_user_id: str,
    limit: int = 50,
    before_id: Optional[int] = Query(None),
    current_user: dict = Depends(get_current_user)
):
    user_id = current_user["id"]

    # Resolve if other_user_id is a store ID
    resolved_id = await _resolve_receiver_id(other_user_id, user_id)
    if resolved_id != other_user_id:
        # It was a store ID, resolved to owner
        other_user_id = resolved_id

    if user_id == other_user_id:
        raise HTTPException(status_code=400, detail="Cannot message yourself")

    # Find or create conversation
    conversation_id = await _get_conversation_id(user_id, other_user_id)

    # Build query
    params: dict = {"cid": conversation_id, "lim": limit}
    query = "SELECT * FROM messages WHERE conversation_id = :cid"
    if before_id:
        query += " AND id < :bid"
        params["bid"] = before_id
    query += " ORDER BY created_at DESC LIMIT :lim"

    rows = await database.fetch_all(query, params)

    # Mark as read (boolean true/false)
    await database.execute(
        "UPDATE messages SET read = true WHERE conversation_id = :cid AND receiver_id = :uid AND read = false",
        {"cid": conversation_id, "uid": user_id}
    )

    messages = list(reversed([dict(row) for row in rows]))
    return {
        "messages": messages,
        "conversation_id": conversation_id,
        "other_user_id": other_user_id
    }

# ---------- Get messages in a conversation (existing, by conversation ID) ----------
@router.get("/messages/{conversation_id}")
async def get_messages(
    conversation_id: str,
    limit: int = 50,
    before_id: Optional[int] = Query(None),
    current_user: dict = Depends(get_current_user)
):
    user_id = current_user["id"]

    # 1. Validate conversation ID format
    valid = _validate_conversation_id(conversation_id, user_id)
    if not valid:
        # If it's a store ID, resolve it
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

    # 2. Fetch messages
    params: dict = {"cid": conversation_id, "lim": limit}
    query = "SELECT * FROM messages WHERE conversation_id = :cid"
    if before_id:
        query += " AND id < :bid"
        params["bid"] = before_id
    query += " ORDER BY created_at DESC LIMIT :lim"

    rows = await database.fetch_all(query, params)

    # Mark unread as read (boolean true/false)
    await database.execute(
        "UPDATE messages SET read = true WHERE conversation_id = :cid AND receiver_id = :uid AND read = false",
        {"cid": conversation_id, "uid": user_id}
    )

    messages = list(reversed([dict(row) for row in rows]))
    return {"messages": messages, "conversation_id": conversation_id}

# ---------- Call signaling (unchanged) ----------
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
    now = datetime.utcnow().isoformat()
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
    now = datetime.utcnow().isoformat()

    # User message
    await database.execute(
        """
        INSERT INTO messages (conversation_id, sender_id, receiver_id, text, created_at)
        VALUES (:cid, :sid, :rid, :text, :now)
        """,
        {"cid": conversation_id, "sid": user_id, "rid": "seai", "text": user_text, "now": now}
    )
    # AI message
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