import sqlite3
import json
import pandas as pd
from math import radians, cos, sin, asin, sqrt

def _compute_distance(loc1_json, loc2_json):
    if not loc1_json or not loc2_json:
        return None
    if isinstance(loc1_json, str):
        loc1 = json.loads(loc1_json)
    else:
        loc1 = loc1_json
    if isinstance(loc2_json, str):
        loc2 = json.loads(loc2_json)
    else:
        loc2 = loc2_json
    lat1, lon1 = loc1["lat"], loc1["lng"]
    lat2, lon2 = loc2["lat"], loc2["lng"]
    R = 6371
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    return R * c

def run():
    conn = sqlite3.connect("seai.db")
    events = pd.read_sql_query("SELECT * FROM events ORDER BY timestamp", conn)
    if events.empty:
        print("No events yet. Add some data first.")
        return

    training_examples = []
    for (user_id, session_id), session_events in events.groupby(["user_id", "session_id"]):
        impressions = session_events[session_events["event_type"] == "impression"]
        actions = session_events[session_events["event_type"].isin(["click", "reserve", "save"])]

        for _, imp in impressions.iterrows():
            label = 0
            if imp["listing_id"] is not None:
                interacted = actions[actions["listing_id"] == imp["listing_id"]]
                if not interacted.empty:
                    label = 1
            training_examples.append({
                "user_id": user_id,
                "listing_id": imp["listing_id"],
                "position": imp["position"],
                "distance": _compute_distance(imp["user_location"], imp["listing_location"]),
                "label": label,
                "timestamp": imp["timestamp"]
            })

    if training_examples:
        train_df = pd.DataFrame(training_examples)
        train_df.to_csv("training_data.csv", index=False)
        print(f"✅ Training data saved: {len(train_df)} examples")
    else:
        print("No training examples generated. Add more events (impressions + clicks).")
    conn.close()

if __name__ == "__main__":
    run()