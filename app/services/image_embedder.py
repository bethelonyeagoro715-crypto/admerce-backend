import os
import json
import requests
from PIL import Image
import numpy as np

# Use HF Inference API for CLIP embeddings (free tier)
HF_TOKEN = os.getenv("HF_TOKEN", "")
HF_MODEL = "openai/clip-vit-base-patch32"

def image_to_embedding(image_path: str) -> list:
    """Convert an image to an embedding using CLIP via Hugging Face Inference API."""
    if not HF_TOKEN:
        print("⚠️ HF_TOKEN not set, using average color as placeholder embedding.")
        return _fallback_embedding(image_path)

    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()
        headers = {"Authorization": f"Bearer {HF_TOKEN}"}
        url = f"https://api-inference.huggingface.co/models/{HF_MODEL}"
        response = requests.post(url, headers=headers, data=image_bytes, timeout=30)
        if response.status_code == 200:
            result = response.json()
            # CLIP returns a list of floats under 'image_embeds' (or similar)
            # For simplicity, we'll return the entire JSON; adjust later if needed.
            # Actually better to extract the vector:
            if isinstance(result, dict) and "image_embeds" in result:
                emb = result["image_embeds"]
            elif isinstance(result, list):
                emb = result
            else:
                emb = []
            # Normalize if possible
            if emb:
                norm = np.linalg.norm(emb)
                if norm > 0:
                    emb = (np.array(emb) / norm).tolist()
            return emb
        else:
            print(f"⚠️ HF error: {response.text}")
            return _fallback_embedding(image_path)
    except Exception as e:
        print(f"⚠️ HF request failed: {e}")
        return _fallback_embedding(image_path)

def _fallback_embedding(image_path: str) -> list:
    """Generate a simple 3‑value embedding from average color."""
    try:
        img = Image.open(image_path).convert("RGB")
        arr = np.array(img).reshape(-1, 3)
        avg = np.mean(arr, axis=0)
        # Normalize to unit length
        norm = np.linalg.norm(avg)
        if norm > 0:
            avg = avg / norm
        return avg.tolist()
    except Exception:
        return [0.0, 0.0, 0.0]

def embedding_to_json(embedding) -> str:
    return json.dumps(embedding)

def json_to_embedding(json_str: str) -> list:
    return json.loads(json_str)

def cosine_similarity(emb1, emb2):
    return np.dot(emb1, emb2)