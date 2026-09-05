from fastapi import APIRouter, UploadFile, File, HTTPException
from app.services.transcription_service import transcribe_audio
import os, uuid

router = APIRouter(prefix="/seai", tags=["SEAI Transcribe"])

@router.post("/transcribe")
async def transcribe_search_audio(audio: UploadFile = File(...)):
    # Save temp audio
    tmp_dir = "uploads/tmp"
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, f"{uuid.uuid4().hex}.webm")
    with open(tmp_path, "wb") as f:
        f.write(await audio.read())

    try:
        text = transcribe_audio(tmp_path)
    except Exception as e:
        os.remove(tmp_path)
        raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")
    os.remove(tmp_path)
    return {"text": text}