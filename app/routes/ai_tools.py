import os
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/storekeeper", tags=["AI Tools"])

# ── Groq client (lazy, safe if key missing) ──────────────────
try:
    from groq import Groq
    _groq_key = os.getenv("GROQ_API_KEY")
    _groq_client = Groq(api_key=_groq_key) if _groq_key else None
    if _groq_client:
        print("✅ AI Tools: Groq rewrite enabled")
    else:
        print("⚠️ AI Tools: GROQ_API_KEY not set, /rewrite-listing will return 503")
except ImportError:
    _groq_client = None
    print("⚠️ AI Tools: groq package not installed")

# ✅ Source the model name from llm_agent so we have ONE place to
#    update when Groq rotates models. Falls back to the known-good
#    string if the import ever fails.
try:
    from app.services.llm_agent import GROQ_MODEL as REWRITE_MODEL
except Exception:
    REWRITE_MODEL = "openai/gpt-oss-120b"


class RewriteRequest(BaseModel):
    title: str
    category: Optional[str] = None


@router.post("/rewrite-listing")
async def rewrite_listing(req: RewriteRequest):
    if _groq_client is None:
        raise HTTPException(
            status_code=503,
            detail="AI rewrite service is temporarily unavailable.",
        )

    category_hint = f" in the '{req.category}' category" if req.category else ""
    prompt = (
        f"You are an expert e-commerce copywriter for a global marketplace.\n"
        f"Rewrite the following product title to be attention-grabbing, keyword-rich, "
        f"and optimised for high conversion, exactly like a top Amazon listing.\n"
        f"Use compelling adjectives, highlight benefits, and include search keywords"
        f"{category_hint}.\n"
        f"Keep the title under 120 characters.\n"
        f"Original title: \"{req.title}\"\n"
        f"Return ONLY the rewritten title in plain text, nothing else."
    )

    try:
        response = _groq_client.chat.completions.create(
            model=REWRITE_MODEL,          # ✅ was "llama-3.1-8b-instant" (retired)
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
            max_tokens=60,
        )
        rewritten = response.choices[0].message.content.strip().strip('"')
    except Exception as e:
        print(f"❌ Rewrite failed: {e}", flush=True)
        raise HTTPException(
            status_code=500,
            detail="Rewrite failed. Please try again.",
        )

    return {"rewritten_title": rewritten, "original": req.title}