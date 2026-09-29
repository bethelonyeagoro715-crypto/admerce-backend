import os
import re
import json
import base64
import asyncio
import hashlib
import time
from typing import Optional, Callable, Awaitable
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from pydantic import BaseModel

from .auth import get_current_user

try:
    import google.generativeai as genai
    _GEMINI_SDK_AVAILABLE = True
except ImportError:
    genai = None
    _GEMINI_SDK_AVAILABLE = False

router = APIRouter(prefix="/storekeeper", tags=["AI Tools"])


# ═══════════════════════════════════════════════════════════════
# Timeouts — every provider must fail fast, never hang
# ═══════════════════════════════════════════════════════════════
GEMINI_TIMEOUT_SEC = 60
OPENROUTER_TIMEOUT_SEC = 60
GROQ_TIMEOUT_SEC = 20
NVIDIA_TIMEOUT_SEC = 60
PROVIDER_OVERALL_TIMEOUT_SEC = 90


# ═══════════════════════════════════════════════════════════════
# Clients
# ═══════════════════════════════════════════════════════════════

# ── OpenRouter (vision — free tier via `:free` models) ────────
try:
    from openai import OpenAI as _OpenAIForOpenRouter
    _openrouter_key = os.getenv("OPENROUTER_API_KEY")
    _openrouter_client = (
        _OpenAIForOpenRouter(
            api_key=_openrouter_key,
            base_url="https://openrouter.ai/api/v1",
            timeout=OPENROUTER_TIMEOUT_SEC,
            max_retries=0,
            default_headers={
                # Optional — helps OpenRouter attribute traffic; not required.
                "HTTP-Referer": "https://admerce-web-app-ashy.vercel.app",
                "X-Title": "Admerce SEAI",
            },
        )
        if _openrouter_key
        else None
    )
    if _openrouter_client:
        print(
            f"✅ AI Tools: OpenRouter vision enabled "
            f"(timeout={OPENROUTER_TIMEOUT_SEC}s)"
        )
    else:
        print("⚠️ AI Tools: OPENROUTER_API_KEY not set, OpenRouter disabled")
except ImportError:
    _openrouter_client = None
    print("⚠️ AI Tools: openai package not installed (OpenRouter unavailable)")


# ── Groq (chat + optional vision) ─────────────────────────────
try:
    from groq import Groq
    _groq_key = os.getenv("GROQ_API_KEY")
    _groq_client = (
        Groq(api_key=_groq_key, timeout=GROQ_TIMEOUT_SEC)
        if _groq_key
        else None
    )
    if _groq_client:
        print(
            f"✅ AI Tools: Groq rewrite enabled (timeout={GROQ_TIMEOUT_SEC}s)"
        )
    else:
        print("⚠️ AI Tools: GROQ_API_KEY not set, AI endpoints will return 503")
except ImportError:
    _groq_client = None
    print("⚠️ AI Tools: groq package not installed")


# ── NVIDIA NIM (vision + chat, OpenAI-compatible) ─────────────
try:
    from openai import OpenAI
    _nvidia_key = os.getenv("NVIDIA_API_KEY")
    _nvidia_client = (
        OpenAI(
            api_key=_nvidia_key,
            base_url="https://integrate.api.nvidia.com/v1",
            timeout=NVIDIA_TIMEOUT_SEC,
            max_retries=0,
        )
        if _nvidia_key
        else None
    )
    if _nvidia_client:
        print(
            f"✅ AI Tools: NVIDIA NIM vision enabled "
            f"(timeout={NVIDIA_TIMEOUT_SEC}s)"
        )
    else:
        print("⚠️ AI Tools: NVIDIA_API_KEY not set, NVIDIA fallback disabled")
except ImportError:
    _nvidia_client = None
    print("⚠️ AI Tools: openai package not installed")


# ── Constants from llm_agent (single source of truth) ────────
try:
    from app.services.llm_agent import (
        GROQ_MODEL as REWRITE_MODEL,
        GEMINI_VISION_MODELS as GEMINI_VISION_CANDIDATES,
        GEMINI_ENABLED,
        should_skip_model,
        mark_model_rate_limited,
        classify_error,
    )
except Exception as e:
    print(f"⚠️ AI Tools: llm_agent import failed ({e}); using fallbacks")
    REWRITE_MODEL = "openai/gpt-oss-120b"
    GEMINI_VISION_CANDIDATES = [
        "gemini-3.6-flash",
        "gemini-2.5-flash",
        "gemini-flash-latest",
    ]
    GEMINI_ENABLED = _GEMINI_SDK_AVAILABLE

    def should_skip_model(_: str) -> bool:
        return False

    def mark_model_rate_limited(_: str, __: int = 60) -> None:
        pass

    def classify_error(_: str) -> str:
        return "other"


# ═══════════════════════════════════════════════════════════════
# Candidate lists
# ═══════════════════════════════════════════════════════════════
# Groq's vision lineup is dead as of Sept 2026 — all 404 — but kept
# here so if they ship one, we pick it up automatically.
GROQ_VISION_CANDIDATES = [
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "qwen/qwen3-vl-235b-a22b-instruct",
    "meta-llama/llama-3.2-90b-vision-preview",
    "meta-llama/llama-3.2-11b-vision-preview",
]

NVIDIA_VISION_CANDIDATES = [
    "meta/llama-3.2-90b-vision-instruct",
    "meta/llama-3.2-11b-vision-instruct",
    "nvidia/nemotron-nano-12b-v2-vl",
    "microsoft/phi-3.5-vision-instruct",
]

# OpenRouter free vision models — the `:free` suffix is what makes
# them cost $0. Order matters: put the model you like best first.
# Swap by hitting https://openrouter.ai/models?modality=text%2Bimage
# and filtering to free.
OPENROUTER_VISION_CANDIDATES = [
    "inclusionai/ling-3.0-flash-vl:free",
    "qwen/qwen2.5-vl-72b-instruct:free",
    "google/gemini-2.0-flash-exp:free",
    "meta-llama/llama-3.2-90b-vision-instruct:free",
    "meta-llama/llama-3.2-11b-vision-instruct:free",
]

DEAD_MODEL_COOLDOWN_SEC = 3600

_gemini_sticky: list[Optional[str]] = [None]
_groq_sticky: list[Optional[str]] = [None]
_nvidia_sticky: list[Optional[str]] = [None]
_openrouter_sticky: list[Optional[str]] = [None]


# ═══════════════════════════════════════════════════════════════
# Response cache
# ═══════════════════════════════════════════════════════════════
_VISION_CACHE: dict[str, tuple[dict, float]] = {}
VISION_CACHE_TTL_SEC = 24 * 60 * 60


def _cache_key(
    image_bytes: bytes, title: str, description: str, category: str
) -> str:
    h = hashlib.sha256()
    h.update(image_bytes)
    h.update(b"|")
    h.update(title.strip().encode("utf-8"))
    h.update(b"|")
    h.update(description.strip().encode("utf-8"))
    h.update(b"|")
    h.update(category.strip().lower().encode("utf-8"))
    return h.hexdigest()


def _cache_get(key: str) -> Optional[dict]:
    entry = _VISION_CACHE.get(key)
    if not entry:
        return None
    data, ts = entry
    if time.time() - ts > VISION_CACHE_TTL_SEC:
        _VISION_CACHE.pop(key, None)
        return None
    return data


def _cache_set(key: str, data: dict) -> None:
    _VISION_CACHE[key] = (data, time.time())
    if len(_VISION_CACHE) > 5000:
        oldest = sorted(_VISION_CACHE.items(), key=lambda kv: kv[1][1])[:500]
        for k, _ in oldest:
            _VISION_CACHE.pop(k, None)


# ═══════════════════════════════════════════════════════════════
# Text rewrite (unchanged)
# ═══════════════════════════════════════════════════════════════

class RewriteRequest(BaseModel):
    title: str
    category: Optional[str] = None
    mode: str = "title"


@router.post("/rewrite-listing")
async def rewrite_listing(
    req: RewriteRequest,
    current_user: dict = Depends(get_current_user),
):
    if _groq_client is None:
        raise HTTPException(
            status_code=503,
            detail="AI rewrite service is temporarily unavailable.",
        )

    mode = (req.mode or "title").lower()
    category_hint = (
        f" in the '{req.category}' category" if req.category else ""
    )

    if mode == "description":
        prompt = (
            f"You are an expert e-commerce copywriter for a global marketplace.\n"
            f"Rewrite the following product description to be compelling, warm, "
            f"and conversion-focused{category_hint}.\n"
            f"Write 2-4 sentences. Focus on benefits and specifics, not fluff.\n"
            f"Original description: \"{req.title}\"\n"
            f"Return ONLY the rewritten description in plain text, nothing else."
        )
        max_tokens = 300
    else:
        prompt = (
            f"You are an expert e-commerce copywriter for a global marketplace.\n"
            f"Rewrite the following product title to be attention-grabbing, "
            f"keyword-rich, and optimised for high conversion{category_hint}.\n"
            f"Keep the title under 120 characters.\n"
            f"Original title: \"{req.title}\"\n"
            f"Return ONLY the rewritten title in plain text, nothing else."
        )
        max_tokens = 80

    try:
        response = _groq_client.chat.completions.create(
            model=REWRITE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
            max_tokens=max_tokens,
        )
        rewritten = response.choices[0].message.content.strip().strip('"')
    except Exception as e:
        print(f"❌ Rewrite failed: {e}", flush=True)
        raise HTTPException(
            status_code=500,
            detail="Rewrite failed. Please try again.",
        )

    return {"rewritten_title": rewritten, "original": req.title, "mode": mode}


# ═══════════════════════════════════════════════════════════════
# Vision prompt + helpers
# ═══════════════════════════════════════════════════════════════

_VISION_PROMPT_TEMPLATE = """You are a senior e-commerce copywriter for Admerce, a hyperlocal marketplace.

Look at the product photo. Identify what the item is, then write compelling listing copy for it.

{categories_line}{existing_line}

Return STRICT JSON with EXACTLY these keys, no markdown, no explanation, no extra text:
{{
  "item_identified": "short noun phrase describing what you see (2-6 words)",
  "title": "attention-grabbing, keyword-rich title under 120 characters",
  "description": "2-3 warm, human sentences a shopper would want to read. Highlight benefits and condition.",
  "category_hint": "one of: tech_electronics, food_beverage, health_wellness, fashion_apparel, building_industrial, home_garden, kids_toys, sports_outdoors, automotive, media_office",
  "condition_hint": "one of: New, Like New, Good, Fair, Used, Refurbished",
  "confidence": "high | medium | low — how sure you are about the identification"
}}

Output ONLY the JSON object. Nothing before it. Nothing after it."""


def _detect_image_mime(
    content_type: Optional[str], filename: Optional[str]
) -> str:
    if content_type and content_type.startswith("image/"):
        return content_type
    if filename:
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        if ext in ("jpg", "jpeg"):
            return "image/jpeg"
        if ext == "png":
            return "image/png"
        if ext == "webp":
            return "image/webp"
        if ext == "gif":
            return "image/gif"
    return "image/jpeg"


def _extract_json(text: str) -> dict:
    if not text:
        raise ValueError("empty response")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


# ═══════════════════════════════════════════════════════════════
# Generic provider runner
# ═══════════════════════════════════════════════════════════════
async def _try_provider_vision(
    provider_label: str,
    candidates: list[str],
    sticky_ref: list[Optional[str]],
    call_model: Callable[[str], Awaitable[str]],
) -> tuple[Optional[dict], Optional[str]]:
    if not candidates:
        return None, f"{provider_label}: no models configured"

    sticky = sticky_ref[0]
    ordered: list[str] = []
    if sticky and sticky in candidates:
        ordered.append(sticky)
    for m in candidates:
        if m != sticky:
            ordered.append(m)

    viable = [m for m in ordered if not should_skip_model(m)]
    if not viable:
        return None, f"{provider_label}: all models rate-limited"

    last_err: Optional[str] = None
    for model_name in viable:
        print(
            f"🤖 [vision/{provider_label}] trying model={model_name}",
            flush=True,
        )
        try:
            raw = await asyncio.wait_for(
                call_model(model_name),
                timeout=PROVIDER_OVERALL_TIMEOUT_SEC,
            )
            if not raw or not raw.strip():
                raise ValueError("empty response")
            parsed = _extract_json(raw)
            sticky_ref[0] = model_name
            print(
                f"✅ [vision/{provider_label}] model={model_name} succeeded",
                flush=True,
            )
            return parsed, None
        except asyncio.TimeoutError:
            last_err = (
                f"{model_name}: timed out after {PROVIDER_OVERALL_TIMEOUT_SEC}s"
            )
            print(
                f"⏱️  [vision/{provider_label}] {model_name} timed out",
                flush=True,
            )
            continue
        except Exception as e:
            err_text = str(e)
            last_err = err_text
            kind = classify_error(err_text)
            if kind == "quota":
                mark_model_rate_limited(model_name)
                print(
                    f"⏸️  [vision/{provider_label}] {model_name} rate-limited, "
                    f"trying next",
                    flush=True,
                )
            elif kind == "model":
                mark_model_rate_limited(model_name, DEAD_MODEL_COOLDOWN_SEC)
                print(
                    f"☠️  [vision/{provider_label}] {model_name} dead, "
                    f"skipping for 1h",
                    flush=True,
                )
            else:
                print(
                    f"⚠️ [vision/{provider_label}] {model_name} error: "
                    f"{err_text[:200]}",
                    flush=True,
                )
            continue

    return None, f"{provider_label}: {last_err}"


# ═══════════════════════════════════════════════════════════════
# Per-provider call adapters
# ═══════════════════════════════════════════════════════════════

async def _gemini_call(
    prompt: str, image_bytes: bytes, mime: str
) -> Callable[[str], Awaitable[str]]:
    async def _call(model_name: str) -> str:
        def _sync():
            model = genai.GenerativeModel(model_name)
            return model.generate_content([
                prompt,
                {"mime_type": mime, "data": image_bytes},
            ])
        response = await asyncio.to_thread(_sync)
        return getattr(response, "text", "") or ""
    return _call


def _groq_call(
    prompt: str, image_bytes: bytes, mime: str
) -> Callable[[str], Awaitable[str]]:
    data_url = (
        f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    )

    async def _call(model_name: str) -> str:
        response = await asyncio.to_thread(
            _groq_client.chat.completions.create,
            model=model_name,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
            temperature=0.7,
            max_tokens=800,
        )
        return response.choices[0].message.content or ""
    return _call


def _nvidia_call(
    prompt: str, image_bytes: bytes, mime: str
) -> Callable[[str], Awaitable[str]]:
    data_url = (
        f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    )

    async def _call(model_name: str) -> str:
        response = await asyncio.to_thread(
            _nvidia_client.chat.completions.create,
            model=model_name,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
            temperature=0.7,
            max_tokens=800,
        )
        return response.choices[0].message.content or ""
    return _call


def _openrouter_call(
    prompt: str, image_bytes: bytes, mime: str
) -> Callable[[str], Awaitable[str]]:
    data_url = (
        f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    )

    async def _call(model_name: str) -> str:
        response = await asyncio.to_thread(
            _openrouter_client.chat.completions.create,
            model=model_name,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
            temperature=0.7,
            max_tokens=800,
        )
        return response.choices[0].message.content or ""
    return _call


# ═══════════════════════════════════════════════════════════════
# Vision endpoint
# ═══════════════════════════════════════════════════════════════
@router.post("/rewrite-listing-vision")
async def rewrite_listing_vision(
    image: UploadFile = File(...),
    title: str = Form(""),
    description: str = Form(""),
    category: str = Form(""),
    current_user: dict = Depends(get_current_user),
):
    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Empty image file")
    if len(image_bytes) > 8 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail="Image too large. Please use a photo under 8MB.",
        )

    cache_key = _cache_key(image_bytes, title, description, category)
    cached = _cache_get(cache_key)
    if cached is not None:
        print(
            f"🎯 [vision] cache hit (bytes={len(image_bytes)})",
            flush=True,
        )
        return cached

    mime = _detect_image_mime(image.content_type, image.filename)

    categories_line = ""
    if category.strip():
        categories_line = (
            f"\nThe seller has pre-selected the category '{category.strip()}'. "
            f"Prefer that if it's correct; otherwise pick the closest match.\n"
        )

    existing_parts = []
    if title.strip():
        existing_parts.append(f'Current title: "{title.strip()}"')
    if description.strip():
        existing_parts.append(f'Current description: "{description.strip()}"')
    existing_line = ""
    if existing_parts:
        existing_line = (
            "\n\nThe seller has already typed the following — improve on it, "
            "don't discard their intent:\n"
            + "\n".join(existing_parts)
            + "\n"
        )

    prompt = _VISION_PROMPT_TEMPLATE.format(
        categories_line=categories_line,
        existing_line=existing_line,
    )

    attempts: list[str] = []
    parsed: Optional[dict] = None
    started_at = time.time()

    # 1. Gemini
    if GEMINI_ENABLED and genai is not None and GEMINI_VISION_CANDIDATES:
        print(
            f"🤖 [vision/gemini] chain start mime={mime} bytes={len(image_bytes)}",
            flush=True,
        )
        parsed, err = await _try_provider_vision(
            "gemini",
            GEMINI_VISION_CANDIDATES,
            _gemini_sticky,
            await _gemini_call(prompt, image_bytes, mime),
        )
        if parsed is None:
            attempts.append(f"gemini={err}")
            print(f"↩️ [vision] Gemini exhausted: {err}", flush=True)

    # 2. OpenRouter
    if (
        parsed is None
        and _openrouter_client is not None
        and OPENROUTER_VISION_CANDIDATES
    ):
        print("↩️ [vision] falling back to OpenRouter", flush=True)
        parsed, err = await _try_provider_vision(
            "openrouter",
            OPENROUTER_VISION_CANDIDATES,
            _openrouter_sticky,
            _openrouter_call(prompt, image_bytes, mime),
        )
        if parsed is None:
            attempts.append(f"openrouter={err}")
            print(f"↩️ [vision] OpenRouter exhausted: {err}", flush=True)

    # 3. NVIDIA NIM
    if (
        parsed is None
        and _nvidia_client is not None
        and NVIDIA_VISION_CANDIDATES
    ):
        print("↩️ [vision] falling back to NVIDIA", flush=True)
        parsed, err = await _try_provider_vision(
            "nvidia",
            NVIDIA_VISION_CANDIDATES,
            _nvidia_sticky,
            _nvidia_call(prompt, image_bytes, mime),
        )
        if parsed is None:
            attempts.append(f"nvidia={err}")
            print(f"↩️ [vision] NVIDIA exhausted: {err}", flush=True)

    # 4. Groq (vision lineup dead — kept for future)
    if parsed is None and _groq_client is not None and GROQ_VISION_CANDIDATES:
        print("↩️ [vision] falling back to Groq", flush=True)
        parsed, err = await _try_provider_vision(
            "groq",
            GROQ_VISION_CANDIDATES,
            _groq_sticky,
            _groq_call(prompt, image_bytes, mime),
        )
        if parsed is None:
            attempts.append(f"groq={err}")
            print(f"↩️ [vision] Groq exhausted: {err}", flush=True)

    elapsed = round(time.time() - started_at, 1)

    if parsed is None:
        print(
            f"❌ [vision] all providers failed in {elapsed}s. "
            f"{' | '.join(attempts)}",
            flush=True,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "AI vision is temporarily unavailable — all providers are "
                "rate-limited or offline. Try again in a few minutes, or "
                "fill in the details manually."
            ),
        )

    result = {
        "item_identified": str(parsed.get("item_identified", ""))[:120],
        "title": str(parsed.get("title", ""))[:200],
        "description": str(parsed.get("description", ""))[:800],
        "category_hint": str(parsed.get("category_hint", "")).strip().lower(),
        "condition_hint": str(parsed.get("condition_hint", "")).strip(),
        "confidence": str(parsed.get("confidence", "medium")).strip().lower(),
    }

    _cache_set(cache_key, result)
    print(
        f"✅ [vision] parsed keys={list(result.keys())} in {elapsed}s",
        flush=True,
    )
    return result


# ═══════════════════════════════════════════════════════════════
# Diagnostics
# ═══════════════════════════════════════════════════════════════
@router.get("/groq-models")
async def list_groq_models(current_user: dict = Depends(get_current_user)):
    if _groq_client is None:
        raise HTTPException(status_code=503, detail="Groq not configured.")
    try:
        models = _groq_client.models.list()
        entries = []
        for m in getattr(models, "data", []) or []:
            entries.append({
                "id": getattr(m, "id", None),
                "owned_by": getattr(m, "owned_by", None),
                "active": getattr(m, "active", None),
            })
        entries.sort(key=lambda e: (e["id"] or "").lower())
        return {"count": len(entries), "models": entries}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Could not list models: {e}"
        )


@router.get("/gemini-models")
async def list_gemini_models(current_user: dict = Depends(get_current_user)):
    if not GEMINI_ENABLED or genai is None:
        raise HTTPException(status_code=503, detail="Gemini not configured.")
    try:
        def _sync_call():
            return list(genai.list_models())
        models = await asyncio.to_thread(_sync_call)
        entries = []
        for m in models:
            methods = getattr(m, "supported_generation_methods", []) or []
            entries.append({
                "name": getattr(m, "name", None),
                "display_name": getattr(m, "display_name", None),
                "methods": list(methods),
                "supports_vision": "generateContent" in methods,
            })
        entries.sort(key=lambda e: (e["name"] or "").lower())
        return {"count": len(entries), "models": entries}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Could not list models: {e}"
        )


@router.get("/openrouter-models")
async def list_openrouter_models(
    current_user: dict = Depends(get_current_user),
):
    """
    Every OpenRouter vision-capable model on this API key.
    Filter for those with ":free" to build a free-tier chain.
    """
    if _openrouter_client is None:
        raise HTTPException(status_code=503, detail="OpenRouter not configured.")
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://openrouter.ai/api/v1/models",
                headers={
                    "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY', '')}",
                },
                timeout=20,
            )
            r.raise_for_status()
            data = r.json().get("data", [])
        entries = []
        for m in data:
            modality = (m.get("architecture") or {}).get("input_modalities") or []
            if "image" not in modality:
                continue
            entries.append({
                "id": m.get("id"),
                "name": m.get("name"),
                "context_length": m.get("context_length"),
                "pricing": m.get("pricing"),
            })
        entries.sort(key=lambda e: (e["id"] or "").lower())
        return {"count": len(entries), "models": entries}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Could not list models: {e}"
        )


@router.get("/vision-status")
async def vision_status(current_user: dict = Depends(get_current_user)):
    return {
        "gemini": {
            "enabled": bool(GEMINI_ENABLED and genai is not None),
            "candidates": GEMINI_VISION_CANDIDATES,
            "sticky": _gemini_sticky[0],
        },
        "openrouter": {
            "enabled": _openrouter_client is not None,
            "candidates": OPENROUTER_VISION_CANDIDATES,
            "sticky": _openrouter_sticky[0],
        },
        "nvidia": {
            "enabled": _nvidia_client is not None,
            "candidates": NVIDIA_VISION_CANDIDATES,
            "sticky": _nvidia_sticky[0],
        },
        "groq": {
            "enabled": _groq_client is not None,
            "candidates": GROQ_VISION_CANDIDATES,
            "sticky": _groq_sticky[0],
        },
        "cache": {
            "entries": len(_VISION_CACHE),
            "ttl_sec": VISION_CACHE_TTL_SEC,
        },
        "timeouts": {
            "gemini_sec": GEMINI_TIMEOUT_SEC,
            "openrouter_sec": OPENROUTER_TIMEOUT_SEC,
            "groq_sec": GROQ_TIMEOUT_SEC,
            "nvidia_sec": NVIDIA_TIMEOUT_SEC,
            "provider_overall_sec": PROVIDER_OVERALL_TIMEOUT_SEC,
        },
    }