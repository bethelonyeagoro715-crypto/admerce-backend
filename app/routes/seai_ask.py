import json
import traceback
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Dict
from app.routes.auth import get_optional_user

# Our agentic modules
from app.services.seai_agent import (
    classify_intent,
    handle_book_service,
    handle_search_items,      # now returns items + services + stores
    handle_get_store_info,
)
from app.services.llm_agent import call_claude

router = APIRouter(prefix="/seai", tags=["SEAI Agent"])

class AskRequest(BaseModel):
    query: str
    lat: float = 6.5244
    lng: float = 3.3792
    radius_km: float = 10.0
    conversation_history: List[Dict[str, str]] = []
    mode: Optional[str] = "gpt"      # "gpt" (SEAI) or "agent" (Cortex)

# ============================================================
# CORE ENDPOINT – now with two‑model routing
# ============================================================
@router.post("/ask")
async def seai_ask(
    req: AskRequest,
    current_user: Optional[dict] = Depends(get_optional_user),
):
    user_id = current_user["id"] if current_user else None

    # ── Agent mode (SEAI Cortex) ──────────────────────────
    if req.mode == "agent":
        if not user_id:
            return StreamingResponse(
                _stream_text("Please log in first to perform actions."),
                media_type="text/event-stream"
            )
        return await _handle_agent_mode(req, user_id)

    # ── GPT mode (SEAI) ──────────────────────────────────
    intent, params = classify_intent(req.query)

    if intent == "search":
        result = await handle_search_items(params, req.lat, req.lng, user_id)
        if result.get("type") == "action":
            return StreamingResponse(
                _stream_text_and_action(result),
                media_type="text/event-stream"
            )
        if result.get("type") == "text":
            return StreamingResponse(
                _stream_text(result["text"]),
                media_type="text/event-stream"
            )

    elif intent == "book_service":
        result = await handle_book_service(user_id, params)
        if result.get("type") == "action":
            return StreamingResponse(
                _stream_action(result),
                media_type="text/event-stream"
            )
        if result.get("type") == "text":
            return StreamingResponse(
                _stream_text(result["text"]),
                media_type="text/event-stream"
            )

    elif intent == "get_store_info":
        result = await handle_get_store_info(params)
        if result.get("type") == "action":
            return StreamingResponse(
                _stream_action(result),
                media_type="text/event-stream"
            )
        if result.get("type") == "text":
            return StreamingResponse(
                _stream_text(result["text"]),
                media_type="text/event-stream"
            )

    # 2. Fallback to Claude
    claude_result = await call_claude(req.query)
    if claude_result["type"] == "action":
        ai_intent = claude_result["data"]["intent"]
        ai_params = claude_result["data"].get("params", {})
        if ai_intent == "search":
            result = await handle_search_items(ai_params, req.lat, req.lng, user_id)
            if result.get("type") == "action":
                return StreamingResponse(
                    _stream_text_and_action(result),
                    media_type="text/event-stream"
                )
            if result.get("type") == "text":
                return StreamingResponse(
                    _stream_text(result["text"]),
                    media_type="text/event-stream"
                )
        elif ai_intent == "book_service":
            result = await handle_book_service(user_id, ai_params)
            if result.get("type") == "action":
                return StreamingResponse(_stream_action(result), media_type="text/event-stream")
            if result.get("type") == "text":
                return StreamingResponse(_stream_text(result["text"]), media_type="text/event-stream")
        elif ai_intent == "get_store_info":
            result = await handle_get_store_info(ai_params)
            if result.get("type") == "action":
                return StreamingResponse(_stream_action(result), media_type="text/event-stream")
            if result.get("type") == "text":
                return StreamingResponse(_stream_text(result["text"]), media_type="text/event-stream")

        # For other intents from Claude – just return text
        return StreamingResponse(
            _stream_text(claude_result.get("text", "Hello!")),
            media_type="text/event-stream"
        )

    # 3. Return plain text
    return StreamingResponse(
        _stream_text(claude_result.get("text", "Hello!")),
        media_type="text/event-stream"
    )

# ── Agent mode handler ─────────────────────────────────────
async def _handle_agent_mode(req: AskRequest, user_id: str):
    intent, params = classify_intent(req.query)
    # For now, agent actions are not fully implemented – reply with a helpful message
    return StreamingResponse(
        _stream_text("Agent mode is ready. I can book services, create listings, and more. Just tell me what to do."),
        media_type="text/event-stream",
    )

# ── Stream generators ──────────────────────────────────────
async def _stream_text(text: str):
    words = text.split()
    for i, word in enumerate(words):
        yield f"data: {json.dumps({'text': word + (' ' if i < len(words)-1 else '')})}\n\n"
    yield "data: [DONE]\n\n"

async def _stream_action(action: dict):
    yield f"data: {json.dumps({'type': 'action', 'data': action['data']})}\n\n"
    yield "data: [DONE]\n\n"

async def _stream_text_and_action(action: dict):
    """Streams a short introduction text, then the action payload (results)."""
    query = action["data"]["query"]
    results = action["data"]["results"]
    count = len(results)
    intro = f"I found {count} result{'s' if count != 1 else ''} matching '{query}':"
    for word in intro.split():
        yield f"data: {json.dumps({'text': word + ' '})}\n\n"
    # Send the action event – the frontend will display the list
    yield f"data: {json.dumps({'type': 'action', 'data': action['data']})}\n\n"
    yield "data: [DONE]\n\n"

# ── Backward compatibility (unchanged) ────────────────────
async def _process_ask(
    query: str,
    lat: float,
    lng: float,
    radius_km: float,
    conversation_history: List[Dict[str, str]],
    user_id: Optional[str] = None,
):
    req = AskRequest(
        query=query,
        lat=lat,
        lng=lng,
        radius_km=radius_km,
        conversation_history=conversation_history,
    )
    return await seai_ask(req=req, current_user={"id": user_id} if user_id else None)