import os
import re
import json
import base64
import asyncio
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from pydantic import BaseModel

from .auth import get_current_user

# ✅ Reuse the already-configured Gemini client from llm_agent.
try:
    import google.generativeai as genai
    _GEMINI_SDK_AVAILABLE = True
except ImportError:
    genai = None
    _GEMINI_SDK_AVAILABLE = False

router = APIRouter(prefix="/storekeeper", tags=["AI Tools"])

# ── Groq client (lazy, safe if key missing) ──────────────────
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

try:
    from app.services.llm_agent import (
        GROQ_MODEL as REWRITE_MODEL,
        GROQ_VISION_MODELS as VISION_MODEL_CANDIDATES,
        GEMINI_VISION_MODELS as GEMINI_VISION_CANDIDATES,
        GEMINI_ENABLED,
    )
except Exception:
    REWRITE_MODEL = "openai/gpt-oss-120b"
    VISION_MODEL_CANDIDATES = [
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "meta-llama/llama-4-maverick-17b-128e-instruct",
    ]
    GEMINI_VISION_CANDIDATES = [
        "gemini-3.6-flash",
        "gemini-2.0-flash-exp",
        "gemini-1.5-flash",
    ]
    GEMINI_ENABLED = _GEMINI_SDK_AVAILABLE

# ✅ Cache the winning model per provider so we skip dead attempts.
_working_gemini_vision_model: Optional[str] = None
_working_groq_vision_model: Optional[str] = None


class RewriteRequest(BaseModel):
    title: str
    category: Optional[str] = None
    mode: str = "title"


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


def _is_model_unusable(err: Exception) -> bool:
    """
    Detect any error that means 'this model name isn't usable right now'.
    Covers: model_not_found, model_decommissioned, 404s, deprecation, etc.
    We want the loop to keep trying instead of failing fast on these.
    """
    msg = str(err).lower()
    phrases = (
        "model_not_found",
        "model_decommissioned",
        "decommissioned",
        "does not exist",
        "do not have access",
        "no longer supported",
        "no longer available",
        "not found",
        "unsupported",
        "404",
    )
    return any(p in msg for p in phrases)


# ============================================================
# Diagnostics
# ============================================================
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


# ============================================================
# Text-only rewrite — title OR description
# ============================================================
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


# ============================================================
# Vision provider helpers
# ============================================================
async def _try_gemini_vision(
    prompt: str, image_bytes: bytes, mime: str,
) -> tuple[Optional[dict], Optional[str]]:
    """Try each Gemini vision model in order until one returns JSON.
    Returns (parsed_dict, used_model) on success, (None, error_msg) on failure."""
    global _working_gemini_vision_model

    if not GEMINI_ENABLED or genai is None:
        return None, "Gemini not configured"

    # If we've already found a working one, use only that.
    candidates = (
        [_working_gemini_vision_model]
        if _working_gemini_vision_model
        else list(GEMINI_VISION_CANDIDATES)
    )

    last_err: Optional[str] = None
    for model_name in candidates:
        print(f"🤖 [vision/gemini] trying model={model_name} mime={mime} bytes={len(image_bytes)}", flush=True)
        try:
            def _sync_call():
                model = genai.GenerativeModel(model_name)
                return model.generate_content([
                    prompt,
                    {"mime_type": mime, "data": image_bytes},
                ])
            response = await asyncio.to_thread(_sync_call)
            raw = getattr(response, "text", "") or ""
            if not raw.strip():
                raise ValueError("empty response from Gemini")
            parsed = _extract_json(raw)
            _working_gemini_vision_model = model_name
            print(f"✅ [vision/gemini] model={model_name} succeeded, cached", flush=True)
            return parsed, model_name
        except Exception as e:
            last_err = str(e)
            if _is_model_unusable(e):
                print(f"⚠️ [vision/gemini] {model_name} not usable, trying next", flush=True)
                continue
            print(f"⚠️ [vision/gemini] {model_name} error (non-model), trying next: {e}", flush=True)
            continue

    return None, last_err or "All Gemini vision models failed"


async def _try_groq_vision(
    prompt: str, image_bytes: bytes, mime: str,
) -> tuple[Optional[dict], Optional[str]]:
    """Fallback Groq path. Returns (parsed_dict, used_model) or (None, error)."""
    global _working_groq_vision_model

    if _groq_client is None:
        return None, "Groq not configured"

    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"

    candidates = (
        [_working_groq_vision_model]
        if _working_groq_vision_model
        else list(VISION_MODEL_CANDIDATES)
    )

    last_err: Optional[str] = None
    for model_name in candidates:
        print(f"🤖 [vision/groq] trying model={model_name} mime={mime} bytes={len(image_bytes)}", flush=True)
        try:
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
            raw = response.choices[0].message.content or ""
            if not raw.strip():
                raise ValueError("empty response from Groq")
            parsed = _extract_json(raw)
            _working_groq_vision_model = model_name
            print(f"✅ [vision/groq] model={model_name} succeeded, cached", flush=True)
            return parsed, model_name
        except Exception as e:
            last_err = str(e)
            if _is_model_unusable(e):
                print(f"⚠️ [vision/groq] {model_name} not usable, trying next", flush=True)
                continue
            print(f"⚠️ [vision/groq] {model_name} error (non-model), trying next: {e}", flush=True)
            continue

    return None, last_err or "All Groq vision models failed"


# ============================================================
# Vision rewrite — Gemini first, Groq fallback
# ============================================================
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
        raise HTTPException(status_code=413, detail="Image too large. Please use a photo under 8MB.")

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
            "don't discard their intent:\n" + "\n".join(existing_parts) + "\n"
        )

    prompt = _VISION_PROMPT_TEMPLATE.format(
        categories_line=categories_line,
        existing_line=existing_line,
    )

    # ✅ Gemini first — it's the stable vision provider.
    parsed, gemini_err = await _try_gemini_vision(prompt, image_bytes, mime)

    # ✅ Groq as opportunistic fallback if Gemini is fully unavailable.
    if parsed is None:
        print(f"↩️ [vision] Gemini exhausted ({gemini_err}); falling back to Groq", flush=True)
        parsed, groq_err = await _try_groq_vision(prompt, image_bytes, mime)
        if parsed is None:
            print(f"❌ [vision] all providers failed. gemini={gemini_err} groq={groq_err}", flush=True)
            raise HTTPException(
                status_code=500,
                detail=(
                    "AI vision isn't available on this server right now. "
                    "An admin can check GET /storekeeper/gemini-models and GET /storekeeper/groq-models."
                ),
            )

    print(f"✅ [vision] parsed keys={list(parsed.keys())}", flush=True)

    return {
        "item_identified": str(parsed.get("item_identified", ""))[:120],
        "title": str(parsed.get("title", ""))[:200],
        "description": str(parsed.get("description", ""))[:800],
        "category_hint": str(parsed.get("category_hint", "")).strip().lower(),
        "condition_hint": str(parsed.get("condition_hint", "")).strip(),
        "confidence": str(parsed.get("confidence", "medium")).strip().lower(),
    }