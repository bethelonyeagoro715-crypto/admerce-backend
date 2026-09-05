import requests
import random

base_url = "http://127.0.0.1:8000/seai/events"

# Simulate 10 sessions
for session_num in range(1, 11):
    user = f"user_{session_num:02d}"
    session = f"sess_{session_num:02d}"
    # Create a random listing location near the user
    lat = 6.5244 + random.uniform(-0.01, 0.01)
    lng = 3.3792 + random.uniform(-0.01, 0.01)
    distance = round(random.uniform(0.1, 5.0), 2)
    # Simulate feed position (1-20)
    position = random.randint(1, 20)

    # Impression event
    impression = {
        "event_type": "impression",
        "user_id": user,
        "session_id": session,
        "listing_id": f"list_{session_num}",
        "user_location": {"lat": 6.5244, "lng": 3.3792},
        "listing_location": {"lat": lat, "lng": lng},
        "position": position
    }
    r = requests.post(base_url, json=impression)
    print(f"Impression {session_num}: {r.json()}")

    # Decide randomly if the user clicks (with probability 0.7 if position < 5, else 0.2)
    if position < 5:
        prob_click = 0.7
    else:
        prob_click = 0.2

    if random.random() < prob_click:
        click_event = {
            "event_type": "click",
            "user_id": user,
            "session_id": session,
            "listing_id": f"list_{session_num}",
            "user_location": {"lat": 6.5244, "lng": 3.3792},
            "listing_location": {"lat": lat, "lng": lng}
        }
        r = requests.post(base_url, json=click_event)
        print(f"Click {session_num}: {r.json()}")

print("✅ Sample data generated.")