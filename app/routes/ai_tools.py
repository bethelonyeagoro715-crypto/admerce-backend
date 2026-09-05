# app/routes/ai_tools.py (or wherever /storekeeper/rewrite-listing lives)
import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from groq import Groq

router = APIRouter(prefix="/storekeeper", tags=["AI Tools"])

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
groq_client = Groq(api_key=GROQ_API_KEY)

class RewriteRequest(BaseModel):
    title: str
    category: Optional[str] = None   # optional, makes the title more targeted

@router.post("/rewrite-listing")
async def rewrite_listing(req: RewriteRequest):
    category_hint = f" in the '{req.category}' category" if req.category else ""
    prompt = (
        f"You are an expert e‑commerce copywriter for a global marketplace.\n"
        f"Rewrite the following product title to be attention‑grabbing, keyword‑rich, "
        f"and optimised for high conversion, exactly like a top Amazon listing.\n"
        f"Use compelling adjectives, highlight benefits, and include search keywords"
        f"{category_hint}.\n"
        f"Keep the title under 120 characters.\n"
        f"Original title: \"{req.title}\"\n"
        f"Return ONLY the rewritten title in plain text, nothing else."
    )
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",   # free on Groq
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
            max_tokens=60,
        )
        rewritten = response.choices[0].message.content.strip().strip('"')
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Rewrite failed: {str(e)}")

    return {"rewritten_title": rewritten, "original": req.title}