from fastapi import APIRouter, HTTPException, Depends, File, UploadFile
from pydantic import BaseModel, Field
from typing import Optional
import logging
import time
from app.db.database import database
from app.utils.security import get_current_user
from app.services.cloudinary_service import upload_image, upload_video
from PIL import Image
import uuid, io
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/services", tags=["Services"])

# In-process rate limiter (per worker)
_rate_buckets: dict[str, list[float]] = {}

def _rate_limit(key: str, max_calls: int, window_sec: int) -> None:
    now = time.time()
    bucket = _rate_buckets.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < window_sec]
    if len(bucket) >= max_calls:
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    bucket.append(now)
    if len(_rate_buckets) > 10_000:
        cutoff = now - 3600
        for k in list(_rate_buckets.keys()):
            _rate_buckets[k] = [t for t in _rate_buckets[k] if t > cutoff]
            if not _rate_buckets[k]:
                del _rate_buckets[k]


# ---------- image dimensions helper ----------
def _image_dimensions(image_bytes: bytes) -> tuple[Optional[int], Optional[int]]:
    try:
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        if w > 0 and h > 0:
            return int(w), int(h)
    except Exception as e:
        print(f"⚠️  Could not read image dimensions: {e}")
    return None, None


# ---------- Models ----------
class CreateServiceRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    category: str = Field(..., min_length=1, max_length=60)
    description: str = Field("", max_length=2000)
    price: float = Field(..., ge=0)
    duration_minutes: int = Field(60, ge=1, le=1440)
    location_lat: float
    location_lng: float

class BookServiceRequest(BaseModel):
    scheduled_for: Optional[str] = Field(None, max_length=64)
    notes: Optional[str] = Field(None, max_length=1000)
    location_lat: Optional[float] = None
    location_lng: Optional[float] = None

class ToggleAvailabilityRequest(BaseModel):
    is_available: bool

class UpdateServiceRequest(BaseModel):
    title: Optional[str] = Field(None, max_length=120)
    category: Optional[str] = Field(None, max_length=60)
    description: Optional[str] = Field(None, max_length=2000)
    price: Optional[float] = Field(None, ge=0)
    duration_minutes: Optional[int] = Field(None, ge=1, le=1440)

class InstantPayRequest(BaseModel):
    service_id: str = Field(..., min_length=1, max_length=128)
    provider_id: str = Field(..., min_length=1, max_length=128)
    reference: str = Field(..., min_length=8, max_length=128)


# ---------- Helpers ----------
def _parse_iso_datetime(value):
    """Parse ISO string → aware UTC datetime. Never returns naive."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _as_float(value, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def _flip_booking_status(
    booking_id: str,
    from_statuses: tuple,
    to_status: str,
) -> bool:
    """
    Atomically flip a booking's status. Returns True iff exactly one row changed.

    `RETURNING 1` is used so the outcome does not depend on driver-specific
    rowcount reporting (databases returns None for UPDATEs without RETURNING
    on asyncpg in some versions).
    """
    placeholders = ", ".join(f":s{i}" for i in range(len(from_statuses)))
    params = {"bid": booking_id, "to_s": to_status}
    for i, s in enumerate(from_statuses):
        params[f"s{i}"] = s
    sql = (
        f"UPDATE service_bookings SET status = :to_s, updated_at = NOW() "
        f"WHERE booking_id = :bid AND status IN ({placeholders}) "
        f"RETURNING 1"
    )
    return (await database.fetch_val(sql, params)) is not None


async def _credit_wallet(user_id: str, amount: float) -> None:
    await database.execute(
        "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
        {"amt": float(amount), "uid": user_id},
    )


async def _record_wallet_txn(
    user_id: str,
    amount: float,
    txn_type: str,
    description: str,
    reference: str,
) -> None:
    """Ledger row. Uses NOW() in SQL — avoids Python datetime → DB tz issues."""
    if amount <= 0 or not user_id:
        return
    await database.execute(
        """
        INSERT INTO wallet_transactions
            (user_id, amount, type, description, reference, status, created_at)
        VALUES (:uid, :amt, :type, :desc, :ref, 'completed', NOW())
        """,
        {
            "uid": user_id,
            "amt": float(amount),
            "type": txn_type,
            "desc": description,
            "ref": reference,
        },
    )


# ============================================================
# STATIC ROUTES
# ============================================================

@router.get("/provider")
async def get_provider_services(current_user: dict = Depends(get_current_user)):
    provider_id = current_user["id"]
    rows = await database.fetch_all(
        "SELECT * FROM services WHERE provider_id = :pid ORDER BY created_at DESC",
        {"pid": provider_id},
    )
    return [dict(row) for row in rows]


@router.get("/bookings/provider")
async def get_provider_bookings(current_user: dict = Depends(get_current_user)):
    provider_id = current_user["id"]
    service_ids = await database.fetch_all(
        "SELECT service_id FROM services WHERE provider_id = :pid",
        {"pid": provider_id},
    )
    service_id_list = [row["service_id"] for row in service_ids]
    if not service_id_list:
        return []

    placeholders = ",".join([f":sid{i}" for i in range(len(service_id_list))])
    params = {f"sid{i}": sid for i, sid in enumerate(service_id_list)}
    query = f"""
        SELECT sb.*, s.title AS service_title,
               s.image_url AS service_image_url,
               s.image_width AS service_image_width,
               s.image_height AS service_image_height,
               COALESCE(
                   NULLIF(CONCAT(u.first_name, ' ', u.last_name), ' '),
                   u.nickname, u.real_name, u.email, 'Customer'
               ) AS user_name,
               u.avatar_url AS user_avatar
        FROM service_bookings sb
        JOIN services s ON sb.service_id = s.service_id
        JOIN users u ON sb.customer_id = u.id
        WHERE sb.service_id IN ({placeholders})
        ORDER BY sb.created_at DESC
    """
    rows = await database.fetch_all(query, params)
    return [dict(row) for row in rows]


@router.get("/providers/stats")
async def get_provider_stats(current_user: dict = Depends(get_current_user)):
    provider_id = current_user["id"]

    today = datetime.now(timezone.utc).date()
    start_dt = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    end_dt = start_dt + timedelta(days=1)

    today_bookings = await database.fetch_val(
        """
        SELECT COUNT(*)
        FROM service_bookings sb
        JOIN services s ON sb.service_id = s.service_id
        WHERE s.provider_id = :pid
          AND sb.created_at >= :start
          AND sb.created_at < :end
        """,
        {"pid": provider_id, "start": start_dt, "end": end_dt},
    )

    total_earnings = 0.0
    try:
        rows = await database.fetch_all(
            """
            SELECT sb.amount
            FROM service_bookings sb
            JOIN services s ON sb.service_id = s.service_id
            WHERE s.provider_id = :pid AND sb.status = 'completed'
            """,
            {"pid": provider_id},
        )
        total_earnings = sum(_as_float(dict(r).get("amount")) for r in rows)
    except Exception as e:
        logger.warning("total_earnings lookup skipped: %s", e)

    rating = 0.0
    try:
        rating = await database.fetch_val(
            """
            SELECT COALESCE(AVG(r.rating), 0)
            FROM reviews r
            JOIN services s ON r.store_id = s.service_id
            WHERE s.provider_id = :pid
            """,
            {"pid": provider_id},
        ) or 0.0
    except Exception as e:
        logger.warning("reviews lookup skipped: %s", e)

    return {
        "today_bookings": today_bookings or 0,
        "total_earnings": _as_float(total_earnings),
        "rating": _as_float(rating),
    }


@router.put("/providers/availability")
async def update_provider_availability(
    req: ToggleAvailabilityRequest,
    current_user: dict = Depends(get_current_user),
):
    provider_id = current_user["id"]
    # Use NOW() in SQL — avoids binding a Python datetime into a column whose
    # type may be TEXT (pre-fix) or TIMESTAMPTZ (post-fix). NOW() works with
    # either.
    await database.execute(
        """
        INSERT INTO provider_availability (user_id, is_available, updated_at)
        VALUES (:uid, :avail, NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            is_available = :avail,
            updated_at = NOW()
        """,
        {"uid": provider_id, "avail": req.is_available},
    )
    return {"user_id": provider_id, "is_available": req.is_available}


# ============================================================
# DIRECT SERVICE PAYMENT
# ============================================================
@router.post("/instant-pay")
async def instant_pay_service(
    req: InstantPayRequest,
    current_user: dict = Depends(get_current_user),
):
    customer_id = current_user["id"]
    _rate_limit(f"instant-pay:{customer_id}", max_calls=30, window_sec=60)

    service_row = await database.fetch_one(
        "SELECT provider_id, price, title FROM services "
        "WHERE service_id = :sid AND is_active = TRUE",
        {"sid": req.service_id},
    )
    if not service_row:
        raise HTTPException(status_code=404, detail="Service not found or inactive")

    service = dict(service_row)
    if service["provider_id"] != req.provider_id:
        raise HTTPException(status_code=400, detail="Provider does not match this service")
    if customer_id == service["provider_id"]:
        raise HTTPException(status_code=400, detail="You cannot pay for your own service")

    price = _as_float(service["price"])
    if price <= 0:
        raise HTTPException(status_code=400, detail="Service price is invalid")

    try:
        async with database.transaction():
            # Idempotency: is this reference already used by this user?
            existing = await database.fetch_one(
                "SELECT 1 AS x FROM wallet_transactions "
                "WHERE user_id = :uid AND reference = :ref",
                {"uid": customer_id, "ref": req.reference},
            )
            if existing:
                raise HTTPException(
                    status_code=400,
                    detail="This payment has already been processed",
                )

            # Atomic wallet debit — WHERE guard blocks overdraft.
            debited = await database.fetch_val(
                """
                UPDATE wallets
                   SET balance = balance - :amt
                 WHERE user_id = :uid AND balance >= :amt
                RETURNING balance
                """,
                {"amt": price, "uid": customer_id},
            )
            if debited is None:
                raise HTTPException(status_code=400, detail="Insufficient balance")

            await _credit_wallet(service["provider_id"], price)

            await _record_wallet_txn(
                user_id=customer_id,
                amount=price,
                txn_type="debit",
                description=f"Service payment: {service.get('title') or req.service_id}",
                reference=req.reference,
            )
            await _record_wallet_txn(
                user_id=service["provider_id"],
                amount=price,
                txn_type="credit",
                description=f"Service payment received: {service.get('title') or req.service_id}",
                reference=req.reference,
            )
    except HTTPException:
        raise
    except Exception:
        logger.exception("instant_pay failed user=%s ref=%s", customer_id, req.reference)
        raise HTTPException(status_code=500, detail="Payment failed. Please try again.")

    return {
        "message": "Payment successful",
        "transaction_id": req.reference,
        "amount": price,
        "service_id": req.service_id,
        "provider_id": service["provider_id"],
    }


# ============================================================
# EDIT — PATCH /services/{id}   (owner only, partial)
# ============================================================
@router.patch("/{service_id}")
async def update_service(
    service_id: str,
    req: UpdateServiceRequest,
    current_user: dict = Depends(get_current_user),
):
    provider_id = current_user["id"]

    updates = []
    params = {"sid": service_id, "pid": provider_id}

    if req.title is not None:
        t = req.title.strip()
        if not t:
            raise HTTPException(status_code=400, detail="Title cannot be empty")
        if len(t) > 120:
            raise HTTPException(status_code=400, detail="Title too long (max 120 characters)")
        updates.append("title = :title")
        params["title"] = t

    if req.category is not None:
        updates.append("category = :category")
        params["category"] = req.category.strip()

    if req.description is not None:
        updates.append("description = :description")
        params["description"] = req.description.strip()

    if req.price is not None:
        if req.price < 0:
            raise HTTPException(status_code=400, detail="Price must be 0 or greater")
        updates.append("price = :price")
        params["price"] = float(req.price)

    if req.duration_minutes is not None:
        if req.duration_minutes <= 0:
            raise HTTPException(status_code=400, detail="Duration must be greater than 0")
        updates.append("duration_minutes = :duration_minutes")
        params["duration_minutes"] = int(req.duration_minutes)

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    query = f"""
        UPDATE services
        SET {', '.join(updates)}
        WHERE service_id = :sid AND provider_id = :pid
        RETURNING 1
    """
    got = await database.fetch_val(query, params)
    if got is None:
        raise HTTPException(status_code=403, detail="Not authorized or service not found")

    row = await database.fetch_one(
        "SELECT * FROM services WHERE service_id = :sid",
        {"sid": service_id},
    )
    return dict(row) if row else {"service_id": service_id, "message": "Updated"}


@router.delete("/{service_id}/video")
async def delete_service_video(
    service_id: str,
    current_user: dict = Depends(get_current_user),
):
    provider_id = current_user["id"]
    got = await database.fetch_val(
        """
        UPDATE services SET video_url = NULL
        WHERE service_id = :sid AND provider_id = :pid
        RETURNING 1
        """,
        {"sid": service_id, "pid": provider_id},
    )
    if got is None:
        raise HTTPException(status_code=403, detail="Not authorized or service not found")
    return {"service_id": service_id, "video_url": None, "message": "Video removed"}


# ============================================================
# TOGGLE / DELETE — existing
# ============================================================
@router.post("/{service_id}/toggle")
async def toggle_service_active(
    service_id: str,
    current_user: dict = Depends(get_current_user),
):
    service = await database.fetch_one(
        "SELECT provider_id, is_active FROM services WHERE service_id = :sid",
        {"sid": service_id},
    )
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    if service["provider_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    new_state = not service["is_active"]
    await database.execute(
        "UPDATE services SET is_active = :state WHERE service_id = :sid",
        {"state": new_state, "sid": service_id},
    )
    return {"service_id": service_id, "is_active": new_state}


@router.delete("/{service_id}")
async def delete_service(service_id: str, current_user: dict = Depends(get_current_user)):
    service = await database.fetch_one(
        "SELECT provider_id FROM services WHERE service_id = :sid",
        {"sid": service_id},
    )
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    if service["provider_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    await database.execute(
        "DELETE FROM services WHERE service_id = :sid", {"sid": service_id}
    )
    return {"message": "Service permanently deleted"}


# ============================================================
# BOOKINGS
# ============================================================
@router.get("/bookings")
async def get_bookings(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    rows = await database.fetch_all(
        """
        SELECT sb.*,
               s.title AS service_title,
               s.image_url AS service_image_url,
               s.image_width AS service_image_width,
               s.image_height AS service_image_height,
               s.duration_minutes AS service_duration,
               COALESCE(
                   NULLIF(CONCAT(u_c.first_name, ' ', u_c.last_name), ' '),
                   u_c.nickname, u_c.real_name, u_c.email, 'Customer'
               ) AS customer_name,
               u_c.avatar_url AS customer_avatar,
               COALESCE(
                   NULLIF(CONCAT(u_p.first_name, ' ', u_p.last_name), ' '),
                   u_p.nickname, u_p.real_name, u_p.business_name, u_p.email,
                   'Provider'
               ) AS provider_name,
               u_p.business_image_url AS provider_image_url,
               u_p.business_image_width AS provider_image_width,
               u_p.business_image_height AS provider_image_height,
               u_p.avatar_url AS provider_avatar
        FROM service_bookings sb
        LEFT JOIN services s ON sb.service_id = s.service_id
        LEFT JOIN users u_c ON sb.customer_id = u_c.id
        LEFT JOIN users u_p ON sb.provider_id = u_p.id
        WHERE sb.customer_id = :uid OR sb.provider_id = :uid2
        ORDER BY sb.created_at DESC
        """,
        {"uid": user_id, "uid2": user_id},
    )
    return [dict(row) for row in rows]


@router.get("/bookings/{booking_id}")
async def get_booking(booking_id: str, current_user: dict = Depends(get_current_user)):
    row = await database.fetch_one(
        """
        SELECT sb.*,
               s.title AS service_title,
               s.image_url AS service_image_url,
               s.image_width AS service_image_width,
               s.image_height AS service_image_height,
               s.duration_minutes AS service_duration,
               COALESCE(
                   NULLIF(CONCAT(u_c.first_name, ' ', u_c.last_name), ' '),
                   u_c.nickname, u_c.real_name, u_c.email, 'Customer'
               ) AS customer_name,
               u_c.avatar_url AS customer_avatar,
               COALESCE(
                   NULLIF(CONCAT(u_p.first_name, ' ', u_p.last_name), ' '),
                   u_p.nickname, u_p.real_name, u_p.business_name, u_p.email,
                   'Provider'
               ) AS provider_name,
               u_p.business_image_url AS provider_image_url,
               u_p.business_image_width AS provider_image_width,
               u_p.business_image_height AS provider_image_height,
               u_p.avatar_url AS provider_avatar
        FROM service_bookings sb
        LEFT JOIN services s ON sb.service_id = s.service_id
        LEFT JOIN users u_c ON sb.customer_id = u_c.id
        LEFT JOIN users u_p ON sb.provider_id = u_p.id
        WHERE sb.booking_id = :bid
        """,
        {"bid": booking_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Booking not found")

    booking = dict(row)
    if (
        booking.get("customer_id") != current_user["id"]
        and booking.get("provider_id") != current_user["id"]
    ):
        raise HTTPException(status_code=403, detail="Access denied")
    return booking


@router.post("/bookings/{booking_id}/accept")
async def accept_booking(
    booking_id: str,
    current_user: dict = Depends(get_current_user),
):
    row = await database.fetch_one(
        "SELECT * FROM service_bookings WHERE booking_id = :bid",
        {"bid": booking_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Booking not found")

    booking = dict(row)
    if booking.get("provider_id") != current_user["id"]:
        raise HTTPException(
            status_code=403,
            detail="Only the service provider can accept this booking",
        )

    if (booking.get("status") or "").lower() != "locked":
        raise HTTPException(
            status_code=400,
            detail="Only pending bookings can be accepted.",
        )

    flipped = await _flip_booking_status(booking_id, ("locked",), "accepted")
    if not flipped:
        return {
            "booking_id": booking_id,
            "status": "accepted",
            "message": "Booking was already accepted.",
        }

    return {
        "booking_id": booking_id,
        "status": "accepted",
        "message": "Booking accepted. The customer has been notified.",
    }


@router.post("/bookings/{booking_id}/decline")
async def decline_booking(
    booking_id: str,
    current_user: dict = Depends(get_current_user),
):
    row = await database.fetch_one(
        "SELECT * FROM service_bookings WHERE booking_id = :bid",
        {"bid": booking_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Booking not found")

    booking = dict(row)
    if booking.get("provider_id") != current_user["id"]:
        raise HTTPException(
            status_code=403,
            detail="Only the service provider can decline this booking",
        )

    if (booking.get("status") or "").lower() != "locked":
        raise HTTPException(
            status_code=400,
            detail="Only pending bookings can be declined.",
        )

    amount = _as_float(booking.get("amount"))
    customer_id = booking.get("customer_id")
    refunded = 0.0

    try:
        async with database.transaction():
            flipped = await _flip_booking_status(booking_id, ("locked",), "declined")
            if not flipped:
                return {
                    "booking_id": booking_id,
                    "status": "declined",
                    "refunded": 0,
                    "message": "Booking was already processed.",
                }

            if amount > 0 and customer_id:
                await _credit_wallet(customer_id, amount)
                refunded = amount
                await _record_wallet_txn(
                    user_id=customer_id,
                    amount=amount,
                    txn_type="credit",
                    description=f"Refund: booking {booking_id} declined by provider",
                    reference=f"decline:{booking_id}",
                )
    except HTTPException:
        raise
    except Exception:
        logger.exception("decline_booking failed %s", booking_id)
        raise HTTPException(status_code=500, detail="Could not decline booking.")

    return {
        "booking_id": booking_id,
        "status": "declined",
        "refunded": refunded,
        "message": (
            f"Booking declined. ₦{refunded:,.0f} returned to the customer."
            if refunded > 0
            else "Booking declined."
        ),
    }


@router.post("/bookings/{booking_id}/confirm")
async def confirm_booking(
    booking_id: str,
    current_user: dict = Depends(get_current_user),
):
    row = await database.fetch_one(
        "SELECT * FROM service_bookings WHERE booking_id = :bid",
        {"bid": booking_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Booking not found")

    booking = dict(row)
    if booking.get("provider_id") != current_user["id"]:
        raise HTTPException(
            status_code=403,
            detail="Only the service provider can confirm completion",
        )

    if (booking.get("status") or "").lower() not in ("locked", "accepted"):
        raise HTTPException(status_code=400, detail="Booking already processed")

    amount = _as_float(booking.get("amount"))
    provider_id = booking["provider_id"]

    try:
        async with database.transaction():
            flipped = await _flip_booking_status(
                booking_id, ("locked", "accepted"), "completed"
            )
            if not flipped:
                return {
                    "booking_id": booking_id,
                    "status": "completed",
                    "message": "Booking was already processed.",
                }

            if amount > 0:
                await _credit_wallet(provider_id, amount)
                await _record_wallet_txn(
                    user_id=provider_id,
                    amount=amount,
                    txn_type="credit",
                    description=f"Payment received: booking {booking_id} confirmed by provider",
                    reference=f"confirm:{booking_id}",
                )
    except HTTPException:
        raise
    except Exception:
        logger.exception("confirm_booking failed %s", booking_id)
        raise HTTPException(status_code=500, detail="Could not confirm booking.")

    return {
        "booking_id": booking_id,
        "status": "completed",
        "message": "Job marked complete, funds released.",
    }


@router.post("/bookings/{booking_id}/complete")
async def complete_booking(
    booking_id: str,
    current_user: dict = Depends(get_current_user),
):
    row = await database.fetch_one(
        "SELECT * FROM service_bookings WHERE booking_id = :bid",
        {"bid": booking_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Booking not found")

    booking = dict(row)
    if booking.get("customer_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only the client can release funds")

    if (booking.get("status") or "").lower() not in ("locked", "accepted"):
        raise HTTPException(status_code=400, detail="Booking already processed")

    amount = _as_float(booking.get("amount"))
    provider_id = booking["provider_id"]

    try:
        async with database.transaction():
            flipped = await _flip_booking_status(
                booking_id, ("locked", "accepted"), "completed"
            )
            if not flipped:
                return {
                    "booking_id": booking_id,
                    "status": "completed",
                    "message": "Booking was already processed.",
                }

            if amount > 0:
                await _credit_wallet(provider_id, amount)
                await _record_wallet_txn(
                    user_id=provider_id,
                    amount=amount,
                    txn_type="credit",
                    description=f"Payment received: booking {booking_id} released by customer",
                    reference=f"complete:{booking_id}",
                )
    except HTTPException:
        raise
    except Exception:
        logger.exception("complete_booking failed %s", booking_id)
        raise HTTPException(status_code=500, detail="Could not complete booking.")

    return {
        "booking_id": booking_id,
        "status": "completed",
        "message": "Funds released to provider.",
    }


@router.post("/bookings/{booking_id}/cancel")
async def cancel_booking(
    booking_id: str,
    current_user: dict = Depends(get_current_user),
):
    row = await database.fetch_one(
        "SELECT * FROM service_bookings WHERE booking_id = :bid",
        {"bid": booking_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Booking not found")

    booking = dict(row)
    user_id = current_user["id"]

    if user_id not in (booking.get("customer_id"), booking.get("provider_id")):
        raise HTTPException(status_code=403, detail="Access denied")

    if (booking.get("status") or "").lower() not in ("locked", "accepted"):
        raise HTTPException(
            status_code=400,
            detail="Only pending or accepted bookings can be cancelled.",
        )

    amount = _as_float(booking.get("amount"))
    customer_id = booking.get("customer_id")
    cancelled_by = "customer" if user_id == customer_id else "provider"
    refunded = 0.0

    try:
        async with database.transaction():
            flipped = await _flip_booking_status(
                booking_id, ("locked", "accepted"), "cancelled"
            )
            if not flipped:
                return {
                    "booking_id": booking_id,
                    "status": "cancelled",
                    "refunded": 0,
                    "cancelled_by": cancelled_by,
                    "message": "Booking was already cancelled.",
                }

            if amount > 0 and customer_id:
                await _credit_wallet(customer_id, amount)
                refunded = amount
                await _record_wallet_txn(
                    user_id=customer_id,
                    amount=amount,
                    txn_type="credit",
                    description=f"Refund: booking {booking_id} cancelled by {cancelled_by}",
                    reference=f"cancel:{booking_id}",
                )
    except HTTPException:
        raise
    except Exception:
        logger.exception("cancel_booking failed %s", booking_id)
        raise HTTPException(status_code=500, detail="Could not cancel booking.")

    return {
        "booking_id": booking_id,
        "status": "cancelled",
        "refunded": refunded,
        "cancelled_by": cancelled_by,
        "message": (
            "Booking cancelled. "
            + (f"₦{refunded:,.0f} returned to the customer's wallet."
               if refunded > 0 else "")
        ).strip(),
    }


# ============================================================
# DYNAMIC — create, list, upload, get, book
# ============================================================

@router.post("/")
async def create_service(
    req: CreateServiceRequest, current_user: dict = Depends(get_current_user)
):
    provider_id = current_user["id"]
    service_id = uuid.uuid4().hex[:8]
    query = """
    INSERT INTO services (
        service_id, provider_id, title, category, description, price,
        duration_minutes, lat, lng, is_active, created_at
    )
    VALUES (:sid, :pid, :title, :cat, :desc, :price, :dur, :lat, :lng, TRUE, NOW())
    """
    await database.execute(
        query,
        {
            "sid": service_id,
            "pid": provider_id,
            "title": req.title,
            "cat": req.category,
            "desc": req.description,
            "price": req.price,
            "dur": req.duration_minutes,
            "lat": req.location_lat,
            "lng": req.location_lng,
        },
    )
    return {"service_id": service_id, "message": "Service created"}


@router.get("/")
async def list_services():
    rows = await database.fetch_all(
        """
        SELECT s.*, u.business_name, u.business_image_url,
               u.business_image_width, u.business_image_height
        FROM services s
        JOIN users u ON s.provider_id = u.id
        WHERE s.is_active = TRUE
        ORDER BY s.created_at DESC
        """
    )
    return [dict(row) for row in rows]


@router.post("/{service_id}/image")
async def upload_service_image(
    service_id: str,
    image: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    service = await database.fetch_one(
        "SELECT provider_id FROM services WHERE service_id = :sid",
        {"sid": service_id},
    )
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    if service["provider_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    image_bytes = await image.read()
    try:
        image_url = upload_image(image_bytes, folder="service_images")
    except Exception as e:
        logger.warning("Cloudinary upload failed: %r", e)
        raise HTTPException(status_code=500, detail="Image upload failed")

    image_width, image_height = _image_dimensions(image_bytes)

    await database.execute(
        "UPDATE services SET image_url = :url, "
        "image_width = :w, image_height = :h "
        "WHERE service_id = :sid",
        {"url": image_url, "w": image_width, "h": image_height, "sid": service_id},
    )
    return {
        "image_url": image_url,
        "image_width": image_width,
        "image_height": image_height,
    }


@router.post("/{service_id}/video")
async def upload_service_video(
    service_id: str,
    video: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    service = await database.fetch_one(
        "SELECT provider_id FROM services WHERE service_id = :sid",
        {"sid": service_id},
    )
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    if service["provider_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    video_bytes = await video.read()
    try:
        video_url = upload_video(video_bytes, folder="service_videos")
    except Exception as e:
        logger.warning("Cloudinary video upload failed: %r", e)
        raise HTTPException(status_code=500, detail="Video upload failed")

    await database.execute(
        "UPDATE services SET video_url = :url WHERE service_id = :sid",
        {"url": video_url, "sid": service_id},
    )
    return {"video_url": video_url}


@router.get("/{service_id}")
async def get_service(service_id: str):
    row = await database.fetch_one(
        """
        SELECT s.*, u.business_name, u.business_image_url,
               u.business_image_width, u.business_image_height
        FROM services s
        JOIN users u ON s.provider_id = u.id
        WHERE s.service_id = :sid AND s.is_active = TRUE
        """,
        {"sid": service_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Service not found")
    return dict(row)


@router.post("/{service_id}/book")
async def book_service(
    service_id: str,
    req: BookServiceRequest,
    current_user: dict = Depends(get_current_user),
):
    customer_id = current_user["id"]
    _rate_limit(f"book:{customer_id}", max_calls=20, window_sec=60)

    service = await database.fetch_one(
        "SELECT * FROM services WHERE service_id = :sid AND is_active = TRUE",
        {"sid": service_id},
    )
    if not service:
        raise HTTPException(status_code=404, detail="Service not found or inactive")

    if customer_id == service["provider_id"]:
        raise HTTPException(status_code=400, detail="You cannot book your own service")

    provider_available = await database.fetch_val(
        "SELECT is_available FROM provider_availability WHERE user_id = :pid",
        {"pid": service["provider_id"]},
    )
    if provider_available is False:
        raise HTTPException(status_code=400, detail="Provider is currently unavailable")

    service_price = _as_float(service["price"])
    if service_price <= 0:
        raise HTTPException(status_code=400, detail="Service price is invalid")

    booking_id = uuid.uuid4().hex[:8]
    scheduled_dt = _parse_iso_datetime(req.scheduled_for)

    try:
        async with database.transaction():
            # Atomic wallet debit.
            debited = await database.fetch_val(
                """
                UPDATE wallets
                   SET balance = balance - :amt
                 WHERE user_id = :uid AND balance >= :amt
                RETURNING balance
                """,
                {"amt": service_price, "uid": customer_id},
            )
            if debited is None:
                raise HTTPException(status_code=400, detail="Insufficient balance")

            await database.execute(
                """
                INSERT INTO service_bookings (
                    booking_id, service_id, customer_id, provider_id, amount,
                    status, scheduled_for, location_lat, location_lng, notes, created_at
                )
                VALUES (
                    :bid, :sid, :cid, :pid, :amt,
                    'locked', :sch, :lat, :lng, :notes, NOW()
                )
                """,
                {
                    "bid": booking_id,
                    "sid": service_id,
                    "cid": customer_id,
                    "pid": service["provider_id"],
                    "amt": service_price,
                    "sch": scheduled_dt,
                    "lat": req.location_lat,
                    "lng": req.location_lng,
                    "notes": req.notes,
                },
            )

            await _record_wallet_txn(
                user_id=customer_id,
                amount=service_price,
                txn_type="debit",
                description=f"Escrow hold: booking {booking_id}",
                reference=f"book:{booking_id}",
            )
    except HTTPException:
        raise
    except Exception:
        logger.exception("book_service failed service=%s user=%s", service_id, customer_id)
        raise HTTPException(status_code=500, detail="Could not create booking.")

    return {
        "booking_id": booking_id,
        "service_id": service_id,
        "status": "locked",
        "message": "Service booked. Funds held in escrow.",
    }


@router.get("/provider/{provider_id}")
async def get_provider_services_by_user(provider_id: str):
    rows = await database.fetch_all(
        """
        SELECT s.*,
               u.business_name,
               u.nickname AS username,
               u.real_name,
               u.business_image_url,
               u.business_image_width,
               u.business_image_height,
               u.avatar_url
        FROM services s
        JOIN users u ON s.provider_id = u.id
        WHERE s.provider_id = :pid AND s.is_active = TRUE
        ORDER BY s.created_at DESC
        """,
        {"pid": provider_id},
    )
    return [dict(row) for row in rows]