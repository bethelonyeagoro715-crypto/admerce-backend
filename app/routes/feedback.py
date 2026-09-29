"""
Feedback and problem reports.
"""
from fastapi import APIRouter, HTTPException, Depends, File, UploadFile, Form
from typing import Optional
import uuid
from datetime import datetime

from app.db.database import database
from app.utils.security import get_current_user
from app.services.cloudinary_service import upload_image


router = APIRouter(prefix="/feedback", tags=["Feedback"])


@router.post("/report", status_code=201)
async def submit_report(
    category: str = Form(...),
    area: str = Form(...),
    description: str = Form(...),
    image: Optional[UploadFile] = File(None),
    current_user: dict = Depends(get_current_user),
):
    if not category.strip():
        raise HTTPException(status_code=400, detail="Category is required")
    if not area.strip():
        raise HTTPException(status_code=400, detail="Area is required")
    desc = description.strip()
    if len(desc) < 10:
        raise HTTPException(status_code=400, detail="Description must be at least 10 characters")
    if len(desc) > 1000:
        raise HTTPException(status_code=400, detail="Description max 1000 characters")

    image_url = None
    if image and image.filename:
        try:
            image_bytes = await image.read()
            if image_bytes:
                image_url = upload_image(image_bytes, folder="reports")
        except Exception as e:
            print(f"⚠️  report image upload failed: {e}")

    report_id = str(uuid.uuid4())
    now = datetime.utcnow()
    await database.execute(
        """
        INSERT INTO feedback (
            id, user_id, type, category, area, description, image_url,
            status, created_at
        ) VALUES (
            :id, :uid, 'report', :cat, :area, :desc, :img, 'open', :now
        )
        """,
        {
            "id": report_id,
            "uid": current_user["id"],
            "cat": category.strip(),
            "area": area.strip(),
            "desc": desc,
            "img": image_url,
            "now": now,
        },
    )
    return {
        "id": report_id,
        "category": category,
        "status": "open",
        "created_at": now,
    }


@router.get("/my-reports")
async def my_reports(current_user: dict = Depends(get_current_user)):
    rows = await database.fetch_all(
        """
        SELECT id, category, area, description, image_url, status, created_at
        FROM feedback
        WHERE user_id = :uid AND type = 'report'
        ORDER BY created_at DESC
        LIMIT 50
        """,
        {"uid": current_user["id"]},
    )
    return [dict(r) for r in rows]


@router.post("")
@router.post("/")
async def submit_feedback(
    rating: int = Form(...),
    kind: str = Form(...),
    comment: Optional[str] = Form(None),
    current_user: dict = Depends(get_current_user),
):
    if rating < 1 or rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 5")
    if not kind.strip():
        raise HTTPException(status_code=400, detail="Kind is required")
    c = (comment or "").strip() or None
    if c and len(c) > 800:
        raise HTTPException(status_code=400, detail="Comment max 800 characters")

    fb_id = str(uuid.uuid4())
    now = datetime.utcnow()
    await database.execute(
        """
        INSERT INTO feedback (id, user_id, type, rating, kind, comment, status, created_at)
        VALUES (:id, :uid, 'feedback', :rating, :kind, :comment, 'open', :now)
        """,
        {
            "id": fb_id,
            "uid": current_user["id"],
            "rating": rating,
            "kind": kind.strip(),
            "comment": c,
            "now": now,
        },
    )
    return {"id": fb_id, "message": "Thanks for your feedback"}