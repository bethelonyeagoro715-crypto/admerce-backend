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
    # Naive UTC to match the TIMESTAMP (without time zone) column.
    # If the column becomes TIMESTAMPTZ, switch back to datetime.now(timezone.utc).
    timestamp: datetime = Field(default_factory=lambda: datetime.utcnow())


async def log_event(event: Event, db: Database):
    query = """
    INSERT INTO events (event_type, user_id, session_id, listing_id, store_id,
                        search_query, user_location, listing_location, position, timestamp)
    VALUES (:event_type, :user_id, :session_id, :listing_id, :store_id,
            :search_query, :user_location, :listing_location, :position, :timestamp)
    """

    values = event.model_dump()

    # Normalise the timestamp: if it's timezone-aware, strip tzinfo so it
    # matches the naive TIMESTAMP column. This is what was raising:
    #   DataError: can't subtract offset-naive and offset-aware datetimes
    ts = values.get("timestamp")
    if isinstance(ts, datetime) and ts.tzinfo is not None:
        values["timestamp"] = ts.replace(tzinfo=None)

    # JSON-encode the dict fields (stored as TEXT columns)
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