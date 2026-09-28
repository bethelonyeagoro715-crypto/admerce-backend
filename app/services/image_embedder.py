import os
import json
import requests
from PIL import Image
import numpy as np

# Use HF Inference API for CLIP embeddings (free tier)
HF_TOKEN = os.getenv("HF_TOKEN", "")
HF_MODEL = "openai/clip-vit-base-patch32"

# Anything shorter than this is the color fallback, not a real embedding.
MIN_REAL_EMBEDDING_DIM = 32


def _fallback_from_bytes(image_bytes: bytes) -> list:
    """3-value average color. Only used when HF is unavailable."""
    try:
        from io import BytesIO
        img = Image.open(BytesIO(image_bytes)).convert("RGB")
        arr = np.array(img).reshape(-1, 3)
        avg = np.mean(arr, axis=0)
        norm = np.linalg.norm(avg)
        if norm > 0:
            avg = avg / norm
        return avg.tolist()
    except Exception:
        return [0.0, 0.0, 0.0]


def _fallback_embedding(image_path: str) -> list:
    """Kept for back-compat — reads path then delegates."""
    try:
        with open(image_path, "rb") as f:
            return _fallback_from_bytes(f.read())
    except Exception:
        return [0.0, 0.0, 0.0]


def _hf_embed(image_bytes: bytes) -> list:
    """Core HF call. Raises on any non-200; caller decides what to do."""
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    url = f"https://api-inference.huggingface.co/models/{HF_MODEL}"
    response = requests.post(url, headers=headers, data=image_bytes, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(f"HF {response.status_code}: {response.text[:200]}")
    result = response.json()
    if isinstance(result, dict) and "image_embeds" in result:
        emb = result["image_embeds"]
    elif isinstance(result, list):
        emb = result
    else:
        raise RuntimeError(f"Unexpected HF shape: {type(result).__name__}")

    # Normalize to unit length
    arr = np.array(emb, dtype=np.float32)
    norm = np.linalg.norm(arr)
    if norm > 0:
        arr = arr / norm
    return arr.tolist()


def image_bytes_to_embedding(image_bytes: bytes) -> list:
    """
    Generate a CLIP embedding from raw bytes — no temp file needed.
    Returns the fallback 3-dim vector if HF isn't configured or fails.
    """
    if not HF_TOKEN:
        print("⚠️ HF_TOKEN not set, using average-color fallback.")
        return _fallback_from_bytes(image_bytes)

    try:
        return _hf_embed(image_bytes)
    except Exception as e:
        print(f"⚠️ HF embedding failed: {e}")
        return _fallback_from_bytes(image_bytes)


def image_to_embedding(image_path: str) -> list:
    """Path-based entry point — used by the backfill script."""
    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()
    except Exception as e:
        print(f"⚠️ Could not read {image_path}: {e}")
        return [0.0, 0.0, 0.0]
    return image_bytes_to_embedding(image_bytes)


def embedding_to_json(embedding) -> str:
    return json.dumps(embedding)


def json_to_embedding(json_str: str) -> list:
    return json.loads(json_str)


def cosine_similarity(emb1, emb2):
    a = np.asarray(emb1, dtype=np.float32)
    b = np.asarray(emb2, dtype=np.float32)
    if a.shape != b.shape:
        # Mismatched dims (fallback vs real) — no similarity.
        return 0.0
    return float(np.dot(a, b))


def is_real_embedding(emb) -> bool:
    """True if the embedding looks like a real model output, not the fallback."""
    return bool(emb) and len(emb) >= MIN_REAL_EMBEDDING_DIM