import os
import cloudinary
import cloudinary.uploader

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

def upload_image(file_bytes, folder="admerce"):
    """Upload image bytes to Cloudinary and return the secure URL."""
    result = cloudinary.uploader.upload(file_bytes, folder=folder)
    return result["secure_url"]

def upload_video(file_bytes, folder="admerce"):
    """Upload video bytes to Cloudinary and return the secure URL."""
    result = cloudinary.uploader.upload(file_bytes, resource_type="video", folder=folder)
    return result["secure_url"]