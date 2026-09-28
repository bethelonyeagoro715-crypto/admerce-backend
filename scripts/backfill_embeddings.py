"""
Backfill image embeddings for listings that don't have one yet.

Run from the backend repo root:

    set DATABASE_URL=postgresql://...   (or use $env: on PowerShell)
    set HF_TOKEN=hf_...
    python scripts/backfill_embeddings.py

Optional flags:
    --limit 5        Process only 5 rows (smoke test)
    --list-only      Just print which rows need embedding
    --force          Re-embed rows that already have a value
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile

import requests
from databases import Database

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.image_embedder import (  # noqa: E402
    image_to_embedding,
    embedding_to_json,
    is_real_embedding,
)


def download_to_tempfile(url: str) -> str:
    r = requests.get(url, timeout=45)
    r.raise_for_status()
    suffix = ".png" if ".png" in url.lower() else ".jpg"
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(r.content)
    return path


async def run(limit: int | None, list_only: bool, force: bool):
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("❌ DATABASE_URL not set")
        sys.exit(1)
    if not os.getenv("HF_TOKEN"):
        print("⚠️  HF_TOKEN not set — you'll get 3-dim color fallbacks.")
        print("    Set HF_TOKEN before running, or all rows will be skipped.")
        if not force:
            print("    Aborting.")
            sys.exit(1)

    db = Database(db_url)
    await db.connect()
    print("✅ Connected")

    where = "image_url IS NOT NULL"
    if not force:
        where += " AND embedding IS NULL"

    rows = await db.fetch_all(
        f"""
        SELECT listing_id, image_url
        FROM listings
        WHERE {where}
        ORDER BY created_at DESC
        """
    )
    if limit:
        rows = rows[:limit]

    print(f"📦 {len(rows)} listings to process")
    if list_only or not rows:
        for r in rows:
            print(f"  • {r['listing_id']}  {r['image_url']}")
        await db.disconnect()
        return

    ok, failed, skipped = 0, 0, 0
    for i, row in enumerate(rows, 1):
        lid = row["listing_id"]
        url = row["image_url"]
        print(f"  [{i}/{len(rows)}] {lid}", flush=True)

        tmp = None
        try:
            tmp = download_to_tempfile(url)
            emb = image_to_embedding(tmp)
            if not is_real_embedding(emb):
                skipped += 1
                print(f"    ⏭  skipped (fallback {len(emb)}-dim)")
                continue
            await db.execute(
                "UPDATE listings SET embedding = :e WHERE listing_id = :lid",
                {"e": embedding_to_json(emb), "lid": lid},
            )
            ok += 1
            print(f"    ✅ {len(emb)}-dim")
        except requests.HTTPError as e:
            failed += 1
            print(f"    ❌ download {e}")
        except Exception as e:
            failed += 1
            print(f"    ❌ {type(e).__name__}: {e}")
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)

    await db.disconnect()
    print(f"\n📊 Done. ok={ok} failed={failed} skipped={skipped}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--list-only", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    asyncio.run(run(args.limit, args.list_only, args.force))


if __name__ == "__main__":
    main()