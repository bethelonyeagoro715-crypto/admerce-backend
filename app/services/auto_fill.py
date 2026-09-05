# app/services/auto_fill.py
import requests
import json

def lookup_barcode(barcode: str) -> dict:
    """
    Look up product information from a barcode.
    Uses the free Open Food Facts API as a demo.
    Returns a dict with title, category, and image_url, or empty dict if not found.
    """
    url = f"https://world.openfoodfacts.org/api/v0/product/{barcode}.json"
    try:
        resp = requests.get(url, timeout=5)
        data = resp.json()
        if data.get("status") == 1:
            product = data["product"]
            return {
                "title": product.get("product_name", ""),
                "category": product.get("categories", ""),
                "image_url": product.get("image_url", "")
            }
    except Exception:
        pass
    return {}

def suggest_from_barcode(barcode: str) -> dict:
    """
    Returns a dictionary with suggested title and category based on barcode.
    """
    info = lookup_barcode(barcode)
    if info.get("title"):
        return {
            "suggested_title": info["title"],
            "suggested_category": info["category"],
            "source": "barcode"
        }
    else:
        # Fallback if barcode not found
        return {
            "suggested_title": "",
            "suggested_category": "",
            "source": "none"
        }