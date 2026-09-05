import pickle
import requests
from math import radians, cos, sin, asin, sqrt

# ---------- Ranking model ----------
model = None
try:
    with open("ranking_model_v2.pkl", "rb") as f:
        model = pickle.load(f)
    print("✅ Ranking model v2 loaded")
except FileNotFoundError:
    print("⚠️ No ranking model found (run train_model_v2.py first)")

# ---------- Distance calculator ----------
def haversine(lat1, lon1, lat2, lon2):
    """Return distance in km between two coordinates."""
    R = 6371
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return R * 2 * asin(sqrt(a))


import requests

# Simple in-memory cache – stores results for each (lat,lng) pair
_geocode_cache = {}

def reverse_geocode(lat: float, lng: float) -> str:
    """Return a short address like 'Yaba, Lagos' for the given coordinates, with caching."""
    key = (round(lat, 5), round(lng, 5))   # round to avoid slight GPS variations
    if key in _geocode_cache:
        return _geocode_cache[key]

    try:
        url = "https://nominatim.openstreetmap.org/reverse"
        params = {
            "lat": lat,
            "lon": lng,
            "format": "json",
            "zoom": 16,
            "addressdetails": 1,
        }
        headers = {"User-Agent": "AdmerceApp/1.0"}
        resp = requests.get(url, params=params, headers=headers, timeout=5)
        data = resp.json()
        address = data.get("address", {})
        parts = []
        for key_name in ("neighbourhood", "suburb", "city", "town", "state"):
            if key_name in address:
                parts.append(address[key_name])
        if not parts:
            parts.append(address.get("county", ""))
        result = ", ".join(parts[:2]) if parts else "Unknown location"
    except Exception:
        result = "Unknown location"

    _geocode_cache[key] = result
    return result