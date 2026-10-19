import json
import os
import re
import uuid
import asyncio
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
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


SYSTEM_PROMPT = """You are SEAI, the ultimate AI Universal Advisor and Economic Strategist for Admerce — a hyper-local commerce marketplace in Nigeria.

## YOUR PERSONA
- **Universal Expert**: You are an expert in EVERYTHING available on the marketplace: Tech (phones, laptops), Fashion, Home goods, Food, and Services (plumbers, cleaners, designers).
- **Economic Strategist**: You understand budgets. You must clearly categorize your recommendations into **Budget**, **Balanced**, and **Premium** when possible.
- **Ruthless & Logical**: If the user asks for something harmful or contradictory (e.g., "I want a brand new iPhone 15 for ₦50,000"), you must logically and firmly tell them why it's impossible. 
- **Medical/Nutrition Expert**: If the user mentions a health condition (diabetes, hypertension, pregnancy), you must switch to "Doctor Mode", prescribe safe meals, and warn them against harmful items.
- **Proximity**: Always prioritize items and services that are closest to the user (low `travel_minutes`).

## YOUR TOOLS
You have THREE tools:

1. `search_items(query, max_price)`
   Search for anything on the marketplace.
   *Multiple Searches*: If a user asks for a recommendation, you MUST run multiple searches to find the best options across the tiers.
   
   **CRITICAL - BUDGET ENFORCEMENT**: If the user mentions a specific amount of money (e.g., "I have 500 naira", "under 10k", "maximum 2000"), you MUST pass that number into the `max_price` parameter.
   - Example: "I have only 500 naira what can I buy to eat" -> `search_items(query="meal", max_price=500)`
   - Example: "I want a cheap laptop" (no specific number) -> `search_items(query="laptop")`

2. `reserve_item(query, quantity)`
   Reserve a physical item.

3. `book_service(service)`
   Book a service (e.g., a plumber, cleaner, or doctor).

## CRITICAL RULES
- **Never recommend items above the user's budget.** If they say they have 500 naira, do not show them a 2,000 naira item.
- **Clarify if Vague**: If the user asks "I want food" and hasn't stated a budget, ask "What kind of food? And do you have a budget in mind?".

## HOW TO RESPOND
1. Acknowledge their request.
2. Run the necessary `search_items` tool calls. **PASS THE MAX_PRICE IF MENTIONED.**
3. Analyze the results.
4. Format your response highlighting the best options.

Keep the response concise (under 100 words)."""


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
                    "Use for any find/show/browse/look-for query. "
                    "If the user asks for multiple items (e.g. 'diabetic food' and 'vegetables'), "
                    "call this tool once per item."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search terms."},
                        "max_price": {"type": "number", "description": "The maximum price the user is willing to pay. Omit if no budget is mentioned."}
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "book_service",
                "description": (
                    "COMMIT to booking a service the user has already chosen. "
                    "Only for explicit BOOK / SCHEDULE / RESERVE requests on a "
                    "service — never for browsing."
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


_CONVERSATIONAL_PATTERNS = [
    r"^(but\s+)?why(\s+not)?[\?\.!]*$",
    r"^how\s+come[\?\.!]*$",
    r"^hmm+[\?\.!]*$",
    r"^ok(ay)?[\?\.!]*$",
    r"^thanks?( you)?[\?\.!]*$",
    r"^hi+[\?\.!]*$",
    r"^hey+[\?\.!]*$",
    r"^hello+[\?\.!]*$",
    r"^yes[\?\.!]*$",
    r"^no[\?\.!]*$",
    r"^really[\?\.!]*$",
    r"^are\s+you\s+sure[\?\.!]*$",
    r"^what[\?\.!]+$",
    r"^what\s+do\s+you\s+mean[\?\.!]*$",
    r"^tell\s+me\s+more[\?\.!]*$",
    r"^more[\?\.!]*$",
    r"^what\s+can\s+you\s+do[\?\.!]*$",
    r"^are\s+there\s+any[\?\.!]*$",
    r"^(why|how|what)\s+(did|didn't|do|don't|are|aren't|can|could)\s+you\b.*",
]

_SYNONYMS = {
    "hamburgers": "burger",
    "hamburger": "burger",
    "meatpies": "meat pie",
    "meatpie": "meat pie",
    "cellphones": "phone",
    "cellphone": "phone",
    "mobile": "phone",
    "sneakers": "shoes",
    "sneaker": "shoes",
}

def _apply_synonyms(text: str) -> str:
    t = text.lower()
    for k, v in _SYNONYMS.items():
        t = re.sub(rf"\b{k}\b", v, t)
    return t


def _is_conversational(text: str) -> bool:
    t = text.strip().lower()
    
    meta_phrases = [
        "you understand", "you mean", "your search", "your response", 
        "why didn't you", "why did you", "what do you mean", "how did you"
    ]
    if any(p in t for p in meta_phrases):
        return True

    if len(t) > 60:
        return False
    return any(re.match(p, t) for p in _CONVERSATIONAL_PATTERNS)


def _is_empty_result(result: dict) -> bool:
    if result.get("type") != "text":
        return False
    text = (result.get("text") or "").strip()
    return (
        text.startswith("I couldn't find")
        or text.startswith("No ")
        or text.startswith("Nothing ")
    )


# ═══════════════════════════════════════════════════════════════════
# MULTI-ITEM DETECTION (deterministic — no LLM dependency)
# ═══════════════════════════════════════════════════════════════════

_MINOR_WORDS = {
    "size", "color", "colour", "small", "medium", "large",
    "tiny", "huge", "big", "some", "another", "new", "old",
    "red", "blue", "green", "black", "white", "yellow", "pink",
    "purple", "orange", "brown", "grey", "gray", "silver", "gold",
}

_URGENCY_WORDS = {
    "urgently", "asap", "immediately", "quickly", "quick", "fast",
    "now", "today", "right away", "right now",
}

_LEADING_FILLER_RE = re.compile(
    r"^(?:okay|ok|hi|hey|hello|please|so|"
    r"i|you|we|they|he|she|"
    r"have|has|had|having|"
    r"a|an|the|some|another|new|"
    r"friend|friends|that|who|which|"
    r"need|needs|needed|want|wants|wanted|"
    r"to|be|am|is|are|was|were|"
    r"looking|looks|looked|for|"
    r"find|finds|found|show|shows|showed|"
    r"me|my|our|get|gets|got|"
    r"search|searching|"
    r"eat|eats|eating|"
    r"buy|buys|buying|"
    r"and|also|plus)\s*",
    re.IGNORECASE,
)


def _strip_filler(text: str) -> str:
    prev = None
    cur = text.strip().strip(".,!?;:")
    while prev != cur:
        prev = cur
        cur = _LEADING_FILLER_RE.sub("", cur).strip().strip(".,!?;:")
    return cur


def _has_content_word(text: str) -> bool:
    for w in re.findall(r"[a-z]+", text.lower()):
        if len(w) >= 3 and w not in _MINOR_WORDS:
            return True
    return False


def _split_items_heuristically(query: str) -> Optional[List[str]]:
    q = query.strip()
    if not q or len(q) > 400:
        return None

    lower = q.lower()
    if not any(sep in lower for sep in (" and ", ",", " with ", " plus ", " & ", " + ")):
        return None

    parts = re.split(
        r"\s*,\s*(?:and\s+)?|\s+(?:and|with|plus|&|\+)\s+",
        q,
        flags=re.IGNORECASE,
    )

    fragments: List[str] = []
    for p in parts:
        stripped = _strip_filler(p)

        for w in list(_URGENCY_WORDS):
            stripped = re.sub(
                rf"\b{re.escape(w)}\b", "", stripped, flags=re.IGNORECASE
            ).strip()
        stripped = re.sub(r"\s+", " ", stripped).strip()
        
        stripped = _apply_synonyms(stripped)

        if len(stripped) < 3:
            continue
        if not _has_content_word(stripped):
            continue
        fragments.append(stripped)

    if len(fragments) < 2:
        return None
    return fragments


async def _search_multi_items(req: AskRequest, user_id: Optional[str], items: List[str]):
    async def _one(item: str):
        try:
            return await handle_search_items({"query": item}, req.lat, req.lng, user_id)
        except Exception as e:
            print(f"⚠️  multi-item search failed for '{item}': {e}", flush=True)
            return None

    results = await asyncio.gather(*[_one(i) for i in items])

    merged: list = []
    seen = set()
    for r in results:
        if not r or r.get("type") != "action":
            continue
        for item in r.get("data", {}).get("results", []):
            key = (
                item.get("type"),
                item.get("listing_id") or item.get("service_id") or item.get("store_id"),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)

    if not merged:
        return StreamingResponse(
            _stream_text(
                f"I searched for {', '.join(items)} nearby but found nothing. "
                "Try broader terms or a different area."
            ),
            media_type="text/event-stream",
        )

    per_item: Dict[str, int] = {}
    trimmed: list = []
    for r in merged:
        title = (r.get("title") or "").lower()
        matched_item = None
        for it in items:
            if any(tok in title for tok in it.lower().split() if len(tok) >= 3):
                matched_item = it
                break
        k = matched_item or "__other__"
        per_item[k] = per_item.get(k, 0) + 1
        if per_item[k] <= 2:
            trimmed.append(r)
        if len(trimmed) >= 6:
            break

    payload = {"query": req.query, "results": trimmed}

    lines: list = []
    for it in items:
        toks = [t for t in it.lower().split() if len(t) >= 3]
        match = next(
            (r for r in trimmed if any(t in (r.get("title") or "").lower() for t in toks)),
            None,
        )
        if match:
            price = match.get("price")
            tm = match.get("travel_minutes")
            store = match.get("store_name") or match.get("provider_name")
            line = f"• {match.get('title')}"
            if store:
                line += f" at {store}"
            if price:
                try:
                    line += f" — ₦{int(float(price)):,}"
                except Exception:
                    pass
            if tm:
                line += f" (~{tm} min)"
            lines.append(line)
        else:
            lines.append(f"• {it} — nothing nearby")

    intro = "Here's what I found:\n" + "\n".join(lines)

    return StreamingResponse(
        _stream_text_and_action(intro, payload),
        media_type="text/event-stream",
    )


@router.post("/ask")
async def seai_ask(
    req: AskRequest,
    current_user: Optional[dict] = Depends(get_optional_user),
):
    user_id = current_user["id"] if current_user else None

    if not _is_conversational(req.query):
        items = _split_items_heuristically(req.query)
        if items and len(items) >= 2:
            print(f"🧩 Multi-item query detected: {items}", flush=True)
            return await _search_multi_items(req, user_id, items)

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


async def _agent_loop(req: AskRequest, user_id: Optional[str], client, model: str):
    messages: list = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in req.conversation_history[-8:]:
        role = m.get("role", "user")
        if role in ("user", "assistant") and m.get("content"):
            messages.append({"role": role, "content": m["content"]})
    messages.append({"role": "user", "content": req.query})

    conversational = _is_conversational(req.query)
    cards_payload: Optional[dict] = None

    first_kwargs = {
        "model": model,
        "messages": messages,
        "max_tokens": 500,
        "temperature": 0.4,
    }
    if not conversational:
        first_kwargs["tools"] = _tools()
        first_kwargs["tool_choice"] = "auto"

    try:
        resp = await client.chat.completions.create(**first_kwargs)
    except Exception as e:
        err = str(e).lower()
        if "tool" in err and not conversational:
            print(f"⚠️  Tool call rejected, retrying text-only: {e}", flush=True)
            retry_kwargs = {
                "model": model,
                "messages": messages,
                "max_tokens": 500,
                "temperature": 0.4,
            }
            resp = await client.chat.completions.create(**retry_kwargs)
        else:
            raise

    msg = resp.choices[0].message
    tool_calls = getattr(msg, "tool_calls", None)

    if not tool_calls:
        return StreamingResponse(
            _stream_text(msg.content or "How can I help?"),
            media_type="text/event-stream",
        )

    merged_results: list = []
    last_result: Optional[dict] = None
    primary_query: Optional[str] = None

    for call in tool_calls:
        fn = call.function.name
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}

        print(f"🔧 SEAI tool call: {fn}({args})", flush=True)

        r = await _execute_agent_function(fn, args, user_id, req.lat, req.lng)
        last_result = r

        if r.get("type") == "action" and r.get("intent") == "search_results":
            data = r.get("data", {})
            if primary_query is None:
                primary_query = data.get("query")
            merged_results.extend(data.get("results", []))

    if merged_results:
        seen = set()
        deduped = []
        for r in merged_results:
            key = (
                r.get("type"),
                r.get("listing_id") or r.get("service_id") or r.get("store_id"),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(r)
        cards_payload = {"query": primary_query or req.query, "results": deduped[:5]}

    result = last_result if last_result is not None else {
        "type": "text",
        "text": "I couldn't complete that action.",
    }

    if cards_payload is None and _is_empty_result(result):
        refinement_messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": req.query},
            {
                "role": "system",
                "content": (
                    f"The search returned: {json.dumps(result)}. "
                    "Write a short, natural 1-2 sentence reply explaining that "
                    "nothing matched and suggesting a next step."
                ),
            },
        ]
        try:
            final = await client.chat.completions.create(
                model=model,
                messages=refinement_messages,
                max_tokens=160,
                temperature=0.6,
            )
            refined = (final.choices[0].message.content or "").strip().strip('"')
            if refined:
                return StreamingResponse(
                    _stream_text(refined),
                    media_type="text/event-stream",
                )
        except Exception as e:
            print(f"⚠️  Empty-result refinement failed: {e}", flush=True)
        return _respond_from_result(result)

    if cards_payload is None:
        return _respond_from_result(result)

    compact = _compact_for_llm(
        {"type": "action", "intent": "search_results", "data": cards_payload}
    )
    intro_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": req.query},
        {
            "role": "system",
            "content": (
                f"Search results: {json.dumps(compact)}. "
                "Write the formatted response as instructed (Budget, Balanced, Premium). "
                "Do NOT list them all. Do NOT say 'I found N results'."
            ),
        },
    ]
    try:
        final = await client.chat.completions.create(
            model=model,
            messages=intro_messages,
            max_tokens=300,
            temperature=0.7,
        )
        intro = (final.choices[0].message.content or "").strip().strip('"')
    except Exception as e:
        print(f"⚠️  Intro refinement failed: {e}", flush=True)
        intro = ""

    if not intro:
        intro = _fallback_intro(cards_payload.get("results", []))

    return StreamingResponse(
        _stream_text_and_action(intro, cards_payload),
        media_type="text/event-stream",
    )


async def _execute_agent_function(
    fn_name: str, args: dict, user_id: Optional[str], lat: float, lng: float
) -> dict:
    try:
        if fn_name == "search_items":
            if args.get("query"):
                args["query"] = _apply_synonyms(args.get("query", ""))
            # args contains max_price now, handled by handle_search_items
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

    async def _query_db(search_tokens: list[str]) -> list:
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
        return await database.fetch_all(sql, params)

    rows = await _query_db(tokens)

    if not rows and tokens:
        longest = max(tokens, key=len)
        if len(longest) >= 4:
            rows = await _query_db([longest[:4]])

    if not rows:
        return None

    top = dict(rows[0])
    if top.get("score", 0) <= 0:
        return None
    return top


async def _handle_reserve_item(
    user_id: str, query: str, quantity: int = 1
) -> dict:
    from app.routes.wallet import reserve as wallet_reserve_route, ReserveRequest

    if not query:
        return {"type": "text", "text": "Which item would you like to reserve?"}

    if quantity < 1:
        quantity = 1

    query = _apply_synonyms(query)
    listing = await _find_best_listing(query)

    if not listing:
        return {
            "type": "text",
            "text": (
                f"I couldn't find an item matching '{query}'. "
                "Try the exact item name, like 'iPhone 14'."
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

    order_id = f"ord_{uuid.uuid4().hex[:8]}"

    try:
        req = ReserveRequest(
            order_id=order_id,
            storekeeper_id=storekeeper_id,
            listing_id=listing_id,
            quantity=quantity,
            idempotency_key=f"idem_{uuid.uuid4().hex[:12]}" # Add idempotency key
        )
        current_user = {"id": user_id}
        
        await wallet_reserve_route(req, current_user)
        
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


def _compact_for_llm(result: dict) -> dict:
    if result.get("type") != "action":
        return result
    data = result.get("data", {})
    items = data.get("results", [])[:4]
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

    lines = []
    for r in results[:3]:
        title = r.get("title") or "An option"
        sn = r.get("store_name") or r.get("provider_name")
        price = r.get("price")
        tm = r.get("travel_minutes")
        line = title
        if sn:
            line += f" at {sn}"
        if price:
            try:
                line += f" — ₦{int(float(price)):,}"
            except Exception:
                pass
        if tm:
            line += f" (~{tm} min)"
        lines.append("• " + line)
    return "Nearby options:\n" + "\n".join(lines)


async def _regex_fallback(req: AskRequest, user_id: Optional[str]):
    if _is_conversational(req.query):
        return StreamingResponse(
            _stream_text(
                "I'm here — ask me to find an item or book a service and I'll take it from there."
            ),
            media_type="text/event-stream",
        )
    intent, params = classify_intent(req.query)
    if intent == "search":
        if params.get("query"):
            params["query"] = _apply_synonyms(params["query"])
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