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

# ✅ Import the model chain. Fall back to a known candidate if the
#    import fails for any reason.
try:
    from app.services.llm_agent import (
        GROQ_MODEL as REWRITE_MODEL,
        GROQ_VISION_MODELS as VISION_MODEL_CANDIDATES,
    )
except Exception:
    REWRITE_MODEL = "openai/gpt-oss-120b"
    VISION_MODEL_CANDIDATES = [
        "llama-3.2-11b-vision-preview",
        "llama-3.2-90b-vision-preview",
        "llama-3.2-11b-vision-instruct",
        "meta-llama/llama-4-scout-17b-16e-instruct",
    ]

# ✅ Cache the first vision model that works so subsequent calls skip
#    the dead attempts. Reset on process restart (fine).
_working_vision_model: Optional[str] = None


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


def _is_model_not_found(err: Exception) -> bool:
    """Groq returns 404 with 'model_not_found' when the account can't
    use a model name. Detect it so we can fall through to the next."""
    msg = str(err)
    return (
        "model_not_found" in msg
        or "does not exist" in msg
        or "you do not have access" in msg
    )


# ============================================================
# Diagnostic — list every model this Groq account can access
# ============================================================
@router.get("/groq-models")
async def list_groq_models(current_user: dict = Depends(get_current_user)):
    """
    Returns every model the current Groq account has access to.
    Use this to pick the correct vision model name.
    """
    if _groq_client is None:
        raise HTTPException(
            status_code=503,
            detail="Groq not configured. Check GROQ_API_KEY on the server.",
        )
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
        return {
            "count": len(entries),
            "models": entries,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not list models: {e}",
        )


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
    global _working_vision_model

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

    # Build the list of models to try. If we've already found a working
    # one, just use it — otherwise walk the candidate list.
    if _working_vision_model:
        models_to_try = [_working_vision_model]
    else:
        models_to_try = list(VISION_MODEL_CANDIDATES)

    raw: Optional[str] = None
    last_err: Optional[Exception] = None

    for model_name in models_to_try:
        print(
            f"🤖 [vision] trying model={model_name} "
            f"mime={mime} bytes={len(image_bytes)}",
            flush=True,
        )
        try:
            response = _groq_client.chat.completions.create(
                model=model_name,
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
                max_tokens=800,
            )
            raw = response.choices[0].message.content or ""
            _working_vision_model = model_name
            print(
                f"✅ [vision] model={model_name} succeeded, cached for future calls",
                flush=True,
            )
            break
        except Exception as e:
            last_err = e
            if _is_model_not_found(e):
                print(
                    f"⚠️ [vision] model={model_name} not available, trying next",
                    flush=True,
                )
                continue
            # Any other error — fail fast, don't burn through the list.
            print(f"❌ [vision] model={model_name} failed: {e}", flush=True)
            raise HTTPException(
                status_code=500,
                detail="AI couldn't analyse the photo. Please try again.",
            )

    if raw is None:
        print(
            f"❌ [vision] all candidate models failed. last_err={last_err}",
            flush=True,
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "AI vision isn't available on this server right now. "
                "No supported vision model was found. "
                "An admin can check GET /storekeeper/groq-models."
            ),
        )

    # Diagnostic — see exactly what the model returned
    print(f"🔍 [vision] raw response ({len(raw)} chars): {raw[:1500]}", flush=True)

    try:
        parsed = _extract_json(raw)
    except Exception as e:
        print(f"❌ Vision JSON parse failed: {e}", flush=True)
        raise HTTPException(
            status_code=500,
            detail="AI returned an unexpected response. Please try again.",
        )

    print(
        f"✅ [vision] parsed keys={list(parsed.keys())}",
        flush=True,
    )
    print(
        f"   title={repr(parsed.get('title'))[:120]} "
        f"description={repr(parsed.get('description'))[:120]}",
        flush=True,
    )

    return {
        "item_identified": str(parsed.get("item_identified", ""))[:120],
        "title": str(parsed.get("title", ""))[:200],
        "description": str(parsed.get("description", ""))[:800],
        "category_hint": str(parsed.get("category_hint", "")).strip().lower(),
        "condition_hint": str(parsed.get("condition_hint", "")).strip(),
        "confidence": str(parsed.get("confidence", "medium")).strip().lower(),
    }