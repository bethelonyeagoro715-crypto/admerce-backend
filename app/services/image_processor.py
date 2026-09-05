import io, random, os
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageEnhance
from rembg import remove, new_session

# ------------------------------------------------------------
# Force CPU so we never run out of GPU memory
# ------------------------------------------------------------
os.environ["CUDA_VISIBLE_DEVICES"] = ""          # hide any GPU
os.environ["PYTORCH_NO_CUDA_MEMORY"] = "1"

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
BACKGROUND_MODEL = "isnet-general-use"   # best for products
ENABLE_SUPER_RES = False                  # keep False – too slow on CPU

# Lazy‑load the rembg session (speeds up subsequent calls)
_session = None
def _get_session():
    global _session
    if _session is None:
        _session = new_session(BACKGROUND_MODEL)
    return _session

# ------------------------------------------------------------
# Public API
# ------------------------------------------------------------
def process_image(image_bytes: bytes, style: str = "warm") -> bytes:
    """
    Main entry point called by the FastAPI endpoint.
    style = 'warm'  → Pinterest aesthetic
    style = 'studio'→ photolab composite
    style = 'clean' → background removal + auto‑enhance (white bg)
    """
    if style == "studio":
        return _process_photolab(image_bytes)
    elif style == "clean":
        return _process_clean(image_bytes)
    return _process_warm(image_bytes)

# ------------------------------------------------------------
# 1. Warm Pinterest‑style (improved)
# ------------------------------------------------------------
def _process_warm(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img = _auto_enhance(img)
    enhancer = ImageEnhance.Color(img)
    img = enhancer.enhance(1.2)
    r, g, b = img.split()
    r = r.point(lambda i: min(255, i * 1.08))
    img = Image.merge("RGB", (r, g, b))
    img = _add_vignette(img, intensity=0.25)
    img = _add_grain(img, strength=5)
    output = io.BytesIO()
    img.save(output, format="PNG")
    return output.getvalue()

# ------------------------------------------------------------
# 2. Clean (background removal + enhancement)
# ------------------------------------------------------------
def _process_clean(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    # Remove background using the best model (CPU)
    output_data = remove(image_bytes, session=_get_session())
    obj = Image.open(io.BytesIO(output_data)).convert("RGBA")
    obj = _auto_enhance(obj.convert("RGB")).convert("RGBA")
    obj = _feather_edges(obj, radius=1.2)
    # Place on pure white
    bg = Image.new("RGBA", obj.size, (255, 255, 255, 255))
    bg.paste(obj, (0, 0), obj)
    if ENABLE_SUPER_RES:
        bg = _super_resolve(bg, scale=2)
    output = io.BytesIO()
    bg.convert("RGB").save(output, format="PNG", quality=95)
    return output.getvalue()

# ------------------------------------------------------------
# 3. Studio photolab (your original creative pipeline)
# ------------------------------------------------------------
def _process_photolab(image_bytes: bytes) -> bytes:
    # Remove background
    output_data = remove(image_bytes, session=_get_session())
    obj = Image.open(io.BytesIO(output_data)).convert("RGBA")
    # Enhance
    obj = _auto_enhance(obj.convert("RGB")).convert("RGBA")
    obj = _feather_edges(obj, radius=1.5)

    # Super resolution (disabled – too heavy for CPU)
    if ENABLE_SUPER_RES:
        obj = _super_resolve(obj, scale=2)

    # Generate premium surface
    width, height = 1920, 1080
    surface = random.choice(["marble", "metal", "velvet", "wood", "neon", "pastel"])
    bg = _make_background(width, height, surface)

    # Resize object (45% of canvas)
    base_width = int(width * 0.45)
    w_percent = base_width / float(obj.size[0])
    h_size = int(float(obj.size[1]) * float(w_percent))
    obj = obj.resize((base_width, h_size), Image.Resampling.LANCZOS)

    # Floor reflection
    reflection = obj.copy().transpose(Image.FLIP_TOP_BOTTOM)
    reflection = reflection.crop((0, 0, reflection.width, int(reflection.height * 0.4)))
    alpha = reflection.split()[3]
    alpha = ImageEnhance.Brightness(alpha).enhance(0.25)
    reflection.putalpha(alpha)

    # Drop shadow
    mask = obj.split()[3]
    shadow = Image.new("RGBA", obj.size, (0, 0, 0, 0))
    shadow.putalpha(mask)
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=25))

    # Position
    obj_x = (width - base_width) // 2
    obj_y = (height - h_size) // 2 - 60
    ref_y = obj_y + h_size + 10
    shadow_x, shadow_y = obj_x + 20, obj_y + 40

    # Composite
    bg.paste(reflection, (obj_x, ref_y), reflection)
    bg.paste(shadow, (shadow_x, shadow_y), shadow)
    bg.paste(obj, (obj_x, obj_y), obj)

    # Lighting & vignette
    bg = _add_lighting(bg)
    bg = _add_vignette(bg, intensity=0.3)

    output = io.BytesIO()
    bg.convert("RGB").save(output, format="PNG", quality=95)
    return output.getvalue()

# ------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------
def _auto_enhance(img):
    arr = np.array(img, dtype=np.float32)
    for c in range(3):
        low, high = np.percentile(arr[:,:,c], (2, 98))
        arr[:,:,c] = np.clip((arr[:,:,c] - low) * (255.0 / (high - low + 1e-8)), 0, 255)
    img = Image.fromarray(arr.astype(np.uint8))
    return ImageEnhance.Contrast(img).enhance(1.1)

def _feather_edges(img, radius=1.0):
    alpha = img.getchannel("A")
    alpha = alpha.filter(ImageFilter.GaussianBlur(radius=radius))
    img.putalpha(alpha)
    return img

def _super_resolve(img, scale=2):
    try:
        import importlib
        realesrgan = importlib.import_module("realesrgan")
        module = importlib.import_module("basicsr.archs.rrdbnet_arch")
        RRDBNet = module.RRDBNet
        model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32, scale=scale)
        upscaler = realesrgan.RealESRGANer(scale=scale, model_path=None, model=model, device='cpu')
        img_np = np.array(img)
        output, _ = upscaler.enhance(img_np, outscale=scale)
        return Image.fromarray(output)
    except ImportError:
        w, h = img.size
        return img.resize((w*scale, h*scale), Image.Resampling.LANCZOS)

def _add_vignette(img, intensity=0.3):
    width, height = img.size
    X, Y = np.meshgrid(np.arange(width), np.arange(height))
    center_x, center_y = width // 2, height // 2
    max_dist = np.sqrt(center_x**2 + center_y**2)
    dist = np.sqrt((X - center_x)**2 + (Y - center_y)**2)
    vignette = 1 - intensity * (dist / max_dist)
    vignette = np.clip(vignette, 0.6, 1.0)
    arr = np.array(img, dtype=np.float32)
    for c in range(3):
        arr[:,:,c] *= vignette
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

def _add_grain(img, strength=5):
    arr = np.array(img, dtype=np.float32)
    noise = np.random.normal(0, strength, arr.shape)
    arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
    return Image.fromarray(arr)

# Background surfaces (unchanged)
def _make_background(w, h, style):
    bg = Image.new("RGB", (w, h))
    pixels = np.array(bg, dtype=np.float32)
    if style == "marble":
        for i in range(h):
            c = 210 + 15 * np.sin(i * 0.02) + 10 * np.random.randn()
            pixels[i, :] = [c, c, c + 5]
    elif style == "metal":
        for i in range(w):
            c = 180 + 20 * np.sin(i * 0.03)
            pixels[:, i] = [c, c, c - 5]
    elif style == "velvet":
        pixels[:, :] = [70, 30, 50]
        pixels += np.random.randint(-10, 10, (h, w, 3))
    elif style == "wood":
        for i in range(h):
            c = 140 + 10 * np.sin(i * 0.05)
            pixels[i, :] = [c + 30, c, c - 20]
    elif style == "neon":
        c1 = np.random.randint(100, 255)
        c2 = np.random.randint(100, 255)
        for i in range(h):
            t = i / h
            r = int(c1 * t + c2 * (1 - t))
            g = int(c1 * (1 - t) + c2 * t)
            b = int(150 + 50 * np.sin(t * 10))
            pixels[i, :] = [r, g, b]
    elif style == "pastel":
        r_base = random.randint(200, 255)
        g_base = random.randint(200, 255)
        b_base = random.randint(200, 255)
        pixels[:, :] = [r_base, g_base, b_base]
        pixels += np.random.randint(-15, 15, (h, w, 3))
    return Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))

def _add_lighting(img):
    width, height = img.size
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for i in range(300):
        alpha = int(50 * (1 - i / 300))
        draw.ellipse([width//2-200-i, -100-i, width//2+200+i, 200+i], fill=(255,255,200, alpha))
    draw.ellipse([-100, height//2-200, 200, height//2+200], fill=(255,100,100, 20))
    draw.ellipse([width-100, height//2-200, width+200, height//2+200], fill=(100,100,255, 20))
    img = img.convert("RGBA")
    img = Image.alpha_composite(img, overlay)
    return img.convert("RGB")
 