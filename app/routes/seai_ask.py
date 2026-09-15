import json
import os
import re
import uuid
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Dict

from groq import AsyncGroq

from app.db.database import database
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


# ════════════════════════════════════════════════════════════
# SYSTEM PROMPT — reserve_item listed FIRST with explicit examples
# ════════════════════════════════════════════════════════════
SYSTEM_PROMPT = """You are SEAI, the AI shopping assistant for Admerce — a hyper-local commerce marketplace in Nigeria.

You have FOUR tools:

1. reserve_item(query, quantity)
   Reserve a physical PRODUCT. Use whenever the user says RESERVE, HOLD, BUY, or GRAB
   and the thing is a physical product (phone, shoe, tv, shirt, food item, etc.).
   Pass ONLY the product name as query. DO NOT include the store name.
   Examples:
     "reserve this iphone"        → reserve_item(query="iPhone 14")
     "Reserve an iphone 14 from graham hub" → reserve_item(query="iPhone 14")
     "hold 2 of those shoes"      → reserve_item(query="shoes", quantity=2)
     "buy that tv"                → reserve_item(query="TV")

2. search_items(query)
   Use when the user wants to FIND, SEE, BROWSE, or EXPLORE anything.
   Examples: "find iphone", "show me pizza", "I'm looking for shoes".

3. book_service(service)
   Use ONLY when the user names a SERVICE that a provider delivers:
   haircut, phone repair, plumbing, cleaning, catering, photography, etc.
   NOT for physical products. If they say "reserve" and it's a product, use reserve_item.
   Examples: "book a haircut", "book phone repair", "schedule a plumber".

4. get_store_info(store)
   Use when the user asks about a specific store by name.

Rules:
- NEVER say "I found N results". Write like a person recommending things.
- Mention travel time or distance naturally.
- Keep responses under 40 words unless asked for detail."""


def _tools():
    # reserve_item listed FIRST so Groq sees it before book_service.
    return [
        {
            "type": "function",
            "function": {
                "name": "reserve_item",
                "description": (
                    "Reserve a physical PRODUCT (phone, shoe, tv, shirt, food item). "
                    "Use whenever the user says reserve, hold, or buy. "
                    "Pass ONLY the product name — never the store name."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Short product name, e.g. 'iPhone 14'",
                        },
                        "quantity": {
                            "type": "integer",
                            "description": "How many. Default 1.",
                            "default": 1,
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_items",
                "description": "Search products, services, and stores near the user.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "book_service",
                "description": (
                    "Book a SERVICE delivered by a provider — haircut, repair, "
                    "plumber, cleaning, catering. NOT for physical products."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_store_info",
                "description": "Get details about a specific store by name.",
                "parameters": {
                    "type": "object",
                    "properties": {"store": {"type": "string"}},
                    "required": ["store"],
                },
            },
        },
    ]


@router.post("/ask")
async def seai_ask(
    req: AskRequest,
    current_user: Optional[dict] = Depends(get_optional_user),
):
    user_id = current_user["id"] if current_user else None
    if not GROQ_ENABLED or _groq is None:
        return await _regex_fallback(req, user_id)
    return await _groq_agent_loop(req, user_id)


async def _groq_agent_loop(req: AskRequest, user_id: Optional[str]):
    messages: list = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in req.conversation_history[-8:]:
        role = m.get("role", "user")
        if role in ("user", "assistant") and m.get("content"):
            messages.append({"role": role, "content": m["content"]})
    messages.append({"role": "user", "content": req.query})

    cards_payload: Optional[dict] = None

    try:
        resp = await _groq.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            tools=_tools(),
            tool_choice="auto",
            max_tokens=400,
            temperature=0.4,           # lower = more deterministic tool picking
        )
        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)

        if not tool_calls:
            return StreamingResponse(
                _stream_text(msg.content or "How can I help?"),
                media_type="text/event-stream",
            )

        call = tool_calls[0]
        fn = call.function.name
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}

        print(f"🔧 SEAI tool call: {fn}({args})", flush=True)

        result = await _execute_agent_function(fn, args, user_id, req.lat, req.lng)

        if result.get("type") == "action" and result.get("intent") == "search_results":
            cards_payload = result["data"]

        if cards_payload is None:
            return _respond_from_result(result)

        compact = _compact_for_llm(result)
        messages.append(msg)
        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(compact),
        })

        final = await _groq.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            max_tokens=140,
            temperature=0.7,
        )
        intro = (final.choices[0].message.content or "").strip().strip('"')
        if not intro:
            intro = _fallback_intro(cards_payload.get("results", []))

        return StreamingResponse(
            _stream_text_and_action(intro, cards_payload),
            media_type="text/event-stream",
        )

    except Exception as e:
        print(f"❌ SEAI agent error: {e}")
        return await _regex_fallback(req, user_id)


# ════════════════════════════════════════════════════════════
# TOOL DISPATCHER — with smart cross-tool fallback
# ════════════════════════════════════════════════════════════
async def _execute_agent_function(
    fn_name: str, args: dict, user_id: Optional[str], lat: float, lng: float
) -> dict:
    try:
        if fn_name == "search_items":
            return await handle_search_items(args, lat, lng, user_id)

        if fn_name == "reserve_item":
            if not user_id:
                return {"type": "text", "text": "Please log in first to reserve items."}
            query = args.get("query", "").strip()
            return await _handle_reserve_item(
                user_id, query, int(args.get("quantity", 1) or 1)
            )

        if fn_name == "book_service":
            result = await handle_book_service(user_id or "", args)

            # ── Fallback: Groq mis-classified a product as a service ──
            # If the service lookup failed, try reserve_item with the same arg.
            if (
                result.get("type") == "text"
                and "No service found" in result.get("text", "")
                and user_id
            ):
                service_arg = (
                    args.get("service", "")
                    or args.get("query", "")
                    or args.get("service_name", "")
                ).strip()
                print(
                    f"↩️  book_service failed, retrying as reserve_item('{service_arg}')",
                    flush=True,
                )
                reserve_result = await _handle_reserve_item(user_id, service_arg, 1)
                return reserve_result

            return result

        if fn_name == "get_store_info":
            return await handle_get_store_info(args)

        return {"type": "text", "text": f"Unknown action: {fn_name}"}
    except Exception as e:
        print(f"❌ Agent function '{fn_name}' failed: {e}")
        return {"type": "text", "text": "I couldn't complete that action."}


# ════════════════════════════════════════════════════════════
# RESERVE HANDLER — token-based fuzzy match
# ════════════════════════════════════════════════════════════
STOP_WORDS = {
    "the", "a", "an", "of", "for", "this", "that", "at",
    "in", "on", "and", "or", "to", "with", "from", "my",
}


def _tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in words if w not in STOP_WORDS and len(w) > 1]


async def _find_best_listing(query: str) -> Optional[dict]:
    tokens = _tokenize(query)
    if not tokens:
        return None

    or_parts = []
    params: dict = {}
    for i, tok in enumerate(tokens):
        or_parts.append(
            f"LOWER(l.title) LIKE :w{i} OR LOWER(COALESCE(s.name, '')) LIKE :w{i}"
        )
        params[f"w{i}"] = f"%{tok}%"

    where = " OR ".join(f"({p})" for p in or_parts)

    score_terms = []
    for i, tok in enumerate(tokens):
        score_terms.append(f"CASE WHEN LOWER(l.title) LIKE :w{i} THEN 3 ELSE 0 END")
        score_terms.append(
            f"CASE WHEN LOWER(COALESCE(s.name, '')) LIKE :w{i} THEN 1 ELSE 0 END"
        )
    score_expr = " + ".join(score_terms)

    sql = f"""
        SELECT
            l.listing_id,
            l.title,
            l.price,
            l.quantity_available,
            l.store_id,
            s.owner_id,
            s.name AS store_name,
            ({score_expr}) AS score
        FROM listings l
        LEFT JOIN stores s ON l.store_id = s.store_id
        WHERE ({where})
          AND (l.quantity_available IS NULL OR l.quantity_available > 0)
        ORDER BY score DESC, LENGTH(l.title) ASC, l.created_at DESC
        LIMIT 5
    """

    rows = await database.fetch_all(sql, params)
    if not rows:
        return None

    top = dict(rows[0])
    if top.get("score", 0) <= 0:
        return None
    return top


async def _handle_reserve_item(
    user_id: str, query: str, quantity: int = 1
) -> dict:
    if not query:
        return {"type": "text", "text": "Which item would you like to reserve?"}

    if quantity < 1:
        quantity = 1

    listing = await _find_best_listing(query)

    if not listing:
        return {
            "type": "text",
            "text": (
                f"I couldn't find an item matching '{query}'. "
                "Try the exact product name, like 'iPhone 14'."
            ),
        }

    listing_id = listing["listing_id"]
    storekeeper_id = listing.get("owner_id")
    store_name = listing.get("store_name") or "the store"
    price = float(listing.get("price") or 0)
    total = price * quantity
    title = listing.get("title") or "this item"

    if not storekeeper_id:
        return {"type": "text",
                "text": f"'{title}' isn't currently available for reservation."}

    if user_id == storekeeper_id:
        return {"type": "text",
                "text": "That's your own listing — you can't reserve it."}

    available = listing.get("quantity_available")
    if available is not None and available < quantity:
        return {"type": "text",
                "text": f"Only {available} left in stock for '{title}'."}

    wallet = await database.fetch_one(
        "SELECT balance FROM wallets WHERE user_id = :uid", {"uid": user_id}
    )
    if not wallet:
        return {"type": "text",
                "text": "Please create a wallet first before reserving."}

    balance = float(wallet["balance"] or 0)
    if balance < total:
        return {
            "type": "text",
            "text": (
                f"You need ₦{total:,.0f} to reserve {quantity}× {title}, "
                f"but your balance is ₦{balance:,.0f}. Top up first."
            ),
        }

    order_id = f"ord_{uuid.uuid4().hex[:8]}"
    now = datetime.utcnow()
    expires_at = now + timedelta(hours=2)

    await database.execute(
        "UPDATE wallets SET balance = balance - :amt WHERE user_id = :uid",
        {"amt": total, "uid": user_id},
    )

    await database.execute(
        """
        INSERT INTO escrow (
            order_id, shopper_id, storekeeper_id, courier_id,
            listing_id, quantity, item_amount, delivery_fee, total_amount,
            status, expires_at, created_at
        )
        VALUES (
            :order_id, :shopper_id, :storekeeper_id, NULL,
            :listing_id, :quantity, :item_amount, 0, :total_amount,
            'locked', :expires_at, :created_at
        )
        """,
        {
            "order_id": order_id,
            "shopper_id": user_id,
            "storekeeper_id": storekeeper_id,
            "listing_id": listing_id,
            "quantity": quantity,
            "item_amount": price,
            "total_amount": total,
            "expires_at": expires_at,
            "created_at": now,
        },
    )

    try:
        await database.execute(
            """
            INSERT INTO wallet_transactions
                (user_id, amount, type, description, reference, status, created_at)
            VALUES
                (:uid, :amt, 'debit', :desc, :ref, 'completed', :now)
            """,
            {
                "uid": user_id,
                "amt": total,
                "desc": f"Reserved {quantity}× {title}",
                "ref": order_id,
                "now": now,
            },
        )
    except Exception as e:
        print(f"⚠️  wallet_transactions insert failed: {e}")

    if available is not None:
        await database.execute(
            "UPDATE listings SET quantity_available = quantity_available - :q "
            "WHERE listing_id = :lid AND quantity_available >= :q",
            {"q": quantity, "lid": listing_id},
        )

    return {
        "type": "text",
        "text": (
            f"✅ Reserved {quantity}× {title} at {store_name} "
            f"for ₦{total:,.0f}. Order #{order_id[:8]} — pick up within 2 hours."
        ),
    }


def _compact_for_llm(result: dict) -> dict:
    if result.get("type") != "action":
        return result
    data = result.get("data", {})
    items = data.get("results", [])[:6]
    slim = [
        {
            "type": r.get("type"),
            "title": r.get("title"),
            "price": r.get("price"),
            "store_name": r.get("store_name") or r.get("provider_name"),
            "distance_km": r.get("distance_km"),
            "travel_minutes": r.get("travel_minutes"),
            "address": r.get("address"),
        }
        for r in items
    ]
    return {"intent": result.get("intent"), "query": data.get("query"), "results": slim}


def _fallback_intro(results: list) -> str:
    if not results:
        return "Nothing turned up nearby — want me to keep looking?"
    if len(results) == 1:
        r = results[0]
        bits = [r.get("title") or "One option"]
        sn = r.get("store_name") or r.get("provider_name")
        if sn:
            bits.append(f"at {sn}")
        if r.get("travel_minutes"):
            bits.append(f"~{r['travel_minutes']} min away")
        return " ".join(bits) + "."
    return f"{len(results)} options nearby — the closest is {results[0].get('title', 'the first one')}."


async def _regex_fallback(req: AskRequest, user_id: Optional[str]):
    intent, params = classify_intent(req.query)
    if intent == "search":
        r = await handle_search_items(params, req.lat, req.lng, user_id)
        return _respond_from_result(r)
    if intent == "reserve_item":
        if not user_id:
            return StreamingResponse(
                _stream_text("Please log in first to reserve items."),
                media_type="text/event-stream",
            )
        r = await _handle_reserve_item(
            user_id,
            params.get("listing_name", "").strip(),
            int(params.get("quantity") or 1),
        )
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
    words = text.split()
    for i, w in enumerate(words):
        yield f"data: {json.dumps({'text': w + (' ' if i < len(words) - 1 else '')})}\n\n"
    yield "data: [DONE]\n\n"


async def _stream_text_and_action(intro: str, data: dict):
    if intro:
        words = intro.split()
        for i, w in enumerate(words):
            yield f"data: {json.dumps({'text': w + (' ' if i < len(words) - 1 else '')})}\n\n"
    yield f"data: {json.dumps({'type': 'action', 'intent': 'search_results', 'data': data})}\n\n"
    yield "data: [DONE]\n\n"


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
    return await seai_ask(
        req=req,
        current_user={"id": user_id} if user_id else None,
    )