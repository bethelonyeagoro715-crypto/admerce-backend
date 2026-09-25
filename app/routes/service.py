from fastapi import APIRouter, HTTPException, Depends, File, UploadFile
from pydantic import BaseModel
from typing import Optional
from app.db.database import database
from app.utils.security import get_current_user
from app.services.cloudinary_service import upload_image, upload_video
import uuid
from datetime import datetime, timedelta

router = APIRouter(prefix="/services", tags=["Services"])

# ---------- Models ----------
class CreateServiceRequest(BaseModel):
    title: str
    category: str
    description: str = ""
    price: float
    duration_minutes: int = 60
    location_lat: float
    location_lng: float

class BookServiceRequest(BaseModel):
    scheduled_for: Optional[str] = None
    notes: Optional[str] = None
    location_lat: Optional[float] = None
    location_lng: Optional[float] = None

class ToggleAvailabilityRequest(BaseModel):
    is_available: bool

class UpdateServiceRequest(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    duration_minutes: Optional[int] = None

class InstantPayRequest(BaseModel):
    service_id: str
    provider_id: str
    reference: str

# ---------- Helpers ----------
def _parse_iso_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _as_float(value, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _update_won(result) -> bool:
    """
    asyncpg's database.execute() returns a status string like 'UPDATE 1'
    or 'UPDATE 0' for UPDATE statements. This returns True iff the row
    was actually touched by the guarded UPDATE — i.e. this caller won
    any race for the row.
    """
    if isinstance(result, str):
        return not result.strip().endswith("0")
    # Anything else (RETURNING value, None) — treat as success.
    return True


async def _record_wallet_txn(
    user_id: str,
    amount: float,
    txn_type: str,
    description: str,
    reference: str,
    now: Optional[datetime] = None,
) -> None:
    """
    Write a wallet_transactions audit row.

    Fire-and-forget: does not participate in any surrounding DB transaction.
    Caller must have already committed the wallet balance UPDATE. Silently
    no-ops when amount <= 0 so callers can call it unconditionally.
    """
    if amount <= 0 or not user_id:
        return
    await database.execute(
        """
        INSERT INTO wallet_transactions
            (user_id, amount, type, description, reference, status, created_at)
        VALUES (:uid, :amt, :type, :desc, :ref, 'completed', :now)
        """,
        {
            "uid": user_id,
            "amt": float(amount),
            "type": txn_type,
            "desc": description,
            "ref": reference,
            "now": now or datetime.utcnow(),
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
               COALESCE(
                   NULLIF(CONCAT(u.first_name, ' ', u.last_name), ' '),
                   u.nickname,
                   u.real_name,
                   u.email,
                   'Customer'
               ) AS user_name
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

    today = datetime.utcnow().date()
    start_dt = datetime(today.year, today.month, today.day)
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
            WHERE s.provider_id = :pid
              AND sb.status = 'completed'
            """,
            {"pid": provider_id},
        )
        total_earnings = sum(_as_float(dict(r).get("amount")) for r in rows)
    except Exception as e:
        print(f"⚠️  total_earnings lookup skipped: {e}")

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
        print(f"⚠️  reviews lookup skipped: {e}")

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
    now = datetime.utcnow()
    await database.execute(
        """
        INSERT INTO provider_availability (user_id, is_available, updated_at)
        VALUES (:uid, :avail, :now)
        ON CONFLICT (user_id) DO UPDATE SET
            is_available = :avail,
            updated_at = :now
        """,
        {"uid": provider_id, "avail": req.is_available, "now": now},
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

    if not req.reference or len(req.reference) < 8:
        raise HTTPException(status_code=400, detail="Invalid payment reference")

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

    existing = await database.fetch_one(
        "SELECT id FROM wallet_transactions "
        "WHERE user_id = :uid AND reference = :ref",
        {"uid": customer_id, "ref": req.reference},
    )
    if existing:
        raise HTTPException(
            status_code=400,
            detail="This payment has already been processed",
        )

    wallet = await database.fetch_one(
        "SELECT balance FROM wallets WHERE user_id = :uid",
        {"uid": customer_id},
    )
    if not wallet or _as_float(wallet["balance"]) < price:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    await database.execute(
        "UPDATE wallets SET balance = balance - :amt WHERE user_id = :uid",
        {"amt": price, "uid": customer_id},
    )
    await database.execute(
        "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
        {"amt": price, "uid": service["provider_id"]},
    )

    await database.execute(
        """
        INSERT INTO wallet_transactions
            (user_id, amount, type, description, reference, status, created_at)
        VALUES
            (:uid, :amt, 'debit', :desc, :ref, 'completed', NOW())
        """,
        {
            "uid": customer_id,
            "amt": price,
            "desc": f"Service payment: {service.get('title') or req.service_id}",
            "ref": req.reference,
        },
    )
    await database.execute(
        """
        INSERT INTO wallet_transactions
            (user_id, amount, type, description, reference, status, created_at)
        VALUES
            (:uid, :amt, 'credit', :desc, :ref, 'completed', NOW())
        """,
        {
            "uid": service["provider_id"],
            "amt": price,
            "desc": f"Service payment received: {service.get('title') or req.service_id}",
            "ref": req.reference,
        },
    )

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
    """
    result = await database.execute(query, params)

    if not _update_won(result):
        raise HTTPException(status_code=403, detail="Not authorized or service not found")

    row = await database.fetch_one(
        "SELECT * FROM services WHERE service_id = :sid",
        {"sid": service_id},
    )
    return dict(row) if row else {"service_id": service_id, "message": "Updated"}


# ============================================================
# DELETE VIDEO — removes only the video_url (owner only)
# ============================================================
@router.delete("/{service_id}/video")
async def delete_service_video(
    service_id: str,
    current_user: dict = Depends(get_current_user),
):
    provider_id = current_user["id"]
    result = await database.execute(
        """
        UPDATE services
        SET video_url = NULL
        WHERE service_id = :sid AND provider_id = :pid
        """,
        {"sid": service_id, "pid": provider_id},
    )
    if not _update_won(result):
        raise HTTPException(status_code=403, detail="Not authorized or service not found")
    return {"service_id": service_id, "video_url": None, "message": "Video removed"}


# ============================================================
# TOGGLE / DELETE / BOOK — existing
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
               COALESCE(
                   NULLIF(CONCAT(u_c.first_name, ' ', u_c.last_name), ' '),
                   u_c.nickname, u_c.real_name, u_c.email, 'Customer'
               ) AS customer_name,
               COALESCE(
                   NULLIF(CONCAT(u_p.first_name, ' ', u_p.last_name), ' '),
                   u_p.nickname, u_p.real_name, u_p.business_name, u_p.email,
                   'Provider'
               ) AS provider_name
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
               COALESCE(
                   NULLIF(CONCAT(u_c.first_name, ' ', u_c.last_name), ' '),
                   u_c.nickname, u_c.real_name, u_c.email, 'Customer'
               ) AS customer_name,
               COALESCE(
                   NULLIF(CONCAT(u_p.first_name, ' ', u_p.last_name), ' '),
                   u_p.nickname, u_p.real_name, u_p.business_name, u_p.email,
                   'Provider'
               ) AS provider_name
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

    current_status = (booking.get("status") or "").lower()
    if current_status != "locked":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot accept a booking with status '{current_status}'. "
                "Only pending bookings can be accepted."
            ),
        )

    now = datetime.utcnow()
    # Atomic guard — if another request already accepted it, this updates 0 rows.
    result = await database.execute(
        """
        UPDATE service_bookings
        SET status = 'accepted', updated_at = :now
        WHERE booking_id = :bid AND status = 'locked'
        """,
        {"now": now, "bid": booking_id},
    )
    if not _update_won(result):
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

    current_status = (booking.get("status") or "").lower()
    if current_status != "locked":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot decline a booking with status '{current_status}'. "
                "Only pending bookings can be declined."
            ),
        )

    amount = _as_float(booking.get("amount"))
    customer_id = booking.get("customer_id")
    now = datetime.utcnow()

    # ✅ Atomic flip FIRST. Refund only if this caller actually won the race.
    result = await database.execute(
        """
        UPDATE service_bookings
        SET status = 'declined', updated_at = :now
        WHERE booking_id = :bid AND status = 'locked'
        """,
        {"now": now, "bid": booking_id},
    )
    if not _update_won(result):
        # Someone else already processed this booking — no double refund.
        return {
            "booking_id": booking_id,
            "status": "declined",
            "refunded": 0,
            "message": "Booking was already processed.",
        }

    refunded = 0.0
    if amount > 0 and customer_id:
        await database.execute(
            "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
            {"amt": amount, "uid": customer_id},
        )
        refunded = amount
        await _record_wallet_txn(
            user_id=customer_id,
            amount=amount,
            txn_type="credit",
            description=f"Refund: booking {booking_id} declined by provider",
            reference=f"decline:{booking_id}",
            now=now,
        )

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

    current_status = (booking.get("status") or "").lower()
    if current_status not in ("locked", "accepted"):
        raise HTTPException(status_code=400, detail="Booking already processed")

    amount = _as_float(booking.get("amount"))
    provider_id = booking["provider_id"]
    now = datetime.utcnow()

    # ✅ Atomic flip FIRST. Credit only if we won the race.
    result = await database.execute(
        "UPDATE service_bookings SET status = 'completed', updated_at = :now "
        "WHERE booking_id = :bid AND status IN ('locked', 'accepted')",
        {"now": now, "bid": booking_id},
    )
    if not _update_won(result):
        return {
            "booking_id": booking_id,
            "status": "completed",
            "message": "Booking was already processed.",
        }

    if amount > 0:
        await database.execute(
            "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
            {"amt": amount, "uid": provider_id},
        )
        await _record_wallet_txn(
            user_id=provider_id,
            amount=amount,
            txn_type="credit",
            description=f"Payment received: booking {booking_id} confirmed by provider",
            reference=f"confirm:{booking_id}",
            now=now,
        )

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

    current_status = (booking.get("status") or "").lower()
    if current_status not in ("locked", "accepted"):
        raise HTTPException(status_code=400, detail="Booking already processed")

    amount = _as_float(booking.get("amount"))
    provider_id = booking["provider_id"]
    now = datetime.utcnow()

    # ✅ Atomic flip FIRST. Credit only if we won the race.
    result = await database.execute(
        "UPDATE service_bookings SET status = 'completed', updated_at = :now "
        "WHERE booking_id = :bid AND status IN ('locked', 'accepted')",
        {"now": now, "bid": booking_id},
    )
    if not _update_won(result):
        return {
            "booking_id": booking_id,
            "status": "completed",
            "message": "Booking was already processed.",
        }

    if amount > 0:
        await database.execute(
            "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
            {"amt": amount, "uid": provider_id},
        )
        await _record_wallet_txn(
            user_id=provider_id,
            amount=amount,
            txn_type="credit",
            description=f"Payment received: booking {booking_id} released by customer",
            reference=f"complete:{booking_id}",
            now=now,
        )

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

    current_status = (booking.get("status") or "").lower()
    if current_status not in ("locked", "accepted"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot cancel a booking with status '{current_status}'. "
                "Only pending or accepted bookings can be cancelled. "
                "If the job has already been completed, please contact support."
            ),
        )

    amount = _as_float(booking.get("amount"))
    customer_id = booking.get("customer_id")
    cancelled_by = "customer" if user_id == customer_id else "provider"
    now = datetime.utcnow()

    # ✅ Atomic flip FIRST. Refund only if this caller won the race.
    result = await database.execute(
        """
        UPDATE service_bookings
        SET status = 'cancelled', updated_at = :now
        WHERE booking_id = :bid AND status IN ('locked', 'accepted')
        """,
        {"now": now, "bid": booking_id},
    )
    if not _update_won(result):
        # Idempotent: another caller already cancelled this booking.
        return {
            "booking_id": booking_id,
            "status": "cancelled",
            "refunded": 0,
            "cancelled_by": cancelled_by,
            "message": "Booking was already cancelled.",
        }

    refunded = 0.0
    if amount > 0 and customer_id:
        await database.execute(
            "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
            {"amt": amount, "uid": customer_id},
        )
        refunded = amount
        await _record_wallet_txn(
            user_id=customer_id,
            amount=amount,
            txn_type="credit",
            description=f"Refund: booking {booking_id} cancelled by {cancelled_by}",
            reference=f"cancel:{booking_id}",
            now=now,
        )

    return {
        "booking_id": booking_id,
        "status": "cancelled",
        "refunded": refunded,
        "cancelled_by": cancelled_by,
        "message": (
            "Booking cancelled. "
            + (
                f"₦{refunded:,.0f} returned to the customer's wallet."
                if refunded > 0
                else ""
            )
        ).strip(),
    }


# ============================================================
# DYNAMIC — create, list, upload, get, book, provider-services
# ============================================================

@router.post("/")
async def create_service(
    req: CreateServiceRequest, current_user: dict = Depends(get_current_user)
):
    provider_id = current_user["id"]
    service_id = uuid.uuid4().hex[:8]
    now = datetime.utcnow()
    query = """
    INSERT INTO services (
        service_id, provider_id, title, category, description, price,
        duration_minutes, lat, lng, is_active, created_at
    )
    VALUES (:sid, :pid, :title, :cat, :desc, :price, :dur, :lat, :lng, TRUE, :created)
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
            "created": now,
        },
    )
    return {"service_id": service_id, "message": "Service created"}

@router.get("/")
async def list_services():
    rows = await database.fetch_all(
        """
        SELECT s.*, u.business_name, u.business_image_url
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
    print(f"📤 Uploading service image: {len(image_bytes)} bytes", flush=True)

    try:
        image_url = upload_image(image_bytes, folder="service_images")
        print(f"✅ Cloudinary returned: {image_url}", flush=True)
    except Exception as e:
        print(f"❌ Cloudinary upload failed: {e!r}", flush=True)
        raise HTTPException(status_code=500, detail="Image upload failed")

    await database.execute(
        "UPDATE services SET image_url = :url WHERE service_id = :sid",
        {"url": image_url, "sid": service_id},
    )
    return {"image_url": image_url}

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
    print(f"📤 Uploading service video: {len(video_bytes)} bytes", flush=True)

    try:
        video_url = upload_video(video_bytes, folder="service_videos")
        print(f"✅ Cloudinary returned: {video_url}", flush=True)
    except Exception as e:
        print(f"❌ Cloudinary video upload failed: {e!r}", flush=True)
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
        SELECT s.*, u.business_name, u.business_image_url
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
    service = await database.fetch_one(
        "SELECT * FROM services WHERE service_id = :sid AND is_active = TRUE",
        {"sid": service_id},
    )
    if not service:
        raise HTTPException(status_code=404, detail="Service not found or inactive")

    customer_id = current_user["id"]
    if customer_id == service["provider_id"]:
        raise HTTPException(status_code=400, detail="You cannot book your own service")

    # If no availability row exists, fetch_val returns None and we treat that
    # as "available by default" — only an explicit False blocks booking.
    provider_available = await database.fetch_val(
        "SELECT is_available FROM provider_availability WHERE user_id = :pid",
        {"pid": service["provider_id"]},
    )
    if provider_available is False:
        raise HTTPException(status_code=400, detail="Provider is currently unavailable")

    service_price = _as_float(service["price"])

    wallet = await database.fetch_one(
        "SELECT balance FROM wallets WHERE user_id = :uid", {"uid": customer_id}
    )
    if not wallet or _as_float(wallet["balance"]) < service_price:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    await database.execute(
        "UPDATE wallets SET balance = balance - :amt WHERE user_id = :uid",
        {"amt": service_price, "uid": customer_id},
    )

    booking_id = uuid.uuid4().hex[:8]
    now = datetime.utcnow()
    scheduled_dt = _parse_iso_datetime(req.scheduled_for)

    await database.execute(
        """
        INSERT INTO service_bookings (
            booking_id, service_id, customer_id, provider_id, amount,
            status, scheduled_for, location_lat, location_lng, notes, created_at
        )
        VALUES (
            :bid, :sid, :cid, :pid, :amt,
            'locked', :sch, :lat, :lng, :notes, :now
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
            "now": now,
        },
    )

    # Audit row for the escrow debit. The booking_id ties the wallet
    # transaction back to the booking so support can trace the flow.
    await _record_wallet_txn(
        user_id=customer_id,
        amount=service_price,
        txn_type="debit",
        description=f"Escrow hold: booking {booking_id}",
        reference=f"book:{booking_id}",
        now=now,
    )

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
               u.nickname       AS username,
               u.real_name,
               u.business_image_url,
               u.avatar_url
        FROM services s
        JOIN users u ON s.provider_id = u.id
        WHERE s.provider_id = :pid AND s.is_active = TRUE
        ORDER BY s.created_at DESC
        """,
        {"pid": provider_id},
    )
    return [dict(row) for row in rows]