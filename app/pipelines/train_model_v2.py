import pandas as pd
import pickle
from sklearn.linear_model import LogisticRegression

def train():
    df = pd.read_csv("enhanced_training_data.csv")
    print(f"Loaded {len(df)} examples")

    # Use all four features now
    X = df[["position", "distance", "minutes_since_listed", "title_quality"]]
    y = df["label"]

    model = LogisticRegression()
    model.fit(X, y)
    print("Accuracy on training data:", model.score(X, y))

    with open("ranking_model_v2.pkl", "wb") as f:
        pickle.dump(model, f)
    print("✅ Model saved as ranking_model_v2.pkl")

if __name__ == "__main__":
    train()