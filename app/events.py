from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field
from databases import Database
import json


class Event(BaseModel):
    event_type: str
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    listing_id: Optional[str] = None
    store_id: Optional[str] = None
    search_query: Optional[str] = None
    user_location: Optional[dict] = None
    listing_location: Optional[dict] = None
    position: Optional[int] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


async def log_event(event: Event, db: Database):
    query = """
    INSERT INTO events (event_type, user_id, session_id, listing_id, store_id,
                        search_query, user_location, listing_location, position, timestamp)
    VALUES (:event_type, :user_id, :session_id, :listing_id, :store_id,
            :search_query, :user_location, :listing_location, :position, :timestamp)
    """

    # Pydantic v2 → model_dump. v1 used .dict(), which is deprecated.
    values = event.model_dump()

    # ✅ Pass the datetime object directly — the column is TIMESTAMP.
    # asyncpg will encode it. Do NOT call .isoformat() here.
    # (The previous code did `values["timestamp"] = event.timestamp.isoformat()`,
    #  which fed a str into a TIMESTAMP column and raised:
    #  TypeError: expected a datetime.date or datetime.datetime instance, got 'str')

    # JSON-encode the dict fields — they're stored as TEXT.
    values["user_location"] = (
        json.dumps(event.user_location) if event.user_location else None
    )
    values["listing_location"] = (
        json.dumps(event.listing_location) if event.listing_location else None
    )

    await db.execute(query, values)
    return {"status": "ok"}


async def get_events(db: Database):
    rows = await db.fetch_all("SELECT * FROM events")
    return [dict(row) for row in rows]