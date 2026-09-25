import os
import re
import json
import base64
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from pydantic import BaseModel

from .auth import get_current_user

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

# ✅ Source model names from llm_agent so there's ONE place to update.
try:
    from app.services.llm_agent import (
        GROQ_MODEL as REWRITE_MODEL,
        GROQ_VISION_MODEL as REWRITE_VISION_MODEL,
    )
except Exception:
    REWRITE_MODEL = "openai/gpt-oss-120b"
    REWRITE_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"


class RewriteRequest(BaseModel):
    title: str
    category: Optional[str] = None
    # ✅ NEW — 'title' (default, backward-compatible) or 'description'.
    #    The old code always used the title prompt, which truncated
    #    descriptions to 120 chars and rewrote them as titles.
    mode: str = "title"


# ── Vision prompt ─────────────────────────────────────────────
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
    """Pick a MIME type for the data URL. Prefer the client-provided one."""
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
    """Tolerant JSON extraction — strips markdown fences, falls back to regex."""
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

    # Field name stays `rewritten_title` for backward compat with the
    # existing frontend. When mode='description', callers read the same
    # field but treat it as a description.
    return {"rewritten_title": rewritten, "original": req.title, "mode": mode}


# ============================================================
# Vision rewrite — sees the actual product photo
# ============================================================
@router.post("/rewrite-listing-vision")
async def rewrite_listing_vision(
    image: UploadFile = File(...),
    title: str = Form(""),
    description: str = Form(""),
    category: str = Form(""),
    current_user: dict = Depends(get_current_user),
):
    """
    Look at the product photo and return a full suggestion set:
    title, description, category_hint, condition_hint.

    The seller's existing typed text (if any) is passed as context so
    the AI improves it rather than discarding it.
    """
    if _groq_client is None:
        raise HTTPException(
            status_code=503,
            detail="AI vision service is temporarily unavailable.",
        )

    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Empty image file")
    if len(image_bytes) > 8 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail="Image too large. Please use a photo under 8MB.",
        )

    mime = _detect_image_mime(image.content_type, image.filename)
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"

    # Optional category hint from the seller's selection
    categories_line = ""
    if category.strip():
        categories_line = (
            f"\nThe seller has pre-selected the category '{category.strip()}'. "
            f"Prefer that if it's correct; otherwise pick the closest match.\n"
        )

    # Optional existing text from the seller
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

    try:
        response = _groq_client.chat.completions.create(
            model=REWRITE_VISION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                    ],
                }
            ],
            temperature=0.7,
            max_tokens=500,
        )
        raw = response.choices[0].message.content or ""
    except Exception as e:
        print(f"❌ Vision rewrite failed: {e}", flush=True)
        raise HTTPException(
            status_code=500,
            detail="AI couldn't analyse the photo. Please try again.",
        )

    try:
        parsed = _extract_json(raw)
    except Exception as e:
        print(f"❌ Vision JSON parse failed: {e} | raw={raw[:300]}", flush=True)
        raise HTTPException(
            status_code=500,
            detail="AI returned an unexpected response. Please try again.",
        )

    return {
        "item_identified": str(parsed.get("item_identified", ""))[:120],
        "title": str(parsed.get("title", ""))[:200],
        "description": str(parsed.get("description", ""))[:800],
        "category_hint": str(parsed.get("category_hint", "")).strip().lower(),
        "condition_hint": str(parsed.get("condition_hint", "")).strip(),
        "confidence": str(parsed.get("confidence", "medium")).strip().lower(),
    }