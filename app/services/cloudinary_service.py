import os
import cloudinary
import cloudinary.uploader

# ── Build config only from keys that actually exist ──────────────────────
# The Python Cloudinary SDK auto-reads CLOUDINARY_URL at import. If you then
# call cloudinary.config(api_key=None, ...), you WIPE that valid config and
# get "ValueError: Must supply api_key". This guard prevents that.
_cfg = {}
if os.getenv("CLOUDINARY_CLOUD_NAME"):
    _cfg["cloud_name"] = os.getenv("CLOUDINARY_CLOUD_NAME")
if os.getenv("CLOUDINARY_API_KEY"):
    _cfg["api_key"] = os.getenv("CLOUDINARY_API_KEY")
if os.getenv("CLOUDINARY_API_SECRET"):
    _cfg["api_secret"] = os.getenv("CLOUDINARY_API_SECRET")

if _cfg:
    _cfg["secure"] = True
    cloudinary.config(**_cfg)
    print("✅ Cloudinary configured:", sorted(_cfg.keys()), flush=True)
else:
    print("⚠️  Cloudinary env vars missing – uploads will fail", flush=True)


def upload_image(file_bytes, folder="admerce"):
    """Upload image bytes to Cloudinary and return the secure URL."""
    result = cloudinary.uploader.upload(file_bytes, folder=folder)
    return result["secure_url"]


def upload_video(file_bytes, folder="admerce"):
    """Upload video bytes to Cloudinary and return the secure URL."""
    result = cloudinary.uploader.upload(
        file_bytes, resource_type="video", folder=folder
    )
    return result["secure_url"]