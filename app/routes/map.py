from fastapi import APIRouter
from app.db.database import database

router = APIRouter(prefix="/map", tags=["Map"])

@router.get("/locations")
async def get_map_locations():
    # Stores with stock information
    stores = await database.fetch_all(
        """
        SELECT s.store_id AS id, s.store_name AS name, s.lat, s.lng,
               'store' AS type,
               CASE
                   WHEN SUM(l.quantity_available) > 0 AND SUM(l.quantity_available) <= 3 THEN 'low_stock'
                   WHEN SUM(l.quantity_available) > 3 THEN 'in_stock'
                   ELSE 'out_of_stock'
               END AS status
        FROM stores s
        LEFT JOIN listings l ON s.store_id = l.store_id AND l.quantity_available > 0
        GROUP BY s.store_id, s.store_name, s.lat, s.lng
        """
    )

    # Services – no stock, always available
    services = await database.fetch_all(
        "SELECT service_id AS id, title AS name, lat, lng, 'service' AS type, 'available' AS status FROM services"
    )

    results = [dict(store) for store in stores] + [dict(service) for service in services]
    return results