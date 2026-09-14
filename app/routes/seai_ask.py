import json
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Dict

from app.routes.auth import get_optional_user
from app.services.seai_agent import (
    classify_intent,
    handle_book_service,
    handle_search_items,
    handle_get_store_info,
)
from app.services.llm_agent import call_llm, GEMINI_ENABLED, genai

router = APIRouter(prefix="/seai", tags=["SEAI Agent"])


class AskRequest(BaseModel):
    query: str
    lat: float = 6.5244
    lng: float = 3.3792
    radius_km: float = 10.0
    conversation_history: List[Dict[str, str]] = []
    mode: Optional[str] = "gpt"   # "gpt" (SEAI) or "agent" (Cortex)


# ════════════════════════════════════════════════════════════
# MAIN ENDPOINT
# ════════════════════════════════════════════════════════════

@router.post("/ask")
async def seai_ask(
    req: AskRequest,
    current_user: Optional[dict] = Depends(get_optional_user),
):
    user_id = current_user["id"] if current_user else None

    # ── Agent mode (SEAI Cortex — Gemini function calling) ──
    if req.mode == "agent":
        if not user_id:
            return StreamingResponse(
                _stream_text("Please log in first to perform actions."),
                media_type="text/event-stream",
            )
        return await _handle_agent_mode(req, user_id)

    # ── GPT mode (SEAI — rule-based + LLM fallback) ─────────
    intent, params = classify_intent(req.query)

    if intent == "search":
        result = await handle_search_items(params, req.lat, req.lng, user_id)
        return _respond_from_result(result)

    if intent == "book_service":
        result = await handle_book_service(user_id, params)
        return _respond_from_result(result)

    if intent == "get_store_info":
        result = await handle_get_store_info(params)
        return _respond_from_result(result)

    # Fallback: ask the LLM (Groq → Gemini → NVIDIA)
    llm_result = await call_llm(req.query)

    if llm_result["type"] == "action":
        ai_intent = llm_result["data"]["intent"]
        ai_params = llm_result["data"].get("params", {})

        if ai_intent == "search":
            result = await handle_search_items(ai_params, req.lat, req.lng, user_id)
            return _respond_from_result(result)

        if ai_intent == "book_service":
            result = await handle_book_service(user_id, ai_params)
            return _respond_from_result(result)

        if ai_intent == "get_store_info":
            result = await handle_get_store_info(ai_params)
            return _respond_from_result(result)

    return StreamingResponse(
        _stream_text(llm_result.get("text", "Hello!")),
        media_type="text/event-stream",
    )


def _respond_from_result(result: dict):
    """Turn the agent helper's result into the correct StreamingResponse."""
    if result.get("type") == "action":
        return StreamingResponse(
            _stream_text_and_action(result),
            media_type="text/event-stream",
        )
    if result.get("type") == "text":
        return StreamingResponse(
            _stream_text(result["text"]),
            media_type="text/event-stream",
        )
    if result.get("type") == "error":
        return StreamingResponse(
            _stream_text(result.get("message", "Something went wrong.")),
            media_type="text/event-stream",
        )
    return StreamingResponse(
        _stream_text("I'm not sure how to help with that."),
        media_type="text/event-stream",
    )


# ════════════════════════════════════════════════════════════
# SEAI CORTEX — Agent mode with Gemini function calling
# ════════════════════════════════════════════════════════════

AGENT_SYSTEM_PROMPT = """You are SEAI Cortex, the agentic assistant for Admerce — a hyper-local commerce platform.

You can call these functions to help the user:
- search_items(query): find products, services, and stores near the user
- book_service(service): reserve a service from a provider
- get_store_info(store): get details about a specific store

Always call the appropriate function when the user's request matches one.
Be concise. Confirm what you're doing in one short sentence before the action."""


def _gemini_tools():
    """Gemini function declarations for SEAI Cortex."""
    return [
        {
            "function_declarations": [
                {
                    "name": "search_items",
                    "description": "Search for items, services, and stores near the user",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "What the user is looking for (e.g. 'iphone', 'pizza', 'plumber')",
                            }
                        },
                        "required": ["query"],
                    },
                },
                {
                    "name": "book_service",
                    "description": "Book a service from a provider",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "service": {
                                "type": "string",
                                "description": "The service the user wants to book",
                            }
                        },
                        "required": ["service"],
                    },
                },
                {
                    "name": "get_store_info",
                    "description": "Get details about a specific store",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "store": {
                                "type": "string",
                                "description": "The name of the store",
                            }
                        },
                        "required": ["store"],
                    },
                },
            ]
        }
    ]


async def _handle_agent_mode(req: AskRequest, user_id: str):
    if not GEMINI_ENABLED or genai is None:
        return StreamingResponse(
            _stream_text(
                "SEAI Cortex needs a Gemini API key. Ask support to enable it."
            ),
            media_type="text/event-stream",
        )

    try:
        model = genai.GenerativeModel(
            "gemini-1.5-flash",
            tools=_gemini_tools(),
            system_instruction=AGENT_SYSTEM_PROMPT,
        )
        chat = model.start_chat()
        response = chat.send_message(req.query)

        # Walk through the response parts looking for a function call
        function_call = None
        response_text = ""

        for part in getattr(response, "parts", []):
            if hasattr(part, "function_call") and part.function_call:
                function_call = part.function_call
                break
            if hasattr(part, "text") and part.text:
                response_text += part.text

        # No function call → just return the text
        if function_call is None:
            return StreamingResponse(
                _stream_text(response_text or "Done."),
                media_type="text/event-stream",
            )

        fn_name = function_call.name
        fn_args = dict(function_call.args) if function_call.args else {}

        result = await _execute_agent_function(fn_name, fn_args, user_id, req.lat, req.lng)
        return _respond_from_result(result)

    except Exception as e:
        print(f"❌ Cortex agent error: {e}")
        return StreamingResponse(
            _stream_text("I'm having trouble with that request. Please try again."),
            media_type="text/event-stream",
        )


async def _execute_agent_function(
    fn_name: str, args: dict, user_id: str, lat: float, lng: float
) -> dict:
    """Execute whatever function Gemini decided to call."""
    try:
        if fn_name == "search_items":
            return await handle_search_items(args, lat, lng, user_id)

        if fn_name == "book_service":
            return await handle_book_service(user_id, args)

        if fn_name == "get_store_info":
            return await handle_get_store_info(args)

        return {"type": "text", "text": f"Unknown action: {fn_name}"}
    except Exception as e:
        print(f"❌ Agent function '{fn_name}' failed: {e}")
        return {"type": "text", "text": "I couldn't complete that action."}


# ════════════════════════════════════════════════════════════
# Stream generators (unchanged interface — frontend already handles)
# ════════════════════════════════════════════════════════════

async def _stream_text(text: str):
    words = text.split()
    for i, word in enumerate(words):
        yield f"data: {json.dumps({'text': word + (' ' if i < len(words) - 1 else '')})}\n\n"
    yield "data: [DONE]\n\n"


async def _stream_action(action: dict):
    yield f"data: {json.dumps({'type': 'action', 'data': action['data']})}\n\n"
    yield "data: [DONE]\n\n"


async def _stream_text_and_action(action: dict):
    query = action["data"]["query"]
    results = action["data"]["results"]
    count = len(results)
    intro = f"I found {count} result{'s' if count != 1 else ''} matching '{query}':"
    for word in intro.split():
        yield f"data: {json.dumps({'text': word + ' '})}\n\n"
    yield f"data: {json.dumps({'type': 'action', 'data': action['data']})}\n\n"
    yield "data: [DONE]\n\n"


# ════════════════════════════════════════════════════════════
# Backward compatibility — imported by events.py and legacy callers
# ════════════════════════════════════════════════════════════

async def _process_ask(
    query: str,
    lat: float,
    lng: float,
    radius_km: float,
    conversation_history: List[Dict[str, str]],
    user_id: Optional[str] = None,
):
    """
    Legacy entry point. Some modules (e.g. app/routes/events.py) still
    import this. Delegates to seai_ask() with a wrapped request object.
    """
    req = AskRequest(
        query=query,
        lat=lat,
        lng=lng,
        radius_km=radius_km,
        conversation_history=conversation_history,
    )
    return await seai_ask(
        req=req,
        current_user={"id": user_id} if user_id else None,
    )