import os
import json
import asyncio
from typing import Optional

# ── Groq (primary — fastest, best free tier for chat) ────────
GROQ_MODEL = "openai/gpt-oss-120b"

# ⚠️ Groq's vision lineup is volatile — previews get decommissioned
#    without notice and access varies by account tier. We keep this
#    as an opportunistic fallback; Gemini is the primary vision path.
GROQ_VISION_MODELS = [
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
]

# Backward-compat alias.
GROQ_VISION_MODEL = GROQ_VISION_MODELS[0]

# ✅ Gemini vision chain. Google's vision models are stable and widely
#    available on every tier. We try them in order; the first one that
#    returns a valid JSON response wins and is cached for the session.
GEMINI_VISION_MODELS = [
    "gemini-3.6-flash",       # matches the chat model — has vision
    "gemini-2.0-flash-exp",
    "gemini-1.5-flash",
]

try:
    from groq import AsyncGroq
    _groq_key = os.getenv("GROQ_API_KEY")
    _groq_client = AsyncGroq(api_key=_groq_key) if _groq_key else None
    GROQ_ENABLED = _groq_client is not None
    if GROQ_ENABLED:
        print(f"✅ Groq enabled ({GROQ_MODEL})")
    else:
        print("⚠️ GROQ_API_KEY not set. Groq disabled.")
except ImportError:
    _groq_client = None
    GROQ_ENABLED = False
    print("⚠️ groq package not installed. Install with: pip install groq")

# ── Google Gemini (secondary — strongest for agent + tools) ──
GEMINI_MODEL = "gemini-3.6-flash"
try:
    import google.generativeai as genai
    _gemini_key = os.getenv("GEMINI_API_KEY")
    if _gemini_key:
        genai.configure(api_key=_gemini_key)
        GEMINI_ENABLED = True
        print(f"✅ Gemini enabled ({GEMINI_MODEL})")
    else:
        GEMINI_ENABLED = False
        print("⚠️ GEMINI_API_KEY not set. Gemini disabled.")
except ImportError:
    GEMINI_ENABLED = False
    print("⚠️ google-generativeai not installed. Install with: pip install google-generativeai")

# ── NVIDIA NIM (tertiary — OpenAI-compatible) ────────────────
NVIDIA_MODEL = "nvidia/nemotron-3-super-120b-a12b"
try:
    from openai import AsyncOpenAI
    _nvidia_key = os.getenv("NVIDIA_API_KEY")
    if _nvidia_key:
        _nvidia_client = AsyncOpenAI(
            api_key=_nvidia_key,
            base_url="https://integrate.api.nvidia.com/v1",
        )
        NVIDIA_ENABLED = True
        print(f"✅ NVIDIA NIM enabled ({NVIDIA_MODEL})")
    else:
        _nvidia_client = None
        NVIDIA_ENABLED = False
        print("⚠️ NVIDIA_API_KEY not set. NVIDIA disabled.")
except ImportError:
    _nvidia_client = None
    NVIDIA_ENABLED = False
    print("⚠️ openai package not installed. Install with: pip install openai")


SYSTEM_PROMPT = """You are SEAI, the AI assistant for Admerce, a hyper-local commerce platform.
You can help users with these actions:
- search for items near them (intent: search, params: {"query": "..."})
- book a service (intent: book_service, params: {"service": "..."})
- get info about a store (intent: get_store_info, params: {"store": "..."})

If the user's request matches one of these actions, respond ONLY with a JSON object like:
{"intent": "search", "params": {"query": "pizza"}}
Otherwise, reply naturally with a friendly text answer.
Keep responses brief and helpful (max 2 sentences)."""


def _parse_response(content: str) -> dict:
    if not content:
        return {"type": "text", "text": "..."}

    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict) and "intent" in parsed:
            return {"type": "action", "data": parsed}
    except json.JSONDecodeError:
        pass

    return {"type": "text", "text": cleaned}


async def _call_groq(user_query: str) -> Optional[dict]:
    if not GROQ_ENABLED:
        return None
    try:
        response = await _groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_query},
            ],
            max_tokens=200,
            temperature=0.5,
        )
        content = response.choices[0].message.content or ""
        return _parse_response(content)
    except Exception as e:
        print(f"❌ Groq error: {e}")
        return None


async def _call_gemini(user_query: str) -> Optional[dict]:
    if not GEMINI_ENABLED:
        return None
    try:
        def _sync_call():
            model = genai.GenerativeModel(GEMINI_MODEL)
            return model.generate_content(
                f"{SYSTEM_PROMPT}\n\nUser: {user_query}\nSEAI:"
            )
        response = await asyncio.to_thread(_sync_call)
        content = getattr(response, "text", "") or ""
        return _parse_response(content)
    except Exception as e:
        print(f"❌ Gemini error: {e}")
        return None


async def _call_nvidia(user_query: str) -> Optional[dict]:
    if not NVIDIA_ENABLED:
        return None
    try:
        response = await _nvidia_client.chat.completions.create(
            model=NVIDIA_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_query},
            ],
            max_tokens=200,
            temperature=0.5,
        )
        content = response.choices[0].message.content or ""
        return _parse_response(content)
    except Exception as e:
        print(f"❌ NVIDIA error: {e}")
        return None


async def call_llm(user_query: str) -> dict:
    """Try providers in order: Groq → Gemini → NVIDIA."""
    for provider_fn in (_call_groq, _call_gemini, _call_nvidia):
        result = await provider_fn(user_query)
        if result is not None:
            return result

    return {
        "type": "text",
        "text": "I'm here to help! Try asking me to search for an item, book a service, or get store info."
    }


call_claude = call_llm