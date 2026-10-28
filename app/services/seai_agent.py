# app/services/seai_agent.py
import re
from typing import Dict, Any, Tuple, Optional
from app.db.database import database
from app.utils import haversine
from app.routes.shopper import (
    _geo_recall,
    _forage_recall,
    _trending_recall,
    _following_recall,
)

# ... (keep INTENT_PATTERNS, STOP_WORDS, _clean_query, classify_intent, _tokenize, _expand_category_terms, _travel_minutes, _directions_url exactly as they are) ...

# ... (keep _query_relevant_items, _service_recall, _store_recall exactly as they are) ...

# ... (keep handle_search_items, handle_book_service, handle_get_store_info exactly as they are) ...

# 🚨 NEW: The AI Tool for Optimization
async def handle_optimize_cart(params: dict, lat: float, lng: float, user_id: str = None) -> dict:
    items = params.get("items", [])
    budget = params.get("budget")
    
    if not items or not budget:
        return {"type": "text", "text": "I need a list of items and a budget to optimize your cart."}

    # We call the logic directly from shopper.py
    from app.routes.shopper import OptimizeRequest, optimize_cart
    
    try:
        req = OptimizeRequest(
            items=items,
            budget=float(budget),
            lat=lat,
            lng=lng,
            radius_km=50.0
        )
        # Simulate the current user if needed (optimize_cart is optional auth)
        user = {"id": user_id} if user_id else None
        
        result = await optimize_cart(req, user)
        
        if result["status"] == "success":
            decision = result["decision"]
            items_str = ", ".join(decision["items"])
            return {
                "type": "text",
                "text": (
                    f"Based on your budget of ₦{budget:,.0f}, the best option is to go to "
                    f"**{decision['store_name']}**. You can get {items_str} "
                    f"for a total of ₦{decision['total']:,.0f}. "
                    f"This leaves you with ₦{result['savings']:,.0f} to spare."
                )
            }
        elif result["status"] == "over_budget":
            return {
                "type": "text",
                "text": f"Even the cheapest options exceed your budget. You would need at least ₦{result['total_needed']:,.0f}."
            }
        else:
            return {
                "type": "text",
                "text": result.get("message", "I couldn't find all the items nearby.")
            }
            
    except Exception as e:
        print(f"⚠️ Optimize failed: {e}")
        return {"type": "text", "text": "I had trouble optimizing your cart right now."}

async def _embedding_recall(user_id, lat, lng, radius_km, limit):
    return []

async def _collab_recall(user_id, lat, lng, radius_km, limit):
    return []