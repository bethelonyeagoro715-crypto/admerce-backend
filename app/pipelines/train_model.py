import pandas as pd
import pickle
from sklearn.linear_model import LogisticRegression

def train():
    # Load the training data we made earlier
    df = pd.read_csv("training_data.csv")
    print(f"Loaded {len(df)} examples")

    # Features: what the model learns from
    X = df[["position", "distance"]]

    # Label: 1 = user interacted, 0 = user ignored
    y = df["label"]

    # Train a simple logistic regression model
    model = LogisticRegression()
    model.fit(X, y)

    print("Model trained! Accuracy on training data:", model.score(X, y))

    # Save the model to a file
    with open("ranking_model.pkl", "wb") as f:
        pickle.dump(model, f)
    print("✅ Model saved as ranking_model.pkl")

if __name__ == "__main__":
    train()