# Valid item category IDs (from frontend)
ITEM_CATEGORY_IDS = [
    'tech_electronics',
    'food_beverage',
    'health_wellness',
    'fashion_apparel',
    'building_industrial',
    'home_garden',
    'kids_toys',
    'sports_outdoors',
    'automotive',
    'media_office',
]

# Valid service category IDs
SERVICE_CATEGORY_IDS = [
    'grooming_beauty',
    'repair_maintenance',
    'cleaning_care',
]

def validate_item_category(category_id: str) -> bool:
    return category_id in ITEM_CATEGORY_IDS

def validate_service_category(category_id: str) -> bool:
    return category_id in SERVICE_CATEGORY_IDS