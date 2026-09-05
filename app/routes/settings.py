from fastapi import APIRouter, HTTPException, Depends, Query
from app.db.database import database
from app.utils.security import get_current_admin

router = APIRouter(prefix="/settings", tags=["Settings"])

@router.get("/feature-flags")
async def get_feature_flags():
    """Get all feature flags from the database."""
    rows = await database.fetch_all(
        "SELECT key, value FROM app_settings WHERE key IN "
        "('enable_delivery', 'enable_courier', 'enable_flipper', 'maintenance_mode', 'app_version')"
    )
    flags = {row["key"]: row["value"] for row in rows}

    return {
        "enable_delivery": flags.get("enable_delivery", "false") == "true",
        "enable_courier": flags.get("enable_courier", "false") == "true",
        "enable_flipper": flags.get("enable_flipper", "false") == "true",
        "maintenance_mode": flags.get("maintenance_mode", "false") == "true",
        "app_version": flags.get("app_version", "1.0.0"),
    }

@router.post("/admin/update-setting")
async def update_setting(
    key: str = Query(...),
    value: str = Query(...),
    current_user: dict = Depends(get_current_admin)
):
    """Update a setting (admin only)."""
    valid_keys = [
        "enable_delivery",
        "enable_courier",
        "enable_flipper",
        "maintenance_mode",
        "app_version",
    ]
    if key not in valid_keys:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid key. Must be one of {valid_keys}"
        )

    # Check if the setting already exists
    existing = await database.fetch_one(
        "SELECT key FROM app_settings WHERE key = :key",
        {"key": key}
    )

    if existing:
        # Update existing row
        await database.execute(
            "UPDATE app_settings SET value = :value, updated_at = CURRENT_TIMESTAMP WHERE key = :key",
            {"key": key, "value": value}
        )
    else:
        # Insert new row
        await database.execute(
            "INSERT INTO app_settings (key, value) VALUES (:key, :value)",
            {"key": key, "value": value}
        )

    return {"message": f"Setting '{key}' updated to '{value}'"}