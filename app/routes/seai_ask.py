import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
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

# ── NVIDIA NIM (fallback, OpenAI-compatible) ─────────────────
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


SYSTEM_PROMPT = """You are SEAI, the AI shopping assistant for Admerce — a hyper-local commerce marketplace in Nigeria.

You have FOUR tools:

1. reserve_item(query, quantity)
   Reserve a physical ITEM. Use when the user says RESERVE, HOLD, BUY, GRAB
   and the thing is a physical item (phone, shoe, tv, shirt, food).
   Pass ONLY the item name — never the store name.
   Examples:
     "reserve this iphone"                  → reserve_item(query="iPhone 14")
     "Reserve an iphone 14 from graham hub" → reserve_item(query="iPhone 14")
     "hold 2 of those shoes"                → reserve_item(query="shoes", quantity=2)

2. search_items(query)
   Search ITEMS, SERVICES, and STORES near the user.
   Use whenever the user wants to FIND, SEE, BROWSE, EXPLORE, or SEARCH — whether the thing is a physical item OR a service OR a store.
   The tool returns the best matches across all three categories, sorted by distance.
   Examples:
     "find iphone"                    → search_items(query="iPhone 14")
     "show me pizza"                  → search_items(query="pizza")
     "I'm looking for shoes"          → search_items(query="shoes")
     "where can I get a haircut"      → search_items(query="haircut")
     "show me cleaning services"      → search_items(query="cleaning")
     "I need a plumber"               → search_items(query="plumber")
     "any phone repairs around?"      → search_items(query="phone repair")
     "services near me"               → search_items(query="services")
     "what stores are open"           → search_items(query="stores")

3. book_service(service)
   BOOK/COMMIT to a service the user already wants. Only when the user says BOOK, SCHEDULE, or confirms a specific service.
   NOT for browsing or finding services.
   Examples:
     "book a haircut for tomorrow"    → book_service(service="haircut")
     "schedule a plumber"             → book_service(service="plumber")
     "I want to book cleaning"        → book_service(service="cleaning")

4. get_store_info(store)
   Get details about a specific store BY NAME. Only when the user names a store.

## CRITICAL — search vs book
This is the most common mistake. Get it right:
- "Find a plumber"        → search_items (user wants to SEE options)
- "Show me plumbers"      → search_items
- "Any plumbers around?"  → search_items
- "Book a plumber"        → book_service (user wants to COMMIT)
- "Schedule a plumber"    → book_service
- "I'll take the second one" → book_service

If the user hasn't explicitly said "book", "schedule", or "reserve", use search_items.

## CRITICAL — multiple items in one query
If the user asks for MORE THAN ONE thing ("I need a laptop, a suit, and meatballs"),
call search_items ONCE FOR EACH ITEM. Do NOT collapse into one search. Do NOT ask
permission to search for the others. Fire all searches in the same turn.
Then in your reply, summarize the best match for each item in one short response.

Examples of multi-item queries:
  "I need a laptop, a suit, and meatballs"
     → search_items("laptop") + search_items("suit") + search_items("meatballs")
  "Find me a phone and a charger"
     → search_items("phone") + search_items("charger")
  "Looking for shoes, a shirt and a watch"
     → search_items("shoes") + search_items("shirt") + search_items("watch")

If the user asks for ONE thing, fire ONE search. Do not over-search.

## LANGUAGE RULES
- Always say "item" or "items". NEVER say "product" or "products".
- Match Admerce vocabulary: item, listing, store, service.

## CRITICAL RULES

### When to use a tool:
- User names or implies an ITEM, SERVICE, or STORE to find/browse → search_items
- User wants to RESERVE a physical item → reserve_item
- User wants to BOOK a specific service (committed) → book_service
- User asks about a specific store by name → get_store_info

### When NOT to use a tool:
- Follow-up questions: "but why", "why not", "how come", "hmm", "ok", "tell me more"
- Greetings: "hi", "hello", "hey"
- Meta questions: "what can you do", "how does this work", "are you sure"
- Thanks: "thanks", "thank you", "nice"

For these, respond CONVERSATIONALLY using the previous turn as context.
NEVER re-call the previous tool for a follow-up question.

### Image search
The Admerce SEAI interface has an attach button (➕) next to the text input.
Users can tap it to upload a photo. That photo is analyzed by a separate
vision service which identifies the object and shows matching listings
nearby as cards.

If the user asks about images, photos, or visual search:
- Tell them to tap the ➕ button next to the input to upload a photo
- The image will be analyzed and matching items shown as cards
- NEVER claim SEAI has no image support — it does, through the attach flow
- NEVER say "I don't have image-analysis built into this chat"

You personally cannot see images typed into the chat. But the app can
analyze uploaded photos. Guide users to the ➕ button instead of denying
the capability.

### When you have search results:
The tools already return ONLY the closest, most relevant matches for the query.
- Mention the CLOSEST ONE or TWO results per item — never list them all.
- Lead with distance ("Just 2 min away…", "Right around the corner at…").
- Say the store/provider name and price naturally.
- For MULTI-ITEM queries, give one line per item.
- Do NOT enumerate more than 2 results per item unless the user asked to see everything.
- If the top result is a SERVICE, mention the provider name and service name.
- If the top result is a STORE, mention the store name and what they sell.

### Explaining empty results:
If your previous search found nothing and the user asks why, explain clearly:
- "I searched items and services in your area and none matched '<query>'."
- Suggest a next step: "Try a broader term" or "Want a wanted alert so you're notified when one is posted?"

### Never:
- Say "product" — always "item"
- Dump every result the tool returned — pick the best 1–2 per item
- Repeat the previous response verbatim
- Say "I found N results"
- Call search_items as a reflex for conversational messages
- Route "find a service" to book_service
- Ask permission to search for the other items in a multi-item query — just search them
- Deny having image search capability

Keep responses under 80 words unless asked for detail."""


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
                        "query": {
                            "type": "string",
                            "description": "Short item name, e.g. 'iPhone 14'",
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
                "description": (
                    "Search ITEMS, SERVICES, and STORES near the user. "
                    "Use for any find/show/browse/look-for query — whether "
                    "the user wants a physical item, a service (plumber, "
                    "haircut, cleaning), or a store. Returns the best matches "
                    "across all three categories, sorted by distance. "
                    "If the user asks for multiple items in one message, call "
                    "this tool once for each item."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Search terms — item name, service type, or "
                                "store name. E.g. 'iPhone 14', 'plumber', "
                                "'haircut', 'Graham Hub'."
                            ),
                        },
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
                    "service — never for browsing. If the user is just looking "
                    "for services, use search_items instead."
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
]


def _is_conversational(text: str) -> bool:
    t = text.strip().lower()
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
# ENTRYPOINT — provider fallback chain
# ═══════════════════════════════════════════════════════════════════

@router.post("/ask")
async def seai_ask(
    req: AskRequest,
    current_user: Optional[dict] = Depends(get_optional_user),
):
    user_id = current_user["id"] if current_user else None

    # 1) Groq (primary — fastest, cheapest)
    if GROQ_ENABLED and _groq is not None:
        try:
            return await _agent_loop(req, user_id, _groq, GROQ_MODEL)
        except Exception as e:
            print(f"❌ Groq agent failed, trying NVIDIA: {e}", flush=True)

    # 2) NVIDIA NIM (OpenAI-compatible, same tool-calling interface)
    if NVIDIA_ENABLED and _nvidia is not None:
        try:
            return await _agent_loop(req, user_id, _nvidia, NVIDIA_MODEL)
        except Exception as e:
            print(f"❌ NVIDIA agent failed, falling back to regex: {e}", flush=True)

    # 3) Regex fallback (no LLM — always works)
    return await _regex_fallback(req, user_id)


# ═══════════════════════════════════════════════════════════════════
# AGENT LOOP (provider-agnostic — pass any OpenAI-compatible client)
# ═══════════════════════════════════════════════════════════════════

async def _agent_loop(req: AskRequest, user_id: Optional[str], client, model: str):
    messages: list = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in req.conversation_history[-8:]:
        role = m.get("role", "user")
        if role in ("user", "assistant") and m.get("content"):
            messages.append({"role": role, "content": m["content"]})
    messages.append({"role": "user", "content": req.query})

    conversational = _is_conversational(req.query)
    cards_payload: Optional[dict] = None

    # ── First call: only send tools when we actually want tool-calling ──
    # Never send tools alongside tool_choice="none". Groq's gpt-oss-120b
    # hallucinates a tool call anyway and 400s. When conversational, strip
    # tools from the payload entirely.
    first_kwargs = {
        "model": model,
        "messages": messages,
        "max_tokens": 400,
        "temperature": 0.4,
    }
    if not conversational:
        first_kwargs["tools"] = _tools()
        first_kwargs["tool_choice"] = "auto"

    try:
        resp = await client.chat.completions.create(**first_kwargs)
    except Exception as e:
        err = str(e).lower()
        # Belt-and-suspenders: if the model still refused, retry text-only
        if "tool" in err and not conversational:
            print(f"⚠️  Tool call rejected, retrying text-only: {e}", flush=True)
            retry_kwargs = {
                "model": model,
                "messages": messages,
                "max_tokens": 400,
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

    # ── Execute EVERY tool call the model returned ──────────────────
    # OpenAI-compatible models fire multiple calls in one response when
    # the user asks for multiple things (e.g. laptop + suit + meatballs).
    # Executing only the first discards the rest, which is why SEAI used to
    # find one item and then ask permission to search for the others.
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
        cards_payload = {
            "query": primary_query or req.query,
            "results": deduped[:5],
        }

    result = last_result if last_result is not None else {
        "type": "text",
        "text": "I couldn't complete that action.",
    }

    # ── Empty result: ask the model for a clean, natural reply ──
    # Use a fresh message list. Do NOT append the assistant message
    # containing tool_calls then call without tools — Groq rejects that
    # as "tool_choice is none, but model called a tool".
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

    # ── Have cards: ask the model for a short intro ──
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
                "Write a 1-2 sentence intro highlighting the closest / best option "
                "for each item. Do NOT list them all. Do NOT say 'I found N results'."
            ),
        },
    ]
    try:
        final = await client.chat.completions.create(
            model=model,
            messages=intro_messages,
            max_tokens=160,
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
    # Aware UTC — matches TIMESTAMPTZ columns in the DB
    now = datetime.now(timezone.utc)
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
    """Cap at 4 — enough for the LLM to pick the top 1–2 per item, not enough to dump."""
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

    # Multi-item: one line per item
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
            line += f" — ₦{int(float(price)):,}"
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