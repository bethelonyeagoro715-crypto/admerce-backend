import torch
import torchvision.models as models
import torchvision.transforms as transforms
from PIL import Image
import numpy as np
import json

# Load a pretrained ResNet-18 (cached locally, no internet needed after first run)
model = models.resnet18(pretrained=True)
model.eval()   # set to inference mode

# Remove the final classification layer to get feature embeddings
embedding_model = torch.nn.Sequential(*list(model.children())[:-1])

# Standard preprocessing for ResNet
preprocess = transforms.Compose([
    transforms.Resize(256),
    transforms.CenterCrop(224),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
])

def image_to_embedding(image_path: str) -> list:
    """Convert an image to a 512‑dimension embedding."""
    image = Image.open(image_path).convert("RGB")
    input_tensor = preprocess(image).unsqueeze(0)   # shape (1, 3, 224, 224)
    with torch.no_grad():
        features = embedding_model(input_tensor).squeeze().cpu().numpy()   # shape (512,)
    # Normalize to unit length for cosine similarity
    norm = np.linalg.norm(features)
    if norm > 0:
        features = features / norm
    return features.tolist()

def embedding_to_json(embedding: list) -> str:
    return json.dumps(embedding)

def json_to_embedding(json_str: str) -> list:
    return json.loads(json_str)

def cosine_similarity(emb1, emb2):
    return np.dot(emb1, emb2)   # already normalized