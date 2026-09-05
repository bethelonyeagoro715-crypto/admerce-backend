from fastapi import APIRouter, HTTPException, Depends, Query
from app.db.database import database
from app.routes.auth import get_current_user
from typing import Optional
from datetime import datetime
import uuid

router = APIRouter(prefix="/admin", tags=["Admin"])

# ---------- Admin guard ----------
async def admin_required(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


# Helper functions
def _subtract_months(dt: datetime, months: int) -> datetime:
    target_month = dt.month - months
    target_year = dt.year
    while target_month <= 0:
        target_month += 12
        target_year -= 1
    day = min(dt.day, [0, 31, 29 if target_year % 4 == 0 and (target_year % 100 != 0 or target_year % 400 == 0) else 28,
                       31, 30, 31, 30, 31, 31, 30, 31, 30, 31][target_month])
    return datetime(target_year, target_month, day)


def _month_start(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, 1)


def _time_ago(iso_date: str) -> str:
    if not iso_date:
        return "Just now"
    try:
        dt = datetime.fromisoformat(iso_date.replace('Z', '+00:00'))
        now = datetime.utcnow()
        diff = now - dt
        seconds = diff.total_seconds()
        if seconds < 60:
            return "Just now"
        elif seconds < 3600:
            mins = int(seconds / 60)
            return f"{mins} min{'s' if mins > 1 else ''} ago"
        elif seconds < 86400:
            hours = int(seconds / 3600)
            return f"{hours} hour{'s' if hours > 1 else ''} ago"
        elif seconds < 604800:
            days = int(seconds / 86400)
            return f"{days} day{'s' if days > 1 else ''} ago"
        else:
            return dt.strftime("%d %b %Y")
    except:
        return "Just now"


# ============================================================
# 1. DASHBOARD STATS
# ============================================================
@router.get("/stats")
async def platform_stats(admin: dict = Depends(admin_required)):
    now = datetime.utcnow()

    total_users = await database.fetch_val("SELECT COUNT(*) FROM users") or 0
    total_stores = await database.fetch_val("SELECT COUNT(*) FROM stores") or 0
    total_orders = await database.fetch_val("SELECT COUNT(*) FROM escrow WHERE status = 'completed'") or 0

    # FIX: cast total_amount to numeric, using NULLIF for empty strings
    total_revenue = await database.fetch_val(
        "SELECT COALESCE(SUM(NULLIF(total_amount, '')::numeric), 0) FROM escrow WHERE status = 'completed'"
    ) or 0

    last_month_start = _subtract_months(now, 1)
    previous_users = await database.fetch_val(
        "SELECT COUNT(*) FROM users WHERE created_at < :date",
        {"date": last_month_start.isoformat()}
    ) or 1
    growth = int(((total_users - previous_users) / previous_users) * 100)

    user_growth = []
    for i in range(11, -1, -1):
        month_start = _month_start(_subtract_months(now, i))
        next_month = _month_start(_subtract_months(now, i - 1))
        count = await database.fetch_val(
            "SELECT COUNT(*) FROM users WHERE created_at >= :start AND created_at < :end",
            {"start": month_start.isoformat(), "end": next_month.isoformat()}
        ) or 0
        user_growth.append(count)

    monthly_revenue = []
    for i in range(11, -1, -1):
        month_start = _month_start(_subtract_months(now, i))
        next_month = _month_start(_subtract_months(now, i - 1))
        revenue = await database.fetch_val(
            "SELECT COALESCE(SUM(NULLIF(total_amount, '')::numeric), 0) FROM escrow "
            "WHERE status = 'completed' AND created_at >= :start AND created_at < :end",
            {"start": month_start.isoformat(), "end": next_month.isoformat()}
        ) or 0
        monthly_revenue.append(revenue)

    category_sales_raw = await database.fetch_all(
        "SELECT category, COUNT(*) as count FROM listings GROUP BY category ORDER BY count DESC LIMIT 6"
    )
    category_sales = {row['category']: row['count'] for row in category_sales_raw}
    if not category_sales:
        category_sales = {"Groceries": 28, "Fashion": 22, "Electronics": 18, "Food": 15, "Services": 10, "Other": 7}

    orders_trend = []
    for i in range(11, -1, -1):
        month_start = _month_start(_subtract_months(now, i))
        next_month = _month_start(_subtract_months(now, i - 1))
        count = await database.fetch_val(
            "SELECT COUNT(*) FROM escrow WHERE created_at >= :start AND created_at < :end",
            {"start": month_start.isoformat(), "end": next_month.isoformat()}
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
            "time": _time_ago(user["created_at"])
        })

    recent_stores = await database.fetch_all(
        "SELECT name, owner_id, created_at FROM stores ORDER BY created_at DESC LIMIT 2"
    )
    for store in recent_stores:
        recent_activity.append({
            "type": "store",
            "action": "opened",
            "name": store["name"],
            "time": _time_ago(store["created_at"])
        })

    recent_orders = await database.fetch_all(
        "SELECT order_id, shopper_id, created_at FROM escrow ORDER BY created_at DESC LIMIT 2"
    )
    for order in recent_orders:
        recent_activity.append({
            "type": "order",
            "action": "placed",
            "name": f"Order #{order['order_id'][:8]}",
            "time": _time_ago(order["created_at"])
        })

    # Keep recent activity limited to 5 (no sorting by time string)
    recent_activity = recent_activity[:5]

    top_stores = await database.fetch_all("""
        SELECT 
            s.name,
            COUNT(l.listing_id) as items,
            COALESCE(SUM(NULLIF(e.total_amount, '')::numeric), 0) as revenue,
            COALESCE(AVG(r.rating), 0) as rating
        FROM stores s
        LEFT JOIN listings l ON s.store_id = l.store_id
        LEFT JOIN escrow e ON s.store_id = e.storekeeper_id AND e.status = 'completed'
        LEFT JOIN (
            SELECT store_id, AVG(rating) as rating FROM reviews GROUP BY store_id
        ) r ON s.store_id = r.store_id
        GROUP BY s.store_id, s.name
        ORDER BY revenue DESC
        LIMIT 5
    """)
    top_stores_list = []
    for store in top_stores:
        top_stores_list.append({
            "name": store["name"],
            "items": store["items"] or 0,
            "revenue": store["revenue"] or 0,
            "rating": round(store["rating"] or 0, 1)
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
    admin: dict = Depends(admin_required)
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
        "SELECT * FROM stores WHERE owner_id = :uid",
        {"uid": user_id}
    )
    user_dict["store"] = dict(store) if store else None

    if user_dict.get("role") == "courier":
        courier = await database.fetch_one(
            "SELECT * FROM couriers WHERE courier_id = :uid",
            {"uid": user_id}
        )
        user_dict["courier"] = dict(courier) if courier else None

    return user_dict


@router.post("/users/{user_id}/suspend")
async def suspend_user(user_id: str, admin: dict = Depends(admin_required)):
    user = await database.fetch_one("SELECT id FROM users WHERE id = :uid", {"uid": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await database.execute(
        "UPDATE users SET suspended = 1, updated_at = :now WHERE id = :uid",
        {"uid": user_id, "now": datetime.utcnow().isoformat()}
    )
    return {"message": "User suspended"}


@router.post("/users/{user_id}/unsuspend")
async def unsuspend_user(user_id: str, admin: dict = Depends(admin_required)):
    user = await database.fetch_one("SELECT id FROM users WHERE id = :uid", {"uid": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await database.execute(
        "UPDATE users SET suspended = 0, updated_at = :now WHERE id = :uid",
        {"uid": user_id, "now": datetime.utcnow().isoformat()}
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
# 3. STORES MANAGEMENT
# ============================================================
@router.get("/stores")
async def list_stores(
    search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = 50,
    offset: int = 0,
    admin: dict = Depends(admin_required)
):
    base_query = """
        SELECT s.*, u.nickname as owner_name, u.email as owner_email,
               COUNT(l.listing_id) as total_items,
               (SELECT COUNT(*) FROM escrow WHERE storekeeper_id = s.store_id AND status = 'completed') as total_orders
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
        conditions.append("s.verified = :verified")
        params["verified"] = status == "Active"
    if conditions:
        base_query += " WHERE " + " AND ".join(conditions)
    base_query += " GROUP BY s.store_id, u.nickname, u.email ORDER BY s.created_at DESC LIMIT :l OFFSET :o"
    params["l"] = limit
    params["o"] = offset
    rows = await database.fetch_all(base_query, params)
    return [dict(row) for row in rows]


@router.get("/stores/{store_id}")
async def get_store_detail(store_id: str, admin: dict = Depends(admin_required)):
    store = await database.fetch_one("SELECT * FROM stores WHERE store_id = :sid", {"sid": store_id})
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    return dict(store)


@router.post("/stores/{store_id}/verify")
async def verify_store(store_id: str, admin: dict = Depends(admin_required)):
    store = await database.fetch_one("SELECT store_id FROM stores WHERE store_id = :sid", {"sid": store_id})
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    await database.execute(
        "UPDATE stores SET verified = 1, updated_at = :now WHERE store_id = :sid",
        {"sid": store_id, "now": datetime.utcnow().isoformat()}
    )
    return {"message": "Store verified"}


@router.post("/stores/{store_id}/suspend")
async def suspend_store(store_id: str, admin: dict = Depends(admin_required)):
    store = await database.fetch_one("SELECT store_id FROM stores WHERE store_id = :sid", {"sid": store_id})
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    await database.execute(
        "UPDATE stores SET verified = 0, updated_at = :now WHERE store_id = :sid",
        {"sid": store_id, "now": datetime.utcnow().isoformat()}
    )
    return {"message": "Store suspended"}


@router.delete("/stores/{store_id}")
async def admin_delete_store(store_id: str, admin: dict = Depends(admin_required)):
    store = await database.fetch_one("SELECT store_id FROM stores WHERE store_id = :sid", {"sid": store_id})
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    await database.execute("DELETE FROM listings WHERE store_id = :sid", {"sid": store_id})
    await database.execute("DELETE FROM stores WHERE store_id = :sid", {"sid": store_id})
    return {"message": f"Store {store_id} and its listings deleted"}


# ============================================================
# 4. ORDERS MANAGEMENT
# ============================================================
@router.get("/orders")
async def list_orders(
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = 50,
    offset: int = 0,
    admin: dict = Depends(admin_required)
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
    admin: dict = Depends(admin_required)
):
    valid_statuses = ['pending', 'shipped', 'completed', 'cancelled']
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {valid_statuses}")
    order = await database.fetch_one("SELECT order_id FROM escrow WHERE order_id = :oid", {"oid": order_id})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    await database.execute(
        "UPDATE escrow SET status = :status, updated_at = :now WHERE order_id = :oid",
        {"status": status, "now": datetime.utcnow().isoformat(), "oid": order_id}
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
# 5. LISTINGS MANAGEMENT
# ============================================================
@router.get("/listings")
async def admin_get_listings(
    search: Optional[str] = Query(None),
    limit: int = 50,
    offset: int = 0,
    admin: dict = Depends(admin_required)
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
# 6. COURIERS MANAGEMENT
# ============================================================
@router.get("/couriers")
async def admin_get_couriers(
    search: Optional[str] = Query(None),
    limit: int = 50,
    offset: int = 0,
    admin: dict = Depends(admin_required)
):
    query = "SELECT * FROM couriers"
    params = {}
    if search:
        query += " WHERE name LIKE :search OR courier_id LIKE :search"
        params["search"] = f"%{search}%"
    # Removed ORDER BY created_at because couriers table doesn't have that column
    query += " LIMIT :limit OFFSET :offset"
    params["limit"] = limit
    params["offset"] = offset
    rows = await database.fetch_all(query, params)
    return [dict(row) for row in rows]


# ============================================================
# 7. FLIPPERS MANAGEMENT
# ============================================================
@router.get("/flippers")
async def admin_get_flippers(
    search: Optional[str] = Query(None),
    limit: int = 50,
    offset: int = 0,
    admin: dict = Depends(admin_required)
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


# ============================================================
# 8. SERVICES MANAGEMENT
# ============================================================
@router.get("/services")
async def admin_get_services(
    search: Optional[str] = Query(None),
    limit: int = 50,
    offset: int = 0,
    admin: dict = Depends(admin_required)
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


# ============================================================
# 9. SERVICE PROVIDERS MANAGEMENT
# ============================================================
@router.get("/service-providers")
async def admin_get_service_providers(
    search: Optional[str] = Query(None),
    limit: int = 50,
    offset: int = 0,
    admin: dict = Depends(admin_required)
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
# 10. TRANSACTIONS
# ============================================================
@router.get("/transactions")
async def list_transactions(
    limit: int = 100,
    offset: int = 0,
    admin: dict = Depends(admin_required)
):
    rows = await database.fetch_all(
        "SELECT * FROM transactions ORDER BY created_at DESC LIMIT :l OFFSET :o",
        {"l": limit, "o": offset}
    )
    return [dict(row) for row in rows]


# ============================================================
# 11. PROMOTIONS
# ============================================================
@router.get("/promotions")
async def get_promotions():
    rows = await database.fetch_all(
        "SELECT * FROM promotions WHERE is_active = 1 ORDER BY position ASC"
    )
    return [dict(row) for row in rows]


@router.post("/promotions")
async def create_promotion(
    image_url: str,
    title: str,
    subtitle: Optional[str] = None,
    target_url: Optional[str] = None,
    position: int = 0,
    admin: dict = Depends(admin_required)
):
    promo_id = uuid.uuid4().hex
    await database.execute(
        """
        INSERT INTO promotions (id, image_url, title, subtitle, target_url, position, is_active)
        VALUES (:id, :img, :title, :sub, :url, :pos, 1)
        """,
        {
            "id": promo_id,
            "img": image_url,
            "title": title,
            "sub": subtitle,
            "url": target_url,
            "pos": position,
        }
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
    admin: dict = Depends(admin_required)
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


# ============================================================
# 12. DISPUTES (Placeholder)
# ============================================================
@router.get("/disputes")
async def list_disputes(admin: dict = Depends(admin_required)):
    return []


# ============================================================
# 13. PLATFORM SETTINGS (Placeholder)
# ============================================================
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