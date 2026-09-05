import json
import os
from app.db.database import database
from app.utils import haversine
from groq import Groq

groq_client = os.getenv("GROQ_API_KEY")

class MarketGapService:
    
    @staticmethod
    async def analyze(
        lat: float,
        lng: float,
        category: str,
        radius_km: float = 10.0
    ) -> dict:
        """
        Analyze market gap for a specific category in a given location.
        Returns: existing count, demand score, recommendation, competitors, AI insight.
        """
        # 1. Count existing businesses in this category within radius
        existing_rows = await database.fetch_all(
            """
            SELECT id, name, rating, reviews_count,
                (6371 * acos(
                    cos(radians(:lat)) * cos(radians(lat)) *
                    cos(radians(lng) - radians(:lng)) +
                    sin(radians(:lat)) * sin(radians(lat))
                )) AS distance_km
            FROM businesses
            WHERE category = :category
            HAVING distance_km < :radius_km
            ORDER BY rating DESC, reviews_count DESC
            LIMIT 5
            """,
            {"lat": lat, "lng": lng, "category": category, "radius_km": radius_km}
        )
        
        existing_count = len(existing_rows)
        competitors = [dict(row) for row in existing_rows]
        
        # 2. Estimate Demand Score (0-100)
        # We use a heuristic: universal high-demand categories get a baseline boost.
        # In production, this would be driven by search volume, population density, etc.
        high_demand_categories = [
            "Plumbing", "Electrical", "Lawyer", "Doctor", "Dentist",
            "Restaurant", "Cafe", "Barber", "Hairdresser", "Tutoring",
            "IT Support", "Car Repair", "Cleaning", "Pharmacy"
        ]
        
        base_demand = 50
        if category in high_demand_categories:
            base_demand = 75
        elif category in ["Luxury Goods", "Fine Dining", "Consulting"]:
            base_demand = 40
        else:
            base_demand = 55
            
        # Adjust demand based on supply (if there are many, demand is partially satisfied)
        supply_factor = min(1.0, existing_count / 10.0)  # 10 businesses = saturated
        demand_score = int(base_demand + (1 - supply_factor) * 30)
        demand_score = max(0, min(100, demand_score))  # Clamp 0-100
        
        # 3. Recommendation
        if existing_count < 3 and demand_score > 70:
            recommendation = "High"
        elif existing_count < 6 and demand_score > 50:
            recommendation = "Medium"
        else:
            recommendation = "Low"
        
        # 4. AI Insight Generation
        insight = await MarketGapService._generate_insight(
            category=category,
            location=await MarketGapService._get_location_name(lat, lng),
            existing_count=existing_count,
            demand_score=demand_score,
            competitors=competitors,
            radius_km=radius_km
        )
        
        return {
            "category": category,
            "existing_businesses": existing_count,
            "demand_score": demand_score,
            "recommendation": recommendation,
            "top_competitors": competitors,
            "insight": insight
        }
    
    @staticmethod
    async def _generate_insight(
        category: str,
        location: str,
        existing_count: int,
        demand_score: int,
        competitors: list,
        radius_km: float
    ) -> str:
        """
        Use Groq AI to generate a human-readable market insight.
        """
        prompt = f"""
        You are a business analyst for Admerce. Analyze the market for "{category}" businesses.
        
        Location: {location}
        Existing competitors within {radius_km}km: {existing_count}
        Estimated demand score (0-100): {demand_score}
        
        Top competitors:
        {json.dumps(competitors, indent=2)}
        
        Provide a 2-3 sentence insight:
        - If demand is high and competition is low, encourage the user to start this business.
        - If competition is high, suggest a niche or differentiation strategy.
        - Be specific and actionable.
        """
        
        try:
            response = groq_client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=200
            )
            return response.choices[0].message.content
        except Exception as e:
            # Fallback if Groq fails
            if existing_count < 3 and demand_score > 70:
                return f"🔥 High opportunity! Only {existing_count} {category} businesses nearby. Demand is strong. You could fill this gap."
            elif existing_count < 6 and demand_score > 50:
                return f"📈 Moderate opportunity. {existing_count} competitors exist, but demand is healthy. Consider a niche within {category}."
            else:
                return f"📊 High competition ({existing_count} businesses). Consider differentiating with better pricing, service, or a specialized sub-category."

    @staticmethod
    async def _get_location_name(lat: float, lng: float) -> str:
        """
        Simple reverse geocoding using geopy (or fallback to coordinates).
        """
        try:
            from geopy.geocoders import Nominatim
            import time
            geolocator = Nominatim(user_agent="admerce_market_analysis")
            time.sleep(0.5)
            location = geolocator.reverse(f"{lat}, {lng}", exactly_one=True, language='en')
            if location and location.raw:
                address = location.raw.get('address', {})
                parts = []
                if 'city' in address:
                    parts.append(address['city'])
                elif 'town' in address:
                    parts.append(address['town'])
                if 'state' in address:
                    parts.append(address['state'])
                if 'country' in address:
                    parts.append(address['country'])
                if parts:
                    return ', '.join(parts)
            return "your area"
        except:
            return f"({lat:.4f}, {lng:.4f})"