import pandas as pd
import random

def run():
    df = pd.read_csv("training_data.csv")
    if df.empty:
        print("No base training data found.")
        return

    # Add recency: higher chance of click for fresher items
    # We'll generate random minutes between 1 and 1440 (1 day)
    minutes_since_listed = []
    title_quality = []
    for _, row in df.iterrows():
        # For clicked items, bias toward lower minutes (fresher)
        if row["label"] == 1:
            minutes_since_listed.append(random.randint(1, 120))   # 0-2 hours
            # Hidden gems: sometimes low quality but still clicked
            title_quality.append(round(random.uniform(0.3, 1.0), 2))
        else:
            minutes_since_listed.append(random.randint(60, 1440)) # up to 1 day
            title_quality.append(round(random.uniform(0.5, 1.0), 2))

    df["minutes_since_listed"] = minutes_since_listed
    df["title_quality"] = title_quality

    df.to_csv("enhanced_training_data.csv", index=False)
    print(f"✅ Enhanced training data saved: {len(df)} examples")
    print(df.head())

if __name__ == "__main__":
    run()