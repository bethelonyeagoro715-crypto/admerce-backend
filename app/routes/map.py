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

    Service aggregation:
      Services are grouped by `provider_id` so a provider with N services
      renders as ONE marker. The marker's coordinates are the average of
      their service locations; `service_count`, `min_price`, and
      `max_price` are also returned so the sheet can summarize.
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

    # ── SERVICES (aggregated per provider) ───────────────────────────
    #
    # Previously: one row per service → providers with N services
    # rendered as N scattered pins. Now: one row per provider_id, with
    # the marker placed at the average of the provider's service coords.
    # The outer SELECT applies the Haversine radius filter against that
    # aggregated point.
    if include_services:
        params_svc: dict = {}

        if has_origin:
            params_svc["user_lat"] = lat
            params_svc["user_lng"] = lng
            params_svc["radius_km"] = radius_km

        # Inner query: collapse services by provider.
        # Provider display name mirrors `_provider_label()` in service.py
        # so map labels match what the rest of the app shows.
        inner_sql = """
            SELECT
                sv.provider_id                          AS id,
                COALESCE(
                    NULLIF(u.business_name, ''),
                    NULLIF(u.real_name, ''),
                    NULLIF(u.nickname, ''),
                    NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), ''),
                    'Service provider'
                )                                       AS name,
                AVG(sv.lat)                             AS lat,
                AVG(sv.lng)                             AS lng,
                'service'                               AS type,
                COUNT(*)                                AS service_count,
                MIN(sv.price)                           AS min_price,
                MAX(sv.price)                           AS max_price,
                MIN(sv.image_url)                       AS image_url,
                'available'                             AS status
            FROM services sv
            LEFT JOIN users u ON u.id = sv.provider_id
            WHERE sv.is_active = TRUE
              AND sv.lat IS NOT NULL
              AND sv.lng IS NOT NULL
            GROUP BY sv.provider_id,
                     u.business_name, u.real_name, u.nickname,
                     u.first_name, u.last_name
        """

        if has_origin:
            hav_outer = _HAVERSINE.format(lat_col='sub.lat', lng_col='sub.lng')
            service_sql = f"""
                SELECT sub.*, {hav_outer} AS distance_km
                FROM ({inner_sql}) sub
                WHERE {hav_outer} <= :radius_km
                ORDER BY distance_km ASC
                LIMIT 500
            """
        else:
            service_sql = f"""
                SELECT sub.*
                FROM ({inner_sql}) sub
                ORDER BY sub.name ASC
                LIMIT 500
            """

        service_rows = await database.fetch_all(service_sql, params_svc)
        results.extend([dict(r) for r in service_rows])

    # When both kinds returned, sort the merged list once more so
    # stores + services interleave correctly by distance.
    if has_origin:
        results.sort(
            key=lambda r: (
                r.get("distance_km") is None,
                r.get("distance_km") or 0,
            )
        )

    return results