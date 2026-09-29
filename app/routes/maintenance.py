"""
Admin-only maintenance endpoints.

Currently hosts the image-dimension backfill so it can run without
Render Shell access. Delete this file before real launch.
"""
from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional, Literal
import asyncio
import io

import httpx
from PIL import Image

from app.db.database import database
from app.utils.security import get_current_admin

router = APIRouter(prefix="/maintenance", tags=["Maintenance"])


# Whitelist — prevents SQL injection via the `table` query param.
TABLES: dict[str, dict[str, str]] = {
    "listings": {
        "id_col": "listing_id",
        "url_col": "image_url",
        "w_col": "image_width",
        "h_col": "image_height",
    },
    "services": {
        "id_col": "service_id",
        "url_col": "image_url",
        "w_col": "image_width",
        "h_col": "image_height",
    },
    "stores": {
        "id_col": "store_id",
        "url_col": "store_image_url",
        "w_col": "image_width",
        "h_col": "image_height",
    },
    "users_business_image": {
        "id_col": "id",
        "url_col": "business_image_url",
        "w_col": "business_image_width",
        "h_col": "business_image_height",
    },
    "users_avatar": {
        "id_col": "id",
        "url_col": "avatar_url",
        "w_col": "avatar_width",
        "h_col": "avatar_height",
    },
}

TableName = Literal[
    "listings",
    "services",
    "stores",
    "users_business_image",
    "users_avatar",
]


async def _fetch_dims(
    client: httpx.AsyncClient, url: str
) -> tuple[Optional[int], Optional[int]]:
    try:
        r = await client.get(url, follow_redirects=True, timeout=30)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        w, h = img.size
        if w > 0 and h > 0:
            return int(w), int(h)
    except Exception as e:
        print(f"⚠️  dims fetch failed for {url}: {e}")
    return None, None


@router.post("/backfill-image-dims")
async def backfill_image_dims(
    table: TableName = Query(..., description="Which table to backfill"),
    limit: int = Query(50, ge=1, le=200, description="Rows per call"),
    current_user: dict = Depends(get_current_admin),
):
    """
    Process up to `limit` rows where image dims are NULL.
    Idempotent — re-run until `remaining == 0`.
    """
    cfg = TABLES.get(table)
    if not cfg:
        raise HTTPException(status_code=400, detail=f"Unknown table '{table}'")

    id_col = cfg["id_col"]
    url_col = cfg["url_col"]
    w_col = cfg["w_col"]
    h_col = cfg["h_col"]

    rows = await database.fetch_all(
        f"""
        SELECT {id_col} AS id, {url_col} AS url
        FROM {table}
        WHERE {url_col} IS NOT NULL
          AND {url_col} <> ''
          AND ({w_col} IS NULL OR {h_col} IS NULL)
        LIMIT :limit
        """,
        {"limit": limit},
    )

    if not rows:
        return {
            "table": table,
            "processed": 0,
            "updated": 0,
            "failed": 0,
            "remaining": 0,
            "done": True,
        }

    updated = 0
    failed = 0
    sem = asyncio.Semaphore(5)

    async def process_one(client: httpx.AsyncClient, row) -> bool:
        async with sem:
            w, h = await _fetch_dims(client, row["url"])
            if not w or not h:
                return False
            await database.execute(
                f"""
                UPDATE {table}
                SET {w_col} = :w, {h_col} = :h
                WHERE {id_col} = :id
                """,
                {"w": w, "h": h, "id": row["id"]},
            )
            return True

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(process_one(client, row) for row in rows)
        )

    for r in results:
        if r:
            updated += 1
        else:
            failed += 1

    remaining_after = await database.fetch_val(
        f"""
        SELECT COUNT(*) FROM {table}
        WHERE {url_col} IS NOT NULL
          AND {url_col} <> ''
          AND ({w_col} IS NULL OR {h_col} IS NULL)
        """
    ) or 0

    return {
        "table": table,
        "processed": len(rows),
        "updated": updated,
        "failed": failed,
        "remaining": remaining_after,
        "done": remaining_after == 0,
    }


@router.get("/backfill-image-dims/status")
async def backfill_status(current_user: dict = Depends(get_current_admin)):
    """How many rows still need dims across all tables."""
    out: dict[str, int] = {}
    for name, cfg in TABLES.items():
        try:
            n = await database.fetch_val(
                f"""
                SELECT COUNT(*) FROM {name}
                WHERE {cfg['url_col']} IS NOT NULL
                  AND {cfg['url_col']} <> ''
                  AND ({cfg['w_col']} IS NULL OR {cfg['h_col']} IS NULL)
                """
            ) or 0
            out[name] = int(n)
        except Exception as e:
            print(f"⚠️  status check failed for {name}: {e}")
            out[name] = -1
    return out