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

# ── Gemini SDK ────────────────────────────────────────────────
try:
    import google.generativeai as genai
    _GEMINI_SDK_AVAILABLE = True
except ImportError:
    genai = None
    _GEMINI_SDK_AVAILABLE = False

router = APIRouter(prefix="/storekeeper", tags=["AI Tools"])


# ═══════════════════════════════════════════════════════════════
# Clients
# ═══════════════════════════════════════════════════════════════

# ── Groq (chat + vision fallback) ─────────────────────────────
try:
    from groq import Groq
    _groq_key = os.getenv("GROQ_API_KEY")
    _groq_client = Groq(api_key=_groq_key) if _groq_key else None
    if _groq_client:
        print("✅ AI Tools: Groq rewrite enabled")
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
        )
        if _nvidia_key
        else None
    )
    if _nvidia_client:
        print("✅ AI Tools: NVIDIA NIM vision enabled")
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
    # Fallback no-op implementations
    def should_skip_model(_: str) -> bool:
        return False
    def mark_model_rate_limited(_: str, __: int = 60) -> None:
        pass
    def classify_error(_: str) -> str:
        return "other"


# ── Groq vision candidates ────────────────────────────────────
# NOTE: Groq rotates its vision lineup frequently. Run
# `GET /storekeeper/groq-models` to see the LIVE list for your
# account, then trim this list to what's available. Dead models
# are skipped automatically — keeping extras costs one wasted
# request the first time, then they're marked unusable for the
# process lifetime.
GROQ_VISION_CANDIDATES = [
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "qwen/qwen3-vl-235b-a22b-instruct",
    "meta-llama/llama-3.2-90b-vision-preview",
    "meta-llama/llama-3.2-11b-vision-preview",
]


# ── NVIDIA NIM vision candidates ──────────────────────────────
# NVIDIA NIM exposes OpenAI-compatible vision endpoints. Model
# IDs tend to be stable across quarters.
NVIDIA_VISION_CANDIDATES = [
    "meta/llama-3.2-90b-vision-instruct",
    "meta/llama-3.2-11b-vision-instruct",
    "nvidia/nemotron-nano-12b-v2-vl",
    "microsoft/phi-3.5-vision-instruct",
]


# ── Sticky working model per provider ─────────────────────────
# We use mutable single-element lists so helper functions can
# mutate them (Python has no `global` for dict entries).
_gemini_sticky: list[Optional[str]] = [None]
_groq_sticky: list[Optional[str]] = [None]
_nvidia_sticky: list[Optional[str]] = [None]


# ═══════════════════════════════════════════════════════════════
# Response cache (image hash → result)
# ═══════════════════════════════════════════════════════════════
# In-process; survives until the worker restarts. On a single
# Render worker this is more than enough — the same photo re-
# uploaded within a day returns instantly with zero API calls.
_VISION_CACHE: dict[str, tuple[dict, float]] = {}
VISION_CACHE_TTL_SEC = 24 * 60 * 60


def _cache_key(image_bytes: bytes, title: str, description: str, category: str) -> str:
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
    # Light eviction — cap at 5,000 entries
    if len(_VISION_CACHE) > 5000:
        oldest = sorted(_VISION_CACHE.items(), key=lambda kv: kv[1][1])[:500]
        for k, _ in oldest:
            _VISION_CACHE.pop(k, None)


# ═══════════════════════════════════════════════════════════════
# Text rewrite endpoint (unchanged)
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


def _detect_image_mime(content_type: Optional[str], filename: Optional[str]) -> str:
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


def _is_model_unusable(err_text: str) -> bool:
    """Detect dead/unknown model names."""
    return classify_error(err_text) == "model"


# ═══════════════════════════════════════════════════════════════
# Generic provider runner
# ═══════════════════════════════════════════════════════════════
async def _try_provider_vision(
    provider_label: str,
    candidates: list[str],
    sticky_ref: list[Optional[str]],
    call_model: Callable[[str], Awaitable[str]],
) -> tuple[Optional[dict], Optional[str]]:
    """
    Try each candidate model in order. Sticky-first, then all others.
    Skips models in the 60s cooldown. Marks 429s for cooldown.
    Returns (parsed_dict, error_message).
    """
    sticky = sticky_ref[0]
    ordered: list[str] = []
    if sticky and sticky in candidates:
        ordered.append(sticky)
    for m in candidates:
        if m != sticky:
            ordered.append(m)

    # Drop any currently rate-limited models
    viable = [m for m in ordered if not should_skip_model(m)]
    if not viable:
        return None, f"{provider_label}: all models rate-limited"

    last_err: Optional[str] = None
    for model_name in viable:
        try:
            raw = await call_model(model_name)
            if not raw or not raw.strip():
                raise ValueError("empty response")
            parsed = _extract_json(raw)
            sticky_ref[0] = model_name
            print(
                f"✅ [vision/{provider_label}] model={model_name} succeeded",
                flush=True,
            )
            return parsed, None
        except Exception as e:
            err_text = str(e)
            last_err = err_text
            kind = classify_error(err_text)
            if kind == "quota":
                mark_model_rate_limited(model_name)
                print(
                    f"⏸️  [vision/{provider_label}] {model_name} rate-limited, trying next",
                    flush=True,
                )
            elif kind == "model":
                print(
                    f"⚠️ [vision/{provider_label}] {model_name} not usable, trying next",
                    flush=True,
                )
            else:
                print(
                    f"⚠️ [vision/{provider_label}] {model_name} error: {err_text[:200]}",
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
    """Returns an async callable for the generic runner."""
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
    data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"

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
    data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"

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


# ═══════════════════════════════════════════════════════════════
# Vision endpoint — 3 providers, cache, cooldown
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

    # ── Cache hit? ────────────────────────────────────────────
    cache_key = _cache_key(image_bytes, title, description, category)
    cached = _cache_get(cache_key)
    if cached is not None:
        print(
            f"🎯 [vision] cache hit, saved an API call "
            f"(bytes={len(image_bytes)})",
            flush=True,
        )
        return cached

    mime = _detect_image_mime(image.content_type, image.filename)

    # ── Build prompt ──────────────────────────────────────────
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
            "don't discard their intent:\n" + "\n".join(existing_parts) + "\n"
        )

    prompt = _VISION_PROMPT_TEMPLATE.format(
        categories_line=categories_line,
        existing_line=existing_line,
    )

    # ── Provider chain ────────────────────────────────────────
    attempts: list[str] = []
    parsed: Optional[dict] = None

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

    # 2. Groq
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

    # 3. NVIDIA NIM
    if parsed is None and _nvidia_client is not None and NVIDIA_VISION_CANDIDATES:
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

    # ── All failed ────────────────────────────────────────────
    if parsed is None:
        print(f"❌ [vision] all providers failed. {' | '.join(attempts)}", flush=True)
        raise HTTPException(
            status_code=503,
            detail=(
                "AI vision is temporarily unavailable. This usually means "
                "our AI providers have hit their rate limit. Try again in "
                "a minute, or fill in the details manually."
            ),
        )

    # ── Shape + cache ─────────────────────────────────────────
    result = {
        "item_identified": str(parsed.get("item_identified", ""))[:120],
        "title": str(parsed.get("title", ""))[:200],
        "description": str(parsed.get("description", ""))[:800],
        "category_hint": str(parsed.get("category_hint", "")).strip().lower(),
        "condition_hint": str(parsed.get("condition_hint", "")).strip(),
        "confidence": str(parsed.get("confidence", "medium")).strip().lower(),
    }

    _cache_set(cache_key, result)
    print(f"✅ [vision] parsed keys={list(result.keys())}", flush=True)
    return result


# ═══════════════════════════════════════════════════════════════
# Diagnostics
# ═══════════════════════════════════════════════════════════════
@router.get("/groq-models")
async def list_groq_models(current_user: dict = Depends(get_current_user)):
    """Every model the current Groq account can access."""
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
        raise HTTPException(status_code=500, detail=f"Could not list models: {e}")


@router.get("/gemini-models")
async def list_gemini_models(current_user: dict = Depends(get_current_user)):
    """Every model the current Gemini API key can access."""
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
        raise HTTPException(status_code=500, detail=f"Could not list models: {e}")


@router.get("/vision-status")
async def vision_status(current_user: dict = Depends(get_current_user)):
    """
    Snapshot of vision config + recent quota state. Handy for
    debugging which models are alive on this process.
    """
    return {
        "gemini": {
            "enabled": bool(GEMINI_ENABLED and genai is not None),
            "candidates": GEMINI_VISION_CANDIDATES,
            "sticky": _gemini_sticky[0],
        },
        "groq": {
            "enabled": _groq_client is not None,
            "candidates": GROQ_VISION_CANDIDATES,
            "sticky": _groq_sticky[0],
        },
        "nvidia": {
            "enabled": _nvidia_client is not None,
            "candidates": NVIDIA_VISION_CANDIDATES,
            "sticky": _nvidia_sticky[0],
        },
        "cache": {
            "entries": len(_VISION_CACHE),
            "ttl_sec": VISION_CACHE_TTL_SEC,
        },
    }