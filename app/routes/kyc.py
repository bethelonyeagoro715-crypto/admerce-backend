from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from app.utils.security import get_current_user
from app.db.database import database
import uuid, os, shutil, random
import requests

router = APIRouter(prefix="/kyc", tags=["KYC"])

UPLOAD_DIR = "uploads/kyc"

# ---------- Simulated NIN lookup ----------
@router.post("/fetch-id")
async def fetch_id_details(
    national_id: str = Form(...),
    current_user: dict = Depends(get_current_user)
):
    if not national_id.isdigit() or len(national_id) < 5:
        raise HTTPException(status_code=400, detail="Invalid National ID number")

    details = {
        "surname": "Okonkwo",
        "first_name": "Chisom",
        "middle_name": "Nneka",
        "date_of_birth": "15/08/1995",
        "lga": "Yaba",
        "state_of_origin": "Lagos",
        "nationality": "Nigerian",
        "residence_address": "12 Aliu Street, Yaba"
    }
    return details

# ---------- Upload ID document and selfie ----------
@router.post("/upload-documents")
async def upload_documents(
    id_image: UploadFile = File(...),
    selfie_image: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    id_ext = os.path.splitext(id_image.filename)[1] or ".jpg"
    id_filename = f"{uuid.uuid4().hex}_id{id_ext}"
    id_path = os.path.join(UPLOAD_DIR, id_filename)
    with open(id_path, "wb") as f:
        shutil.copyfileobj(id_image.file, f)

    selfie_ext = os.path.splitext(selfie_image.filename)[1] or ".jpg"
    selfie_filename = f"{uuid.uuid4().hex}_selfie{selfie_ext}"
    selfie_path = os.path.join(UPLOAD_DIR, selfie_filename)
    with open(selfie_path, "wb") as f:
        shutil.copyfileobj(selfie_image.file, f)

    id_url = f"/uploads/kyc/{id_filename}"
    selfie_url = f"/uploads/kyc/{selfie_filename}"
    await database.execute(
        "UPDATE users SET id_document_url = :id_url, selfie_url = :selfie_url WHERE id = :uid",
        {"id_url": id_url, "selfie_url": selfie_url, "uid": current_user["id"]}
    )

    return {
        "id_document_url": id_url,
        "selfie_url": selfie_url,
        "message": "Documents uploaded"
    }

# ---------- NEW: Start active liveness (returns random actions) ----------
@router.post("/start-liveness")
async def start_liveness(current_user: dict = Depends(get_current_user)):
    # All possible actions
    actions_pool = ["BLINK", "MOUTH", "HEAD_LEFT", "HEAD_RIGHT", "HEAD_UP", "HEAD_DOWN"]
    # Pick 3 random actions (or however many you like)
    selected = random.sample(actions_pool, 3)
    return {
        "actions": selected,
        "message": "Perform these actions in front of the camera. Record a video and upload it."
    }

# ---------- NEW: Submit liveness video for verification ----------
@router.post("/liveness-video")
async def liveness_video(
    video: UploadFile = File(...),
    actions: str = Form(...),            # comma‑separated list, e.g. "BLINK,MOUTH,HEAD_LEFT"
    current_user: dict = Depends(get_current_user)
):
    # Save video temporarily
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    video_ext = os.path.splitext(video.filename)[1] or ".mp4"
    video_filename = f"{uuid.uuid4().hex}_liveness{video_ext}"
    video_path = os.path.join(UPLOAD_DIR, video_filename)
    with open(video_path, "wb") as f:
        shutil.copyfileobj(video.file, f)

    # Face++ Liveness API
    api_key = "M97dltO5kVe4BmhrE0hIYKlVqqM-5hG7"
    api_secret = "6TrG1cYvxrrgkXfr6XJ_e-hmtYRVtnq8"

    # Prepare action list
    action_list = [a.strip() for a in actions.split(",")]

    try:
        with open(video_path, "rb") as video_file:
            response = requests.post(
                "https://api-us.faceplusplus.com/facepp/v3/face/liveness",
                data={
                    "api_key": api_key,
                    "api_secret": api_secret,
                    "actions": action_list,      # Face++ expects a comma-separated string if sent as repeated param, but the SDK allows array. We'll pass it as a list.
                },
                files={"video_file": video_file}
            )
        result = response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Liveness request failed: {str(e)}")

    # Check if passed
    if result.get("result") != "passed":
        raise HTTPException(status_code=400, detail=f"Liveness check failed: {result.get('reason', 'unknown')}")

    # Mark user as liveness verified
    await database.execute(
        "UPDATE users SET liveness_verified = 1 WHERE id = :uid",
        {"uid": current_user["id"]}
    )

    # Clean up video (optional)
    os.remove(video_path)

    return {
        "message": "Liveness verification passed",
        "confidence": result.get("confidence", 0),
        "liveness_verified": True
    }

# ---------- UPDATED: Face verification (requires liveness first) ----------
@router.post("/verify-face")
async def verify_face(current_user: dict = Depends(get_current_user)):
    user = await database.fetch_one(
        "SELECT id_document_url, selfie_url, liveness_verified FROM users WHERE id = :uid",
        {"uid": current_user["id"]}
    )
    if not user or not user["id_document_url"] or not user["selfie_url"]:
        raise HTTPException(status_code=400, detail="Please upload documents first")
    if not user["liveness_verified"]:
        raise HTTPException(status_code=400, detail="You must pass active liveness check before face matching")

    api_key = "M97dltO5kVe4BmhrE0hIYKlVqqM-5hG7"
    api_secret = "6TrG1cYvxrrgkXfr6XJ_e-hmtYRVtnq8"

    base_dir = r"C:\Users\Bethel\SEAI PROJECT"
    id_path = os.path.join(base_dir, user["id_document_url"].lstrip("/"))
    selfie_path = os.path.join(base_dir, user["selfie_url"].lstrip("/"))

    try:
        response = requests.post(
            "https://api-us.faceplusplus.com/facepp/v3/compare",
            data={"api_key": api_key, "api_secret": api_secret},
            files={
                "image_file1": open(id_path, "rb"),
                "image_file2": open(selfie_path, "rb")
            }
        )
        result = response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Face++ request failed: {str(e)}")

    confidence = result.get("confidence", 0)
    if confidence < 80:
        raise HTTPException(status_code=400, detail=f"Face mismatch – confidence: {confidence:.1f}%")

    await database.execute(
        "UPDATE users SET kyc_verified = 1 WHERE id = :uid",
        {"uid": current_user["id"]}
    )

    return {
        "message": "Face verification successful",
        "confidence": confidence,
        "kyc_verified": True
    }

# ---------- Get KYC status ----------
@router.get("/status")
async def get_kyc_status(current_user: dict = Depends(get_current_user)):
    user = await database.fetch_one(
        "SELECT kyc_verified, first_name, last_name, national_id_number, liveness_verified FROM users WHERE id = :uid",
        {"uid": current_user["id"]}
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "kyc_verified": bool(user["kyc_verified"]),
        "liveness_verified": bool(user["liveness_verified"]),
        "first_name": user["first_name"],
        "last_name": user["last_name"],
        "national_id_number": user["national_id_number"]
    }