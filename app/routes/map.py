from fastapi import APIRouter, Query
from typing import Optional
from app.db.database import database

router = APIRouter(prefix="/map", tags=["Map"])


# ── Haversine distance in SQL (km) ───────────────────────────────────
# Portable — doesn't require PostGIS or earthdistance extensions.
# Only included in the query when the client sends lat/lng.
_HAVERSINE = """
    6371 * 2 * ASIN(SQRT(
        POWER(SIN(RADIANS(({lat_col} - :user_lat) / 2)), 2)
        + COS(RADIANS(:user_lat))
        * COS(RADIANS({lat_col}))
        * POWER(SIN(RADIANS(({lng_col} - :user_lng) / 2)), 2)
    ))
"""


@router.get("/locations")
async def get_map_locations(
    lat: Optional[float] = Query(None, ge=-90, le=90),
    lng: Optional[float] = Query(None, ge=-180, le=180),
    radius_km: float = Query(50.0, ge=0.5, le=500),
    include_stores: bool = Query(True),
    include_services: bool = Query(True),
    only_with_stock: bool = Query(False),
):
    """
    Map markers for stores and/or services.

    When lat/lng are provided:
      - only items within `radius_km` are returned
      - each item gets a `distance_km` field
      - results are sorted by distance ascending

    When lat/lng are omitted:
      - returns everything (cap at 500 total, no distance)
      - sorted by name ascending

    Store status logic:
      - `in_stock`   → at least one available listing, or an untracked one
      - `low_stock`  → total tracked stock ≤ 3
      - `out_of_stock` → all listings have 0 tracked stock
    """
    if not include_stores and not include_services:
        return []

    has_origin = lat is not None and lng is not None
    results: list[dict] = []

    # ── STORES ───────────────────────────────────────────────────────
    if include_stores:
        distance_select = ""
        distance_where = ""
        params: dict = {}

        if has_origin:
            distance_select = (
                f", {_HAVERSINE.format(lat_col='s.latitude', lng_col='s.longitude')}"
                " AS distance_km"
            )
            distance_where = (
                f" AND {_HAVERSINE.format(lat_col='s.latitude', lng_col='s.longitude')}"
                " <= :radius_km"
            )
            params["user_lat"] = lat
            params["user_lng"] = lng
            params["radius_km"] = radius_km

        order_clause = "distance_km ASC" if has_origin else "s.name ASC"
        stock_filter = (
            "HAVING COALESCE(SUM(l.quantity_available), 0) > 0 "
            "OR COUNT(l.listing_id) FILTER (WHERE l.quantity_available IS NULL) > 0"
            if only_with_stock
            else ""
        )

        store_sql = f"""
            SELECT
                s.store_id                              AS id,
                s.name                                  AS name,
                s.latitude                              AS lat,
                s.longitude                             AS lng,
                'store'                                 AS type,
                s.address                               AS address,
                s.store_image_url                       AS image_url,
                s.verification_status                   AS verification_status,
                COUNT(l.listing_id)                     AS listing_count,
                COALESCE(SUM(l.quantity_available), 0)  AS tracked_stock,
                COUNT(l.listing_id) FILTER (
                    WHERE l.quantity_available IS NULL
                )                                       AS untracked_count,
                CASE
                    WHEN COUNT(l.listing_id) = 0 THEN 'out_of_stock'
                    WHEN COUNT(l.listing_id) FILTER (
                        WHERE l.quantity_available IS NULL
                    ) > 0 THEN 'in_stock'
                    WHEN COALESCE(SUM(l.quantity_available), 0) = 0 THEN 'out_of_stock'
                    WHEN SUM(l.quantity_available) <= 3 THEN 'low_stock'
                    ELSE 'in_stock'
                END                                     AS status
                {distance_select}
            FROM stores s
            LEFT JOIN listings l ON s.store_id = l.store_id
            WHERE s.verification_status IS DISTINCT FROM 'suspended'
            {distance_where}
            GROUP BY s.store_id, s.name, s.latitude, s.longitude,
                     s.address, s.store_image_url, s.verification_status
            {stock_filter}
            ORDER BY {order_clause}
            LIMIT 500
        """

        store_rows = await database.fetch_all(store_sql, params)
        results.extend([dict(r) for r in store_rows])

    # ── SERVICES ─────────────────────────────────────────────────────
    if include_services:
        distance_select = ""
        distance_where = ""
        params: dict = {}

        if has_origin:
            distance_select = (
                f", {_HAVERSINE.format(lat_col='sv.lat', lng_col='sv.lng')}"
                " AS distance_km"
            )
            distance_where = (
                f" AND {_HAVERSINE.format(lat_col='sv.lat', lng_col='sv.lng')}"
                " <= :radius_km"
            )
            params["user_lat"] = lat
            params["user_lng"] = lng
            params["radius_km"] = radius_km

        order_clause = "distance_km ASC" if has_origin else "sv.title ASC"

        service_sql = f"""
            SELECT
                sv.service_id       AS id,
                sv.title            AS name,
                sv.lat              AS lat,
                sv.lng              AS lng,
                'service'           AS type,
                sv.category         AS category,
                sv.price            AS price,
                sv.image_url        AS image_url,
                'available'         AS status
                {distance_select}
            FROM services sv
            WHERE sv.is_active = TRUE
              AND sv.lat IS NOT NULL
              AND sv.lng IS NOT NULL
              {distance_where}
            ORDER BY {order_clause}
            LIMIT 500
        """

        service_rows = await database.fetch_all(service_sql, params)
        results.extend([dict(r) for r in service_rows])

    # When both kinds returned, sort the merged list once more so
    # stores + services interleave correctly by distance.
    if has_origin:
        results.sort(key=lambda r: (r.get("distance_km") is None, r.get("distance_km") or 0))

    return results