import json
import os
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Dict

from groq import AsyncGroq

from app.routes.auth import get_optional_user
from app.services.seai_agent import (
    classify_intent,
    handle_book_service,
    handle_search_items,
    handle_get_store_info,
)
from app.services.llm_agent import GROQ_ENABLED, GROQ_MODEL

router = APIRouter(prefix="/seai", tags=["SEAI Agent"])

_groq = AsyncGroq(api_key=os.getenv("GROQ_API_KEY")) if os.getenv("GROQ_API_KEY") else None


class AskRequest(BaseModel):
    query: str
    lat: float = 6.5244
    lng: float = 3.3792
    radius_km: float = 10.0
    conversation_history: List[Dict[str, str]] = []
    mode: Optional[str] = "gpt"


SYSTEM_PROMPT = """You are SEAI, the AI shopping assistant for Admerce — a hyper-local commerce marketplace.

You are warm, helpful, and conversational. You speak like a knowledgeable friend who knows the local area.

You have these tools:
- search_items(query)   → find products, services, and stores near the user
- book_service(service) → book a service
- get_store_info(store) → get details on a specific store

Rules:
1. If the user wants to find, see, buy, or explore something — call search_items.
2. After a search, LOOK at the results before writing. Reference specific
   store names, prices, and locations from the data.
3. Never say "I found N results" — write like a person recommending things.
4. Mention travel time or distance naturally when it helps.
5. Keep responses under 40 words unless the user asks for detail.
6. For non-shopping questions, answer helpfully with plain text.

Example good responses:
  • "Graham Hub has an iPhone 14 for ₦450,000, about 22 minutes from you — worth a look."
  • "Three shops have what you need. The nearest is FixIt Lagos at 0.8 km."
  • "Nothing local matches that yet. Want me to keep looking?"
"""


def _tools():
    return [
        {"type": "function", "function": {
            "name": "search_items",
            "description": "Search products, services, and stores near the user",
            "parameters": {"type": "object",
                "properties": {"query": {"type": "string",
                    "description": "What to search for"}},
                "required": ["query"]}}},
        {"type": "function", "function": {
            "name": "book_service",
            "description": "Book a service",
            "parameters": {"type": "object",
                "properties": {"service": {"type": "string"}},
                "required": ["service"]}}},
        {"type": "function", "function": {
            "name": "get_store_info",
            "description": "Get info on a store by name",
            "parameters": {"type": "object",
                "properties": {"store": {"type": "string"}},
                "required": ["store"]}}},
    ]


@router.post("/ask")
async def seai_ask(
    req: AskRequest,
    current_user: Optional[dict] = Depends(get_optional_user),
):
    user_id = current_user["id"] if current_user else None

    if req.mode == "agent":
        # Agent mode is the same loop — kept as a flag for future divergence
        pass

    if not GROQ_ENABLED or _groq is None:
        # No LLM at all → fall back to regex path
        return await _regex_fallback(req, user_id)

    return await _groq_agent_loop(req, user_id)


async def _groq_agent_loop(req: AskRequest, user_id: Optional[str]):
    """Full agentic loop: Groq reasons, calls tools, then writes the response."""

    messages: list = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Include prior turns for context
    for m in req.conversation_history[-6:]:
        role = m.get("role", "user")
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": m.get("content", "")})

    messages.append({"role": "user", "content": req.query})

    search_results = None
    cards_payload: Optional[dict] = None

    try:
        # ── Step 1: let Groq decide ────────────────────────────
        resp = await _groq.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            tools=_tools(),
            tool_choice="auto",
            max_tokens=400,
            temperature=0.6,
        )

        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)

        if not tool_calls:
            # Groq answered directly
            return StreamingResponse(
                _stream_text(msg.content or "How can I help?"),
                media_type="text/event-stream",
            )

        # ── Step 2: execute the chosen tool ────────────────────
        call = tool_calls[0]
        fn = call.function.name
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}

        if fn == "search_items":
            result = await handle_search_items(args, req.lat, req.lng, user_id)
        elif fn == "book_service":
            result = await handle_book_service(user_id or "", args)
        elif fn == "get_store_info":
            result = await handle_get_store_info(args)
        else:
            result = {"type": "text", "text": "I can't do that yet."}

        # If it's a search result, remember the cards
        if result.get("type") == "action" and result.get("intent") == "search_results":
            cards_payload = result["data"]

        # ── Step 3: feed results back to Groq for the final reply ─
        compact = _compact_for_llm(result)

        messages.append(msg)  # the assistant's tool_call message
        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(compact),
        })

        # If no search happened, just return the raw result
        if cards_payload is None:
            return _respond_from_result(result)

        # ── Step 4: Groq writes the intro using real data ──────
        final = await _groq.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            max_tokens=120,
            temperature=0.7,
        )
        intro = (final.choices[0].message.content or "").strip().strip('"')

        if not intro:
            intro = _fallback_intro(cards_payload["results"])

        return StreamingResponse(
            _stream_text_and_action(intro, cards_payload),
            media_type="text/event-stream",
        )

    except Exception as e:
        print(f"❌ SEAI agent error: {e}")
        return await _regex_fallback(req, user_id)


def _compact_for_llm(result: dict) -> dict:
    """Trim the tool result so it fits in the LLM's token budget."""
    if result.get("type") != "action":
        return result
    data = result.get("data", {})
    items = data.get("results", [])[:6]
    slim = [{
        "type": r.get("type"),
        "title": r.get("title"),
        "price": r.get("price"),
        "store_name": r.get("store_name") or r.get("provider_name"),
        "distance_km": r.get("distance_km"),
        "travel_minutes": r.get("travel_minutes"),
        "address": r.get("address"),
    } for r in items]
    return {"intent": result.get("intent"), "query": data.get("query"), "results": slim}


def _fallback_intro(results: list) -> str:
    if not results:
        return "Nothing turned up nearby — want me to keep looking?"
    if len(results) == 1:
        r = results[0]
        bits = [r.get("title") or "One option"]
        if r.get("store_name") or r.get("provider_name"):
            bits.append(f"at {r['store_name'] if r.get('store_name') else r['provider_name']}")
        if r.get("travel_minutes"):
            bits.append(f"about {r['travel_minutes']} min away")
        return " ".join(bits) + "."
    return f"{len(results)} options nearby — the closest is {results[0].get('title', 'first on the list')}."


async def _regex_fallback(req: AskRequest, user_id: Optional[str]):
    """Used only when Groq is unavailable."""
    intent, params = classify_intent(req.query)
    if intent == "search":
        r = await handle_search_items(params, req.lat, req.lng, user_id)
        return _respond_from_result(r)
    if intent == "book_service":
        r = await handle_book_service(user_id or "", params)
        return _respond_from_result(r)
    if intent == "get_store_info":
        r = await handle_get_store_info(params)
        return _respond_from_result(r)
    return StreamingResponse(
        _stream_text("I'm offline for a moment. Try again shortly."),
        media_type="text/event-stream",
    )


def _respond_from_result(result: dict):
    if result.get("type") == "action":
        return StreamingResponse(
            _stream_text_and_action("", result["data"]),
            media_type="text/event-stream",
        )
    return StreamingResponse(
        _stream_text(result.get("text") or "Nothing to show."),
        media_type="text/event-stream",
    )


async def _stream_text(text: str):
    for i, w in enumerate(text.split()):
        yield f"data: {json.dumps({'text': w + (' ' if i < len(text.split()) - 1 else '')})}\n\n"
    yield "data: [DONE]\n\n"


async def _stream_text_and_action(intro: str, data: dict):
    if intro:
        words = intro.split()
        for i, w in enumerate(words):
            yield f"data: {json.dumps({'text': w + (' ' if i < len(words) - 1 else '')})}\n\n"
    yield f"data: {json.dumps({'type': 'action', 'intent': 'search_results', 'data': data})}\n\n"
    yield "data: [DONE]\n\n"


async def _process_ask(query, lat, lng, radius_km, conversation_history, user_id=None):
    req = AskRequest(query=query, lat=lat, lng=lng,
                     radius_km=radius_km, conversation_history=conversation_history)
    return await seai_ask(req=req, current_user={"id": user_id} if user_id else None)