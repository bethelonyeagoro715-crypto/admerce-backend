import json
import os
import re
import uuid
import asyncio
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, Tuple

from groq import AsyncGroq

from app.db.database import database
from app.routes.auth import get_optional_user
from app.services.seai_agent import (
    classify_intent,
    handle_book_service,
    handle_search_items,
    handle_get_store_info,
    handle_optimize_cart,
)
from app.services.llm_agent import GROQ_ENABLED, GROQ_MODEL
from app.services.seai_reflection import (
    ensure_reflection_table,
    fetch_relevant_reflections,
    format_reflections_for_prompt,
    evaluate_response,
    store_reflection,
)

router = APIRouter(prefix="/seai", tags=["SEAI Agent"])

_groq = AsyncGroq(api_key=os.getenv("GROQ_API_KEY")) if os.getenv("GROQ_API_KEY") else None

try:
    from app.services.llm_agent import (
        _nvidia_client as _nvidia,
        NVIDIA_ENABLED,
        NVIDIA_MODEL,
    )
except Exception:
    _nvidia = None
    NVIDIA_ENABLED = False
    NVIDIA_MODEL = ""


class AskRequest(BaseModel):
    query: str
    lat: float = 6.5244
    lng: float = 3.3792
    radius_km: float = 10.0
    conversation_history: List[Dict[str, str]] = []
    mode: Optional[str] = "gpt"


# ═══════════════════════════════════════════════════════════════════
# BUDGET EXTRACTION — hard ceiling enforced in Python, not prompt
# ═══════════════════════════════════════════════════════════════════

_BUDGET_CUES = (
    "budget", "naira", "₦", "ngn", "afford",
    "spend", "under", "less than", "only have", "can only", "max",
)


def _extract_budget_naira(text: str) -> Optional[float]:
    if not text:
        return None
    lower = text.lower()

    has_cue = any(c in lower for c in _BUDGET_CUES)
    has_k = bool(re.search(r"\d\s*k\b", lower))
    if not has_cue and not has_k:
        return None

    candidates: List[float] = []

    for m in re.finditer(r"[₦n]\s*([\d,]+(?:\.\d+)?)", lower):
        try:
            candidates.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass

    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:k|thousand)\b", lower):
        try:
            candidates.append(float(m.group(1)) * 1000)
        except ValueError:
            pass

    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(?:naira|ngn)\b", lower):
        try:
            candidates.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass

    candidates = [c for c in candidates if 50 <= c <= 50_000_000]
    return max(candidates) if candidates else None


def _under_ceiling(price, ceiling: Optional[float]) -> bool:
    if ceiling is None:
        return True
    if price is None:
        return True
    try:
        return float(price) <= ceiling
    except (TypeError, ValueError):
        return True


SYSTEM_PROMPT = """You are SEAI, the AI Universal Advisor for Admerce — a hyper-local commerce marketplace in Nigeria.

## YOUR REASONING PROTOCOL
Before doing ANYTHING, think through the request inside a <think>...</think> block.
Your thinking is private — users never see it. Inside <think>, work out:

1. **What does the user actually want?** Restate it plainly.
2. **Is this a shopping-list-with-budget request?** If the user gives 2+ distinct items AND a total budget (e.g. "rice, beans, oil for 5000"), call `optimize_cart` ONCE with the full item list and budget. Do NOT call search_items for those. Otherwise, if there are multiple distinct items without a total budget, list each one and call `search_items` once per item.
3. **Budget**: Did the user name a specific number (e.g. "500 naira", "under 10k")? If yes, that exact number becomes max_price for every search_items call.
4. **Synonyms / intent**: Resolve informal words yourself — "sneakers" → "shoes", "mobile" → "phone", "meatpie" → "meat pie", etc. Write the resolved term.
5. **Is this purely conversational** (greeting, "thanks", "why", "ok", follow-up with no new request)? If yes, respond directly — do NOT call any tool.
6. **Health / medical context**: Does the user mention a condition (diabetes, hypertension, pregnancy)? If yes, activate Doctor Mode: prescribe safe options, warn against harmful ones.

Only after your <think> block should you call tools or write a response.

## YOUR TOOLS
- `search_items(query, max_price?)` — find items or services near the user. Call once per distinct item.
- `optimize_cart(items, budget)` — when the user gives 2+ items AND a total budget, find the single cheapest nearby store that carries all of them. Do NOT also call search_items for the same request.
- `reserve_item(query, quantity)` — reserve a physical item the user has chosen.
- `book_service(service)` — book a service the user has explicitly chosen.
- `get_store_info(store)` — get details about a specific store.

## RESPONSE RULES
- **Budget is a hard ceiling.** Never surface an item above max_price. If the user stated a budget, pass it as max_price on EVERY search_items call.
- **Proximity first.** Prefer results with the lowest travel_minutes.
- **Format**: When presenting options, group them as **Budget**, **Balanced**, **Premium** where the results allow it.
- **Brevity**: Final response under 120 words. Do not narrate your tool calls.
- **Impossible requests**: If a budget is physically impossible (e.g. brand-new iPhone for ₦50,000), say so firmly and explain why.
- **Vague requests**: If the user says "I want food" with no other context, ask one clarifying question before searching.
"""


# ═══════════════════════════════════════════════════════════════════
# TOOL DEFINITIONS
# ═══════════════════════════════════════════════════════════════════

def _tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "reserve_item",
                "description": (
                    "Reserve a physical ITEM (phone, shoe, tv, shirt, food item). "
                    "Use whenever the user says reserve, hold, or buy. "
                    "Pass ONLY the item name — never the store name."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Short item name, e.g. 'iPhone 14'"},
                        "quantity": {"type": "integer", "description": "How many. Default 1.", "default": 1},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_items",
                "description": (
                    "Search ITEMS, SERVICES, and STORES near the user. "
                    "Call once per distinct item when the user asks for multiple things. "
                    "Apply synonym resolution before passing the query."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Resolved search term."},
                        "max_price": {
                            "type": "number",
                            "description": "Hard price ceiling. Pass only when the user stated a specific budget.",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "optimize_cart",
                "description": (
                    "Find the SINGLE cheapest nearby store that carries ALL items in the user's "
                    "shopping list, within their total budget. "
                    "Call ONLY when the user gives 2+ distinct items AND a total budget in the same message. "
                    "Do NOT also call search_items for the same request — this tool does its own search."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of item names to buy, e.g. ['rice', 'beans', 'palm oil']",
                        },
                        "budget": {
                            "type": "number",
                            "description": "Total Naira budget across all items.",
                        },
                    },
                    "required": ["items", "budget"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "book_service",
                "description": (
                    "COMMIT to booking a service the user has already chosen. "
                    "Only for explicit BOOK / SCHEDULE / RESERVE on a service — never for browsing."
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


# ═══════════════════════════════════════════════════════════════════
# THINKING BLOCK PARSER
# ═══════════════════════════════════════════════════════════════════

def _extract_thinking(text: str) -> Tuple[str, str]:
    think_match = re.search(r"<think>(.*?)</think>", text, re.DOTALL | re.IGNORECASE)
    thinking = think_match.group(1).strip() if think_match else ""
    visible = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    return thinking, visible


# ═══════════════════════════════════════════════════════════════════
# MAIN ENDPOINT
# ═══════════════════════════════════════════════════════════════════

@router.post("/ask")
async def seai_ask(
    req: AskRequest,
    current_user: Optional[dict] = Depends(get_optional_user),
):
    user_id = current_user["id"] if current_user else None

    if GROQ_ENABLED and _groq is not None:
        try:
            return await _agent_loop(req, user_id, _groq, GROQ_MODEL)
        except Exception as e:
            print(f"❌ Groq agent failed, trying NVIDIA: {e}", flush=True)

    if NVIDIA_ENABLED and _nvidia is not None:
        try:
            return await _agent_loop(req, user_id, _nvidia, NVIDIA_MODEL)
        except Exception as e:
            print(f"❌ NVIDIA agent failed, falling back to regex: {e}", flush=True)

    return await _regex_fallback(req, user_id)


# ═══════════════════════════════════════════════════════════════════
# AGENT LOOP — reasoning + budget clamp + reflexion
# ═══════════════════════════════════════════════════════════════════

async def _agent_loop(req: AskRequest, user_id: Optional[str], client, model: str):
    await ensure_reflection_table()

    ceiling = _extract_budget_naira(req.query)
    if ceiling is not None:
        print(f"💰 SEAI budget ceiling extracted: ₦{ceiling:,.0f}", flush=True)

    past_reflections = await fetch_relevant_reflections(req.query)
    reflection_block = format_reflections_for_prompt(past_reflections)

    system_content = SYSTEM_PROMPT
    if reflection_block:
        system_content = f"{SYSTEM_PROMPT}\n\n{reflection_block}"
        print(
            f"🔁 SEAI injected {len(past_reflections)} past reflection(s) into prompt",
            flush=True,
        )

    messages: List[dict] = [{"role": "system", "content": system_content}]

    for m in req.conversation_history[-8:]:
        role = m.get("role", "user")
        if role in ("user", "assistant") and m.get("content"):
            messages.append({"role": role, "content": m["content"]})

    messages.append({"role": "user", "content": req.query})

    try:
        resp1 = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=600,
            temperature=0.4,
            tools=_tools(),
            tool_choice="auto",
        )
    except Exception as e:
        err = str(e).lower()
        if "tool" in err:
            print(f"⚠️  Tool schema rejected, retrying text-only: {e}", flush=True)
            resp1 = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=600,
                temperature=0.4,
            )
        else:
            raise

    msg1 = resp1.choices[0].message
    tool_calls = getattr(msg1, "tool_calls", None)

    if not tool_calls:
        _, visible = _extract_thinking(msg1.content or "How can I help?")
        final_text = visible or "How can I help?"

        eval_ctx = None
        if ceiling is not None:
            eval_ctx = {
                "client": client,
                "model": model,
                "user_id": user_id,
                "user_query": req.query,
                "response_text": final_text,
                "ceiling": ceiling,
                "tool_calls_made": [],
                "results_returned": [],
            }
        return StreamingResponse(
            _stream_with_eval(final_text, eval_ctx),
            media_type="text/event-stream",
        )

    messages.append({"role": "assistant", "content": msg1.content, "tool_calls": [
        {
            "id": tc.id,
            "type": "function",
            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
        }
        for tc in tool_calls
    ]})

    merged_search_results: List[dict] = []
    primary_query: Optional[str] = None
    last_non_search_result: Optional[dict] = None
    tool_calls_made: List[str] = []

    async def _run_call(tc):
        fn = tc.function.name
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}

        if ceiling is not None and fn == "search_items":
            existing = args.get("max_price")
            if existing is None:
                args["max_price"] = ceiling
            else:
                try:
                    args["max_price"] = min(float(existing), ceiling)
                except (TypeError, ValueError):
                    args["max_price"] = ceiling

        print(f"🔧 SEAI tool: {fn}({args})", flush=True)
        result = await _execute_agent_function(fn, args, user_id, req.lat, req.lng)
        return tc.id, fn, result

    call_results = await asyncio.gather(*[_run_call(tc) for tc in tool_calls])

    for call_id, fn_name, result in call_results:
        tool_calls_made.append(fn_name)
        messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(result),
        })

        if fn_name == "search_items" and result.get("type") == "action":
            data = result.get("data", {})
            if primary_query is None:
                primary_query = data.get("query")
            merged_search_results.extend(data.get("results", []))
        else:
            last_non_search_result = result

    seen_keys: set = set()
    deduped: List[dict] = []
    for r in merged_search_results:
        key = (
            r.get("type"),
            r.get("listing_id") or r.get("service_id") or r.get("store_id"),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if not _under_ceiling(r.get("price"), ceiling):
            continue
        deduped.append(r)

    cards_payload: Optional[dict] = None
    if deduped:
        cards_payload = {"query": primary_query or req.query, "results": deduped[:6]}

    ceiling_note = (
        f" User budget ceiling: ₦{ceiling:,.0f}. Do NOT mention or recommend anything priced above this."
        if ceiling is not None else ""
    )

    if cards_payload:
        compact = _compact_for_llm(
            {"type": "action", "intent": "search_results", "data": cards_payload}
        )
        messages.append({
            "role": "system",
            "content": (
                f"Tool results (compact): {json.dumps(compact)}.{ceiling_note} "
                "Now write your final response. Think first in <think>, "
                "then give the user a concise, well-formatted answer. "
                "Group options as Budget / Balanced / Premium where relevant. "
                "Under 120 words."
            ),
        })
    elif last_non_search_result:
        messages.append({
            "role": "system",
            "content": (
                f"Tool result: {json.dumps(last_non_search_result)}.{ceiling_note} "
                "Acknowledge the outcome naturally and suggest a next step if relevant."
            ),
        })
    else:
        messages.append({
            "role": "system",
            "content": (
                f"All searches returned empty.{ceiling_note} In 1-2 sentences, "
                "tell the user nothing matched and suggest a next step."
            ),
        })

    try:
        resp2 = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=350,
            temperature=0.6,
        )
        raw_final = (resp2.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"⚠️  Pass 2 failed: {e}", flush=True)
        raw_final = ""

    _, final_text = _extract_thinking(raw_final)

    if not final_text:
        if cards_payload:
            final_text = _fallback_intro(cards_payload["results"], ceiling)
        elif last_non_search_result:
            final_text = last_non_search_result.get("text", "Done.")
        else:
            final_text = "I searched but couldn't find anything nearby. Try different terms."

    eval_ctx = {
        "client": client,
        "model": model,
        "user_id": user_id,
        "user_query": req.query,
        "response_text": final_text,
        "ceiling": ceiling,
        "tool_calls_made": tool_calls_made,
        "results_returned": cards_payload["results"] if cards_payload else [],
    }

    if cards_payload:
        return StreamingResponse(
            _stream_with_eval(final_text, eval_ctx, action_payload=cards_payload),
            media_type="text/event-stream",
        )

    return StreamingResponse(
        _stream_with_eval(final_text, eval_ctx),
        media_type="text/event-stream",
    )


# ═══════════════════════════════════════════════════════════════════
# TOOL EXECUTION
# ═══════════════════════════════════════════════════════════════════

async def _execute_agent_function(
    fn_name: str, args: dict, user_id: Optional[str], lat: float, lng: float
) -> dict:
    try:
        if fn_name == "search_items":
            return await handle_search_items(args, lat, lng, user_id)

        if fn_name == "optimize_cart":
            return await handle_optimize_cart(args, lat, lng, user_id)

        if fn_name == "reserve_item":
            if not user_id:
                return {"type": "text", "text": "Please log in first to reserve items."}
            return await _handle_reserve_item(
                user_id,
                args.get("query", "").strip(),
                int(args.get("quantity", 1) or 1),
            )

        if fn_name == "book_service":
            if not user_id:
                return {"type": "text", "text": "Please log in first to book a service."}
            result = await handle_book_service(user_id, args)
            if (
                result.get("type") == "text"
                and "No service found" in result.get("text", "")
            ):
                service = (
                    args.get("service") or args.get("query") or args.get("service_name", "")
                ).strip()
                print(f"↩️  book_service miss → retrying as reserve_item('{service}')", flush=True)
                return await _handle_reserve_item(user_id, service, 1)
            return result

        if fn_name == "get_store_info":
            return await handle_get_store_info(args)

        return {"type": "text", "text": f"Unknown action: {fn_name}"}

    except Exception as e:
        print(f"❌ Tool '{fn_name}' raised: {e}", flush=True)
        return {"type": "text", "text": "I couldn't complete that action."}


# ═══════════════════════════════════════════════════════════════════
# ITEM RESERVATION
# ═══════════════════════════════════════════════════════════════════

STOP_WORDS = {
    "the", "a", "an", "of", "for", "this", "that", "at",
    "in", "on", "and", "or", "to", "with", "from", "my",
}


def _tokenize(text: str) -> List[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in words if w not in STOP_WORDS and len(w) > 1]


async def _find_best_listing(query: str) -> Optional[dict]:
    tokens = _tokenize(query)
    if not tokens:
        return None

    async def _query_db(search_tokens: List[str]) -> list:
        or_parts = []
        params: dict = {}
        for i, tok in enumerate(search_tokens):
            or_parts.append(
                f"LOWER(l.title) LIKE :w{i} OR LOWER(COALESCE(s.name, '')) LIKE :w{i}"
            )
            params[f"w{i}"] = f"%{tok}%"

        where = " OR ".join(f"({p})" for p in or_parts)
        score_terms = []
        for i, tok in enumerate(search_tokens):
            score_terms.append(f"CASE WHEN LOWER(l.title) LIKE :w{i} THEN 3 ELSE 0 END")
            score_terms.append(
                f"CASE WHEN LOWER(COALESCE(s.name, '')) LIKE :w{i} THEN 1 ELSE 0 END"
            )
        score_expr = " + ".join(score_terms)

        sql = f"""
            SELECT
                l.listing_id, l.title, l.price, l.quantity_available,
                l.store_id, s.owner_id, s.name AS store_name,
                ({score_expr}) AS score
            FROM listings l
            LEFT JOIN stores s ON l.store_id = s.store_id
            WHERE ({where})
              AND (l.quantity_available IS NULL OR l.quantity_available > 0)
            ORDER BY score DESC, LENGTH(l.title) ASC, l.created_at DESC
            LIMIT 5
        """
        return await database.fetch_all(sql, params)

    rows = await _query_db(tokens)
    if not rows and tokens:
        longest = max(tokens, key=len)
        if len(longest) >= 4:
            rows = await _query_db([longest[:4]])

    if not rows:
        return None
    top = dict(rows[0])
    return top if top.get("score", 0) > 0 else None


async def _handle_reserve_item(user_id: str, query: str, quantity: int = 1) -> dict:
    from app.routes.wallet import reserve as wallet_reserve_route, ReserveRequest

    if not query:
        return {"type": "text", "text": "Which item would you like to reserve?"}
    if quantity < 1:
        quantity = 1

    listing = await _find_best_listing(query)
    if not listing:
        return {
            "type": "text",
            "text": f"I couldn't find '{query}'. Try the exact item name.",
        }

    listing_id     = listing["listing_id"]
    storekeeper_id = listing.get("owner_id")
    store_name     = listing.get("store_name") or "the store"
    price          = float(listing.get("price") or 0)
    total          = price * quantity
    title          = listing.get("title") or "this item"

    if not storekeeper_id:
        return {"type": "text", "text": f"'{title}' isn't available for reservation."}
    if user_id == storekeeper_id:
        return {"type": "text", "text": "That's your own listing — you can't reserve it."}

    available = listing.get("quantity_available")
    if available is not None and available < quantity:
        return {"type": "text", "text": f"Only {available} left in stock for '{title}'."}

    order_id = f"ord_{uuid.uuid4().hex[:8]}"
    try:
        req = ReserveRequest(
            order_id=order_id,
            storekeeper_id=storekeeper_id,
            listing_id=listing_id,
            quantity=quantity,
            idempotency_key=f"idem_{uuid.uuid4().hex[:12]}",
        )
        await wallet_reserve_route(req, {"id": user_id})
        return {
            "type": "text",
            "text": (
                f"✅ Reserved {quantity}× {title} at {store_name} "
                f"for ₦{total:,.0f}. Order #{order_id[:8]} — pick up within 2 hours."
            ),
        }
    except HTTPException as e:
        return {"type": "text", "text": f"Reservation failed: {e.detail}"}
    except Exception as e:
        return {"type": "text", "text": f"Reservation failed: {str(e)}"}


# ═══════════════════════════════════════════════════════════════════
# RESPONSE HELPERS
# ═══════════════════════════════════════════════════════════════════

def _compact_for_llm(result: dict) -> dict:
    if result.get("type") != "action":
        return result
    data  = result.get("data", {})
    items = data.get("results", [])[:5]
    slim  = [
        {
            "type":         r.get("type"),
            "title":        r.get("title"),
            "price":        r.get("price"),
            "store_name":   r.get("store_name") or r.get("provider_name"),
            "distance_km":  r.get("distance_km"),
            "travel_minutes": r.get("travel_minutes"),
            "address":      r.get("address"),
        }
        for r in items
    ]
    return {"intent": result.get("intent"), "query": data.get("query"), "results": slim}


def _fallback_intro(results: list, ceiling: Optional[float] = None) -> str:
    if ceiling is not None:
        results = [r for r in results if _under_ceiling(r.get("price"), ceiling)]
    if not results:
        return "Nothing nearby — try broader search terms."
    lines = []
    for r in results[:3]:
        title = r.get("title") or "Option"
        sn    = r.get("store_name") or r.get("provider_name")
        price = r.get("price")
        tm    = r.get("travel_minutes")
        line  = title
        if sn:    line += f" at {sn}"
        if price:
            try: line += f" — ₦{int(float(price)):,}"
            except Exception: pass
        if tm:    line += f" (~{tm} min)"
        lines.append("• " + line)
    return "Nearby options:\n" + "\n".join(lines)


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


async def _stream_with_eval(text: str, eval_ctx: Optional[dict], action_payload: Optional[dict] = None):
    """
    Stream the response, then run the reflexion evaluator AFTER [DONE] is sent.
    The client sees the full response before evaluation begins.
    """
    if action_payload:
        async for chunk in _stream_text_and_action(text, action_payload):
            yield chunk
    else:
        async for chunk in _stream_text(text):
            yield chunk

    if eval_ctx:
        try:
            evaluation = await evaluate_response(**eval_ctx)
            print(
                f"🧪 SEAI eval: passed={evaluation['passed']} "
                f"failed={evaluation.get('failed_dimensions') or []}",
                flush=True,
            )
            if not evaluation["passed"] and evaluation.get("reflection"):
                await store_reflection(
                    user_id=eval_ctx.get("user_id"),
                    query=eval_ctx.get("user_query") or "",
                    response_text=eval_ctx.get("response_text") or "",
                    evaluation={
                        "passed": False,
                        "failed_dimensions": evaluation.get("failed_dimensions", []),
                    },
                    reflection_text=evaluation["reflection"],
                )
                print(f"📝 Stored reflection: {evaluation['reflection']}", flush=True)
        except Exception as e:
            print(f"⚠️  Post-stream eval failed: {e}", flush=True)


# ═══════════════════════════════════════════════════════════════════
# REGEX FALLBACK
# ═══════════════════════════════════════════════════════════════════

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
        if not user_id:
            return StreamingResponse(
                _stream_text("Please log in first to book a service."),
                media_type="text/event-stream",
            )
        r = await handle_book_service(user_id, params)
        return _respond_from_result(r)

    if intent == "get_store_info":
        r = await handle_get_store_info(params)
        return _respond_from_result(r)

    return StreamingResponse(
        _stream_text("I'm offline for a moment — try again shortly."),
        media_type="text/event-stream",
    )


# ═══════════════════════════════════════════════════════════════════
# INTERNAL HELPER
# ═══════════════════════════════════════════════════════════════════

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