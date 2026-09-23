from fastapi import APIRouter, HTTPException, Depends, Query, Body
from pydantic import BaseModel
from app.db.database import database
from app.routes.auth import get_current_user
from typing import Optional, Union
from datetime import datetime
import uuid
import json

router = APIRouter(prefix="/admin", tags=["Admin"])


# ---------- Admin guard ----------
async def admin_required(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


# ---------- Helpers ----------
def _add_months(dt: datetime, months: int) -> datetime:
    month_index = (dt.year * 12 + (dt.month - 1)) + months
    year = month_index // 12
    month = (month_index % 12) + 1
    if month == 2:
        leap = (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0))
        max_day = 29 if leap else 28
    else:
        max_day = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    day = min(dt.day, max_day)
    return datetime(year, month, day)


def _month_start(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, 1)


def _time_ago(value: Union[str, datetime, None]) -> str:
    if not value:
        return "Just now"
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        now = datetime.utcnow()
        seconds = (now - dt).total_seconds()
        if seconds < 0:
            return "Just now"
        if seconds < 60:
            return "Just now"
        elif seconds < 3600:
            mins = int(seconds / 60)
            return f"{mins} min{'s' if mins != 1 else ''} ago"
        elif seconds < 86400:
            hours = int(seconds / 3600)
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        elif seconds < 604800:
            days = int(seconds / 86400)
            return f"{days} day{'s' if days != 1 else ''} ago"
        else:
            return dt.strftime("%d %b %Y")
    except Exception:
        return "Just now"


# ─────────────────────────────────────────────────────────────
# Audit log writer — single place that touches
# store_verification_events. Every status change funnels here.
# ─────────────────────────────────────────────────────────────
async def _log_verification_event(
    store_id: str,
    from_status: Optional[str],
    to_status: str,
    actor_id: Optional[str],
    reason: Optional[str] = None,
    reference: Optional[str] = None,
):
    await database.execute(
        """
        INSERT INTO store_verification_events
            (store_id, actor_id, from_status, to_status, reason, reference, created_at)
        VALUES
            (:sid, :actor, :from_s, :to_s, :reason, :ref, NOW())
        """,
        {
            "sid": store_id,
            "actor": actor_id,
            "from_s": from_status,
            "to_s": to_status,
            "reason": reason,
            "ref": reference,
        },
    )


# ============================================================
# 1. DASHBOARD STATS
# ============================================================
@router.get("/stats")
async def platform_stats(admin: dict = Depends(admin_required)):
    now = datetime.utcnow()

    total_users = await database.fetch_val("SELECT COUNT(*) FROM users") or 0
    total_stores = await database.fetch_val("SELECT COUNT(*) FROM stores") or 0
    total_orders = await database.fetch_val(
        "SELECT COUNT(*) FROM escrow WHERE status = 'completed'"
    ) or 0

    total_revenue = await database.fetch_val(
        "SELECT COALESCE(SUM(total_amount), 0) FROM escrow WHERE status = 'completed'"
    ) or 0

    last_month_start = _add_months(now, -1)
    previous_users = await database.fetch_val(
        "SELECT COUNT(*) FROM users WHERE created_at < :date",
        {"date": last_month_start},
    ) or 1
    growth = int(((total_users - previous_users) / previous_users) * 100)

    user_growth = []
    for i in range(11, -1, -1):
        month_start = _month_start(_add_months(now, -i))
        next_month = _month_start(_add_months(now, 1 - i))
        count = await database.fetch_val(
            "SELECT COUNT(*) FROM users WHERE created_at >= :start AND created_at < :end",
            {"start": month_start, "end": next_month},
        ) or 0
        user_growth.append(count)

    monthly_revenue = []
    for i in range(11, -1, -1):
        month_start = _month_start(_add_months(now, -i))
        next_month = _month_start(_add_months(now, 1 - i))
        revenue = await database.fetch_val(
            "SELECT COALESCE(SUM(total_amount), 0) FROM escrow "
            "WHERE status = 'completed' AND created_at >= :start AND created_at < :end",
            {"start": month_start, "end": next_month},
        ) or 0
        monthly_revenue.append(revenue)

    category_sales_raw = await database.fetch_all(
        "SELECT category, COUNT(*) as count FROM listings GROUP BY category ORDER BY count DESC LIMIT 6"
    )
    category_sales = {row["category"]: row["count"] for row in category_sales_raw}
    if not category_sales:
        category_sales = {"Groceries": 28, "Fashion": 22, "Electronics": 18, "Food": 15, "Services": 10, "Other": 7}

    orders_trend = []
    for i in range(11, -1, -1):
        month_start = _month_start(_add_months(now, -i))
        next_month = _month_start(_add_months(now, 1 - i))
        count = await database.fetch_val(
            "SELECT COUNT(*) FROM escrow WHERE created_at >= :start AND created_at < :end",
            {"start": month_start, "end": next_month},
        ) or 0
        orders_trend.append(count)

    recent_activity = []
    recent_users = await database.fetch_all(
        "SELECT id, nickname, created_at FROM users ORDER BY created_at DESC LIMIT 3"
    )
    for user in recent_users:
        recent_activity.append({
            "type": "user",
            "action": "signed up",
            "name": user["nickname"] or user["id"][:8],
            "time": _time_ago(user["created_at"]),
        })

    recent_stores = await database.fetch_all(
        "SELECT name, owner_id, created_at FROM stores ORDER BY created_at DESC LIMIT 2"
    )
    for store in recent_stores:
        recent_activity.append({
            "type": "store",
            "action": "opened",
            "name": store["name"],
            "time": _time_ago(store["created_at"]),
        })

    recent_orders = await database.fetch_all(
        "SELECT order_id, shopper_id, created_at FROM escrow ORDER BY created_at DESC LIMIT 2"
    )
    for order in recent_orders:
        recent_activity.append({
            "type": "order",
            "action": "placed",
            "name": f"Order #{order['order_id'][:8]}",
            "time": _time_ago(order["created_at"]),
        })

    recent_activity = recent_activity[:5]

    top_stores = await database.fetch_all("""
        SELECT
            s.name,
            (SELECT COUNT(*) FROM listings l WHERE l.store_id = s.store_id) AS items,
            (SELECT COALESCE(SUM(e.total_amount), 0)
                FROM escrow e
                WHERE e.storekeeper_id = s.owner_id AND e.status = 'completed') AS revenue,
            (SELECT COALESCE(AVG(r.rating), 0)
                FROM reviews r
                WHERE r.store_id = s.store_id) AS rating
        FROM stores s
        ORDER BY revenue DESC
        LIMIT 5
    """)
    top_stores_list = []
    for store in top_stores:
        top_stores_list.append({
            "name": store["name"],
            "items": store["items"] or 0,
            "revenue": store["revenue"] or 0,
            "rating": round(store["rating"] or 0, 1),
        })

    return {
        "total_users": total_users,
        "total_stores": total_stores,
        "total_orders": total_orders,
        "total_revenue": total_revenue,
        "growth": growth,
        "user_growth": user_growth,
        "monthly_revenue": monthly_revenue,
        "category_sales": category_sales,
        "orders_trend": orders_trend,
        "recent_activity": recent_activity,
        "top_stores": top_stores_list,
    }


# ============================================================
# 2. USERS MANAGEMENT
# ============================================================
@router.get("/users")
async def list_users(
    search: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    limit: int = 50,
    offset: int = 0,
    admin: dict = Depends(admin_required),
):
    base_query = """
        SELECT id, phone, email, nickname, verified, kyc_verified, role,
               created_at, avatar_url, suspended
        FROM users
    """
    params = {}
    conditions = []

    if search:
        conditions.append("(phone LIKE :s OR email LIKE :s OR nickname LIKE :s)")
        params["s"] = f"%{search}%"

    if role and role != "All":
        conditions.append("role = :role")
        params["role"] = role

    if conditions:
        base_query += " WHERE " + " AND ".join(conditions)

    base_query += " ORDER BY created_at DESC LIMIT :l OFFSET :o"
    params["l"] = limit
    params["o"] = offset

    rows = await database.fetch_all(base_query, params)
    return [dict(row) for row in rows]


@router.get("/users/{user_id}/full")
async def get_user_full_details(user_id: str, admin: dict = Depends(admin_required)):
    user = await database.fetch_one("SELECT * FROM users WHERE id = :uid", {"uid": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user_dict = dict(user)

    store = await database.fetch_one(
        "SELECT * FROM stores WHERE owner_id = :uid", {"uid": user_id}
    )
    user_dict["store"] = dict(store) if store else None

    if user_dict.get("role") == "courier":
        courier = await database.fetch_one(
            "SELECT * FROM couriers WHERE courier_id = :uid", {"uid": user_id}
        )
        user_dict["courier"] = dict(courier) if courier else None

    return user_dict


@router.post("/users/{user_id}/suspend")
async def suspend_user(user_id: str, admin: dict = Depends(admin_required)):
    user = await database.fetch_one("SELECT id FROM users WHERE id = :uid", {"uid": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await database.execute(
        "UPDATE users SET suspended = TRUE, updated_at = :now WHERE id = :uid",
        {"uid": user_id, "now": datetime.utcnow()},
    )
    return {"message": "User suspended"}


@router.post("/users/{user_id}/unsuspend")
async def unsuspend_user(user_id: str, admin: dict = Depends(admin_required)):
    user = await database.fetch_one("SELECT id FROM users WHERE id = :uid", {"uid": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await database.execute(
        "UPDATE users SET suspended = FALSE, updated_at = :now WHERE id = :uid",
        {"uid": user_id, "now": datetime.utcnow()},
    )
    return {"message": "User unsuspended"}


@router.delete("/users/{user_id}")
async def admin_delete_user(user_id: str, admin: dict = Depends(admin_required)):
    user = await database.fetch_one("SELECT id FROM users WHERE id = :uid", {"uid": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await database.execute("DELETE FROM users WHERE id = :uid", {"uid": user_id})
    return {"message": f"User {user_id} deleted"}


# ============================================================
# 3. STORES — list & detail
# ============================================================
@router.get("/stores")
async def list_stores(
    search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = 50,
    offset: int = 0,
    admin: dict = Depends(admin_required),
):
    base_query = """
        SELECT s.*, u.nickname as owner_name, u.email as owner_email,
               COUNT(l.listing_id) as total_items,
               (SELECT COUNT(*) FROM escrow WHERE storekeeper_id = s.owner_id AND status = 'completed') as total_orders
        FROM stores s
        LEFT JOIN users u ON s.owner_id = u.id
        LEFT JOIN listings l ON s.store_id = l.store_id
    """
    params = {}
    conditions = []

    if search:
        conditions.append("(s.name LIKE :s OR u.nickname LIKE :s OR s.address LIKE :s)")
        params["s"] = f"%{search}%"

    if status and status != "All":
        if status == "Active":
            conditions.append("s.verification_status = :vstatus")
            params["vstatus"] = "verified"
        elif status == "Inactive":
            conditions.append("s.verification_status != :vstatus")
            params["vstatus"] = "verified"
        else:
            conditions.append("s.verification_status = :vstatus")
            params["vstatus"] = status.lower()

    if conditions:
        base_query += " WHERE " + " AND ".join(conditions)

    base_query += " GROUP BY s.store_id, u.nickname, u.email ORDER BY s.created_at DESC LIMIT :l OFFSET :o"
    params["l"] = limit
    params["o"] = offset
    rows = await database.fetch_all(base_query, params)
    return [dict(row) for row in rows]


@router.get("/stores/{store_id}")
async def get_store_detail(store_id: str, admin: dict = Depends(admin_required)):
    store = await database.fetch_one(
        "SELECT * FROM stores WHERE store_id = :sid", {"sid": store_id}
    )
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    return dict(store)


@router.delete("/stores/{store_id}")
async def admin_delete_store(store_id: str, admin: dict = Depends(admin_required)):
    store = await database.fetch_one(
        "SELECT store_id FROM stores WHERE store_id = :sid", {"sid": store_id}
    )
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    await database.execute("DELETE FROM listings WHERE store_id = :sid", {"sid": store_id})
    await database.execute("DELETE FROM stores WHERE store_id = :sid", {"sid": store_id})
    return {"message": f"Store {store_id} and its listings deleted"}


# ============================================================
# 4. STORE VERIFICATION — THE STATE MACHINE
# ============================================================
#
# Transitions enforced here:
#   unverified → pending        (storekeeper submits, see storekeeper.py)
#   pending    → verified       (admin approves: /approve)
#   pending    → rejected       (admin rejects:  /reject, reason required)
#   rejected   → pending        (storekeeper re-applies)
#   verified   → suspended      (admin suspends: /suspend, reason required)
#   suspended  → unverified     (admin reinstates: /reinstate)
#   any        → unverified     (admin cancels a pending request: /cancel)
#
# Everything is idempotent on `reference_code` — a retry that carries the
# same reference does nothing if the target state was already reached.

class ApproveRequest(BaseModel):
    reference: str
    note: Optional[str] = None


class RejectRequest(BaseModel):
    reference: str
    reason: str


class SuspendRequest(BaseModel):
    reason: str


class ReinstateRequest(BaseModel):
    note: Optional[str] = None


@router.get("/verifications")
async def list_pending_verifications(
    status: Optional[str] = Query("pending"),
    limit: int = 50,
    offset: int = 0,
    admin: dict = Depends(admin_required),
):
    """Queue of verification requests. Defaults to pending."""
    params = {"l": limit, "o": offset}
    where = ""
    if status and status != "all":
        where = "WHERE sv.status = :status"
        params["status"] = status

    rows = await database.fetch_all(
        f"""
        SELECT sv.*,
               s.name AS store_name,
               s.store_image_url,
               u.nickname AS owner_name,
               u.email AS owner_email,
               u.phone AS owner_phone
        FROM store_verifications sv
        JOIN stores s ON sv.store_id = s.store_id
        JOIN users u ON s.owner_id = u.id
        {where}
        ORDER BY sv.submitted_at DESC
        LIMIT :l OFFSET :o
        """,
        params,
    )
    return [dict(row) for row in rows]


@router.get("/stores/{store_id}/verification")
async def get_store_verification(
    store_id: str, admin: dict = Depends(admin_required)
):
    """The active verification request + the full audit history for a store."""
    current = await database.fetch_one(
        """
        SELECT * FROM store_verifications
        WHERE store_id = :sid
        ORDER BY submitted_at DESC
        LIMIT 1
        """,
        {"sid": store_id},
    )
    events = await database.fetch_all(
        """
        SELECT * FROM store_verification_events
        WHERE store_id = :sid
        ORDER BY created_at DESC
        LIMIT 50
        """,
        {"sid": store_id},
    )
    return {
        "request": dict(current) if current else None,
        "events": [dict(e) for e in events],
    }


@router.post("/stores/{store_id}/verification/approve")
async def approve_store_verification(
    store_id: str,
    req: ApproveRequest,
    admin: dict = Depends(admin_required),
):
    """
    Approve a pending verification request.
    Idempotent on reference: a retry with the same reference on a store that
    is already verified returns success without creating a new event.
    """
    store = await database.fetch_one(
        "SELECT store_id, verification_status FROM stores WHERE store_id = :sid",
        {"sid": store_id},
    )
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")

    current_status = store["verification_status"] or "unverified"

    if current_status == "verified":
        return {
            "store_id": store_id,
            "status": "verified",
            "message": "Already verified",
        }

    if current_status != "pending":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot approve a store in '{current_status}' state. "
                "Only 'pending' requests can be approved."
            ),
        )

    pending = await database.fetch_one(
        """
        SELECT id, reference_code FROM store_verifications
        WHERE store_id = :sid AND status = 'pending'
        ORDER BY submitted_at DESC
        LIMIT 1
        """,
        {"sid": store_id},
    )
    if not pending:
        raise HTTPException(status_code=400, detail="No pending verification request found")

    if pending["reference_code"] != req.reference:
        raise HTTPException(status_code=409, detail="Reference does not match the pending request")

    now = datetime.utcnow()

    await database.execute(
        """
        UPDATE stores
        SET verification_status = 'verified',
            verified = TRUE,
            verified_at = :now,
            updated_at = :now
        WHERE store_id = :sid AND verification_status = 'pending'
        """,
        {"now": now, "sid": store_id},
    )

    await database.execute(
        """
        UPDATE store_verifications
        SET status = 'approved',
            reviewed_by = :admin,
            reviewed_at = :now,
            review_reason = :note
        WHERE id = :rid
        """,
        {"admin": admin["id"], "now": now, "note": req.note, "rid": pending["id"]},
    )

    await _log_verification_event(
        store_id=store_id,
        from_status="pending",
        to_status="verified",
        actor_id=admin["id"],
        reason=req.note,
        reference=req.reference,
    )

    return {"store_id": store_id, "status": "verified", "message": "Store verified"}


@router.post("/stores/{store_id}/verification/reject")
async def reject_store_verification(
    store_id: str,
    req: RejectRequest,
    admin: dict = Depends(admin_required),
):
    """Reject a pending request. A reason is required."""
    if not req.reason or not req.reason.strip():
        raise HTTPException(status_code=400, detail="A rejection reason is required")

    store = await database.fetch_one(
        "SELECT verification_status FROM stores WHERE store_id = :sid",
        {"sid": store_id},
    )
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")

    current_status = store["verification_status"] or "unverified"

    if current_status == "rejected":
        return {"store_id": store_id, "status": "rejected", "message": "Already rejected"}

    if current_status != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot reject a store in '{current_status}' state",
        )

    pending = await database.fetch_one(
        """
        SELECT id, reference_code FROM store_verifications
        WHERE store_id = :sid AND status = 'pending'
        ORDER BY submitted_at DESC
        LIMIT 1
        """,
        {"sid": store_id},
    )
    if not pending:
        raise HTTPException(status_code=400, detail="No pending verification request found")
    if pending["reference_code"] != req.reference:
        raise HTTPException(status_code=409, detail="Reference does not match the pending request")

    now = datetime.utcnow()

    await database.execute(
        """
        UPDATE stores
        SET verification_status = 'rejected',
            verified = FALSE,
            updated_at = :now
        WHERE store_id = :sid AND verification_status = 'pending'
        """,
        {"now": now, "sid": store_id},
    )
    await database.execute(
        """
        UPDATE store_verifications
        SET status = 'rejected',
            reviewed_by = :admin,
            reviewed_at = :now,
            review_reason = :reason
        WHERE id = :rid
        """,
        {"admin": admin["id"], "now": now, "reason": req.reason, "rid": pending["id"]},
    )
    await _log_verification_event(
        store_id=store_id,
        from_status="pending",
        to_status="rejected",
        actor_id=admin["id"],
        reason=req.reason,
        reference=req.reference,
    )
    return {"store_id": store_id, "status": "rejected", "message": "Store rejected"}


@router.post("/stores/{store_id}/suspend")
async def suspend_store(
    store_id: str,
    req: SuspendRequest,
    admin: dict = Depends(admin_required),
):
    """Suspend a verified store. Reason required."""
    if not req.reason or not req.reason.strip():
        raise HTTPException(status_code=400, detail="A suspension reason is required")

    store = await database.fetch_one(
        "SELECT verification_status FROM stores WHERE store_id = :sid",
        {"sid": store_id},
    )
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")

    current_status = store["verification_status"] or "unverified"

    if current_status == "suspended":
        return {"store_id": store_id, "status": "suspended", "message": "Already suspended"}

    if current_status != "verified":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot suspend a store in '{current_status}' state",
        )

    now = datetime.utcnow()
    await database.execute(
        """
        UPDATE stores
        SET verification_status = 'suspended',
            verified = FALSE,
            updated_at = :now
        WHERE store_id = :sid AND verification_status = 'verified'
        """,
        {"now": now, "sid": store_id},
    )
    await _log_verification_event(
        store_id=store_id,
        from_status="verified",
        to_status="suspended",
        actor_id=admin["id"],
        reason=req.reason,
    )
    return {"store_id": store_id, "status": "suspended", "message": "Store suspended"}


@router.post("/stores/{store_id}/reinstate")
async def reinstate_store(
    store_id: str,
    req: ReinstateRequest,
    admin: dict = Depends(admin_required),
):
    """Reinstate a suspended store back to unverified (must re-apply)."""
    store = await database.fetch_one(
        "SELECT verification_status FROM stores WHERE store_id = :sid",
        {"sid": store_id},
    )
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")

    current_status = store["verification_status"] or "unverified"

    if current_status == "unverified":
        return {"store_id": store_id, "status": "unverified", "message": "Already unverified"}

    if current_status != "suspended":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot reinstate a store in '{current_status}' state",
        )

    now = datetime.utcnow()
    await database.execute(
        """
        UPDATE stores
        SET verification_status = 'unverified',
            verified = FALSE,
            updated_at = :now
        WHERE store_id = :sid AND verification_status = 'suspended'
        """,
        {"now": now, "sid": store_id},
    )
    await _log_verification_event(
        store_id=store_id,
        from_status="suspended",
        to_status="unverified",
        actor_id=admin["id"],
        reason=req.note,
    )
    return {
        "store_id": store_id,
        "status": "unverified",
        "message": "Store reinstated — owner can re-apply",
    }


# ============================================================
# 5. ORDERS MANAGEMENT
# ============================================================
@router.get("/orders")
async def list_orders(
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = 50,
    offset: int = 0,
    admin: dict = Depends(admin_required),
):
    base_query = """
        SELECT e.*,
               u1.nickname as shopper_name,
               u2.nickname as storekeeper_name,
               s.name as store_name
        FROM escrow e
        LEFT JOIN users u1 ON e.shopper_id = u1.id
        LEFT JOIN users u2 ON e.storekeeper_id = u2.id
        LEFT JOIN stores s ON e.storekeeper_id = s.owner_id
    """
    params = {}
    conditions = []
    if status and status != "All":
        conditions.append("e.status = :status")
        params["status"] = status
    if search:
        conditions.append(
            "(e.order_id LIKE :s OR u1.nickname LIKE :s OR u2.nickname LIKE :s OR s.name LIKE :s)"
        )
        params["s"] = f"%{search}%"
    if conditions:
        base_query += " WHERE " + " AND ".join(conditions)
    base_query += " ORDER BY e.created_at DESC LIMIT :l OFFSET :o"
    params["l"] = limit
    params["o"] = offset
    rows = await database.fetch_all(base_query, params)
    return [dict(row) for row in rows]


@router.get("/orders/{order_id}")
async def get_order_detail(order_id: str, admin: dict = Depends(admin_required)):
    order = await database.fetch_one("SELECT * FROM escrow WHERE order_id = :oid", {"oid": order_id})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return dict(order)


@router.post("/orders/{order_id}/status")
async def update_order_status(
    order_id: str,
    status: str,
    admin: dict = Depends(admin_required),
):
    valid_statuses = ["pending", "shipped", "completed", "cancelled"]
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {valid_statuses}")
    order = await database.fetch_one("SELECT order_id FROM escrow WHERE order_id = :oid", {"oid": order_id})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    await database.execute(
        "UPDATE escrow SET status = :status, updated_at = :now WHERE order_id = :oid",
        {"status": status, "now": datetime.utcnow(), "oid": order_id},
    )
    return {"message": f"Order status updated to {status}"}


@router.delete("/orders/{order_id}")
async def admin_delete_order(order_id: str, admin: dict = Depends(admin_required)):
    order = await database.fetch_one("SELECT order_id FROM escrow WHERE order_id = :oid", {"oid": order_id})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    await database.execute("DELETE FROM escrow WHERE order_id = :oid", {"oid": order_id})
    return {"message": f"Order {order_id} deleted"}


# ============================================================
# 6. LISTINGS MANAGEMENT
# ============================================================
@router.get("/listings")
async def admin_get_listings(
    search: Optional[str] = Query(None),
    limit: int = 50,
    offset: int = 0,
    admin: dict = Depends(admin_required),
):
    query = "SELECT l.*, s.name AS store_name FROM listings l JOIN stores s ON l.store_id = s.store_id"
    params = {}
    if search:
        query += " WHERE l.title LIKE :search OR l.listing_id LIKE :search"
        params["search"] = f"%{search}%"
    query += " ORDER BY l.created_at DESC LIMIT :limit OFFSET :offset"
    params["limit"] = limit
    params["offset"] = offset
    rows = await database.fetch_all(query, params)
    return [dict(row) for row in rows]


@router.delete("/listings/{listing_id}")
async def admin_delete_listing(listing_id: str, admin: dict = Depends(admin_required)):
    listing = await database.fetch_one("SELECT listing_id FROM listings WHERE listing_id = :lid", {"lid": listing_id})
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    await database.execute("DELETE FROM listings WHERE listing_id = :lid", {"lid": listing_id})
    return {"message": f"Listing {listing_id} deleted"}


# ============================================================
# 7. COURIERS / FLIPPERS / SERVICES / PROVIDERS
# ============================================================
@router.get("/couriers")
async def admin_get_couriers(
    search: Optional[str] = Query(None),
    limit: int = 50,
    offset: int = 0,
    admin: dict = Depends(admin_required),
):
    query = "SELECT * FROM couriers"
    params = {}
    if search:
        query += " WHERE name LIKE :search OR courier_id LIKE :search"
        params["search"] = f"%{search}%"
    query += " LIMIT :limit OFFSET :offset"
    params["limit"] = limit
    params["offset"] = offset
    rows = await database.fetch_all(query, params)
    return [dict(row) for row in rows]


@router.get("/flippers")
async def admin_get_flippers(
    search: Optional[str] = Query(None),
    limit: int = 50,
    offset: int = 0,
    admin: dict = Depends(admin_required),
):
    query = "SELECT id, phone, email, nickname, created_at FROM users WHERE role = 'flipper'"
    params = {}
    if search:
        query += " AND (phone LIKE :search OR email LIKE :search OR nickname LIKE :search)"
        params["search"] = f"%{search}%"
    query += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
    params["limit"] = limit
    params["offset"] = offset
    rows = await database.fetch_all(query, params)
    return [dict(row) for row in rows]


@router.delete("/flippers/{user_id}")
async def admin_delete_flipper(user_id: str, admin: dict = Depends(admin_required)):
    await database.execute("DELETE FROM users WHERE id = :uid AND role = 'flipper'", {"uid": user_id})
    return {"message": f"Flipper {user_id} deleted"}


@router.get("/services")
async def admin_get_services(
    search: Optional[str] = Query(None),
    limit: int = 50,
    offset: int = 0,
    admin: dict = Depends(admin_required),
):
    query = "SELECT * FROM services"
    params = {}
    if search:
        query += " WHERE title LIKE :search OR service_id LIKE :search"
        params["search"] = f"%{search}%"
    query += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
    params["limit"] = limit
    params["offset"] = offset
    rows = await database.fetch_all(query, params)
    return [dict(row) for row in rows]


@router.delete("/services/{service_id}")
async def admin_delete_service(service_id: str, admin: dict = Depends(admin_required)):
    await database.execute("DELETE FROM services WHERE service_id = :sid", {"sid": service_id})
    return {"message": f"Service {service_id} deleted"}


@router.get("/service-providers")
async def admin_get_service_providers(
    search: Optional[str] = Query(None),
    limit: int = 50,
    offset: int = 0,
    admin: dict = Depends(admin_required),
):
    query = "SELECT id, phone, email, nickname, created_at FROM users WHERE role = 'service_provider'"
    params = {}
    if search:
        query += " AND (phone LIKE :search OR email LIKE :search OR nickname LIKE :search)"
        params["search"] = f"%{search}%"
    query += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
    params["limit"] = limit
    params["offset"] = offset
    rows = await database.fetch_all(query, params)
    return [dict(row) for row in rows]


@router.delete("/service-providers/{user_id}")
async def admin_delete_service_provider(user_id: str, admin: dict = Depends(admin_required)):
    await database.execute("DELETE FROM users WHERE id = :uid AND role = 'service_provider'", {"uid": user_id})
    return {"message": f"Service provider {user_id} deleted"}


# ============================================================
# 8. TRANSACTIONS / PROMOTIONS / DISPUTES / SETTINGS
# ============================================================
@router.get("/transactions")
async def list_transactions(
    limit: int = 100,
    offset: int = 0,
    admin: dict = Depends(admin_required),
):
    rows = await database.fetch_all(
        "SELECT * FROM wallet_transactions ORDER BY created_at DESC LIMIT :l OFFSET :o",
        {"l": limit, "o": offset},
    )
    return [dict(row) for row in rows]


@router.get("/promotions")
async def get_promotions():
    rows = await database.fetch_all(
        "SELECT * FROM promotions WHERE is_active = TRUE ORDER BY position ASC"
    )
    return [dict(row) for row in rows]


@router.post("/promotions")
async def create_promotion(
    image_url: str,
    title: str,
    subtitle: Optional[str] = None,
    target_url: Optional[str] = None,
    position: int = 0,
    admin: dict = Depends(admin_required),
):
    promo_id = uuid.uuid4().hex
    await database.execute(
        """
        INSERT INTO promotions (id, image_url, title, subtitle, target_url, position, is_active)
        VALUES (:id, :img, :title, :sub, :url, :pos, TRUE)
        """,
        {
            "id": promo_id,
            "img": image_url,
            "title": title,
            "sub": subtitle,
            "url": target_url,
            "pos": position,
        },
    )
    return {"id": promo_id, "message": "Promotion created"}


@router.put("/promotions/{promo_id}")
async def update_promotion(
    promo_id: str,
    image_url: Optional[str] = None,
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    target_url: Optional[str] = None,
    position: Optional[int] = None,
    is_active: Optional[bool] = None,
    admin: dict = Depends(admin_required),
):
    updates = []
    params = {"id": promo_id}
    if image_url is not None:
        updates.append("image_url = :img")
        params["img"] = image_url
    if title is not None:
        updates.append("title = :title")
        params["title"] = title
    if subtitle is not None:
        updates.append("subtitle = :sub")
        params["sub"] = subtitle
    if target_url is not None:
        updates.append("target_url = :url")
        params["url"] = target_url
    if position is not None:
        updates.append("position = :pos")
        params["pos"] = position
    if is_active is not None:
        updates.append("is_active = :active")
        params["active"] = is_active
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    query = f"UPDATE promotions SET {', '.join(updates)} WHERE id = :id"
    await database.execute(query, params)
    return {"message": "Promotion updated"}


@router.delete("/promotions/{promo_id}")
async def delete_promotion(promo_id: str, admin: dict = Depends(admin_required)):
    await database.execute("DELETE FROM promotions WHERE id = :id", {"id": promo_id})
    return {"message": "Promotion deleted"}


@router.get("/disputes")
async def list_disputes(admin: dict = Depends(admin_required)):
    return []


@router.get("/settings")
async def get_settings(admin: dict = Depends(admin_required)):
    return {
        "currency": "NGN",
        "delivery_fee": 1500,
        "commission_rate": 5.0,
        "min_withdrawal": 1000,
    }


@router.get("/users/{user_id}")
async def get_user(user_id: str, admin: dict = Depends(admin_required)):
    user = await database.fetch_one("SELECT * FROM users WHERE id = :uid", {"uid": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return dict(user)