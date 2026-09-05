from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db.database import database
import uuid
from app.routes.auth import create_access_token

router = APIRouter(prefix="/auth", tags=["Auth Social"])

class SocialLoginRequest(BaseModel):
    provider: str          # 'google', 'facebook', 'twitter'
    token: str
    secret: str = None

@router.post("/social")
async def social_login(req: SocialLoginRequest):
    # In production, verify the token with the provider's SDK.
    # For now, simulate by using the token as a unique identifier.
    provider_id = f"{req.provider}_{req.token[:20]}"
    user = await database.fetch_one(
        "SELECT * FROM users WHERE phone = :ph", {"ph": provider_id}
    )
    if not user:
        user_id = uuid.uuid4().hex
        await database.execute(
            "INSERT INTO users (id, phone, email, hashed_password, nickname) "
            "VALUES (:id, :ph, :em, :pw, :nn)",
            {
                "id": user_id,
                "ph": provider_id,
                "em": f"{req.provider}@admerce.app",
                "pw": "",   # no password for social login
                "nn": req.provider,
            },
        )
        await database.execute(
            "INSERT INTO wallets (user_id, balance) VALUES (:uid, 0.0)",
            {"uid": user_id},
        )
    else:
        user_id = user["id"]

    token = create_access_token({"sub": user_id, "phone": provider_id})
    return {"access_token": token, "token_type": "bearer", "user_id": user_id}