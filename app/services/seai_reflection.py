# app/services/seai_reflection.py
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from app.db.database import database

REFERENCE_WINDOW_DAYS = 14
MAX_REFLECTIONS_INJECTED = 3
MAX_CANDIDATES_FETCHED = 10

_table_ready = False


def _tokenize_simple(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2]


async def ensure_reflection_table() -> None:
    """Idempotent — safe to call on every startup."""
    global _table_ready
    if _table_ready:
        return
    try:
        await database.execute("""
            CREATE TABLE IF NOT EXISTS seai_reflections (
                id SERIAL PRIMARY KEY,
                user_id TEXT,
                query TEXT NOT NULL,
                response_text TEXT,
                evaluation JSONB,
                reflection_text TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_seai_reflections_created_at
            ON seai_reflections (created_at DESC)
        """)
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_seai_reflections_failed
            ON seai_reflections ((evaluation->>'passed'))
        """)
        _table_ready = True
    except Exception as e:
        print(f"⚠️  ensure_reflection_table failed: {e}", flush=True)


def _as_dict(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


async def fetch_relevant_reflections(query: str, limit: int = MAX_REFLECTIONS_INJECTED) -> List[dict]:
    """Fetch recent failed reflections, ranked by token overlap with the current query."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=REFERENCE_WINDOW_DAYS)
    try:
        rows = await database.fetch_all(
            """
            SELECT query, reflection_text, evaluation, created_at
            FROM seai_reflections
            WHERE created_at >= :cutoff
              AND (evaluation->>'passed') = 'false'
              AND reflection_text IS NOT NULL
              AND reflection_text <> ''
            ORDER BY created_at DESC
            LIMIT :limit
            """,
            {"cutoff": cutoff, "limit": MAX_CANDIDATES_FETCHED},
        )
    except Exception as e:
        print(f"⚠️  Reflection fetch failed: {e}", flush=True)
        return []

    if not rows:
        return []

    query_tokens = set(_tokenize_simple(query))
    scored = []
    for r in rows:
        d = dict(r)
        d["evaluation"] = _as_dict(d.get("evaluation"))
        rq_tokens = set(_tokenize_simple(d.get("query") or ""))
        overlap = len(query_tokens & rq_tokens)
        scored.append((overlap, d))

    scored.sort(key=lambda x: -x[0])
    return [d for _, d in scored[:limit]]


def format_reflections_for_prompt(reflections: List[dict]) -> str:
    """Build the injection block. Returns empty string when no useful reflections."""
    if not reflections:
        return ""
    lines = ["## PAST MISTAKES — LEARN FROM THESE"]
    seen = set()
    for r in reflections[:MAX_REFLECTIONS_INJECTED]:
        refl = (r.get("reflection_text") or "").strip()
        if not refl or refl in seen:
            continue
        seen.add(refl)
        failed = r.get("evaluation", {}).get("failed_dimensions") or []
        dims = f" [{', '.join(failed)}]" if failed else ""
        lines.append(f"- {refl}{dims}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def _build_rubric_prompt(
    user_query: str,
    response_text: str,
    ceiling: Optional[float],
    tool_calls_made: List[str],
    results_returned: List[dict],
) -> str:
    if ceiling is not None:
        ceiling_line = (
            f"User budget ceiling: ₦{ceiling:,.0f}. "
            f"ANY price in the response above this = FAIL budget_respected."
        )
    else:
        ceiling_line = "No budget ceiling specified."

    slim = [
        {
            "type": r.get("type"),
            "title": r.get("title"),
            "price": r.get("price"),
            "store_name": r.get("store_name") or r.get("provider_name"),
        }
        for r in (results_returned or [])[:8]
    ]

    return f"""Evaluate this AI response against a hard rubric.

USER QUERY: {user_query}

RESPONSE GIVEN: {response_text}

{ceiling_line}

TOOLS CALLED: {tool_calls_made or "none"}

RESULTS RETURNED FROM TOOLS: {json.dumps(slim)}

Rubric (ALL must be TRUE to pass):
1. budget_respected — No price mentioned in RESPONSE GIVEN exceeds the ceiling (only apply if a ceiling exists).
2. tool_called_correctly — If the user asked for items/services, search_items was called.
3. no_hallucinated_store — Every store/provider name in RESPONSE GIVEN appears in RESULTS RETURNED (or no stores were mentioned).
4. user_intent_matched — RESPONSE GIVEN directly answers USER QUERY.

Return STRICT JSON only:
{{"passed": true or false, "failed_dimensions": ["name", ...], "reflection": "one short lesson for next time"}}

If passed=true, reflection must be "".
"""


async def evaluate_response(
    client,
    model: str,
    user_query: str,
    response_text: str,
    ceiling: Optional[float],
    tool_calls_made: List[str],
    results_returned: List[dict],
) -> dict:
    """
    Score the response. Returns:
        {"passed": bool, "failed_dimensions": [...], "reflection": str}
    Swallows all exceptions — returns passed=True on failure so nothing breaks.
    """
    prompt = _build_rubric_prompt(
        user_query, response_text, ceiling, tool_calls_made, results_returned
    )
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a strict evaluator. Output ONLY valid JSON, no prose, no markdown."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=300,
            temperature=0.0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        parsed = json.loads(raw)
    except Exception as e:
        print(f"⚠️  Reflection eval failed: {e}", flush=True)
        return {"passed": True, "failed_dimensions": [], "reflection": ""}

    failed = parsed.get("failed_dimensions") or []
    passed = bool(parsed.get("passed", not failed)) and not failed
    reflection = (parsed.get("reflection") or "").strip()
    return {
        "passed": passed,
        "failed_dimensions": failed,
        "reflection": reflection if not passed else "",
    }


async def store_reflection(
    user_id: Optional[str],
    query: str,
    response_text: str,
    evaluation: dict,
    reflection_text: str,
) -> None:
    try:
        await database.execute(
            """
            INSERT INTO seai_reflections
                (user_id, query, response_text, evaluation, reflection_text)
            VALUES (:uid, :q, :resp, CAST(:eval AS JSONB), :refl)
            """,
            {
                "uid": user_id,
                "q": query,
                "resp": (response_text or "")[:2000],
                "eval": json.dumps(evaluation),
                "refl": reflection_text,
            },
        )
    except Exception as e:
        print(f"⚠️  Reflection store failed: {e}", flush=True)