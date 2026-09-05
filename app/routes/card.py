from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.db.database import database
from app.routes.auth import get_current_user

router = APIRouter(prefix="/wallet", tags=["Cards"])

class AddCardRequest(BaseModel):
    card_token: str
    last4: str
    expiry_month: str
    expiry_year: str
    brand: str
    cardholder_name: Optional[str] = None

@router.get("/cards")
async def get_cards(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    rows = await database.fetch_all(
        "SELECT id, last4, expiry_month, expiry_year, brand, cardholder_name FROM cards WHERE user_id = :uid ORDER BY created_at DESC",
        {"uid": user_id}
    )
    return [dict(row) for row in rows]

@router.post("/cards")
async def add_card(req: AddCardRequest, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    await database.execute(
        """
        INSERT INTO cards (user_id, card_token, last4, expiry_month, expiry_year, brand, cardholder_name)
        VALUES (:uid, :token, :last4, :exp_m, :exp_y, :brand, :holder)
        """,
        {
            "uid": user_id,
            "token": req.card_token,
            "last4": req.last4,
            "exp_m": req.expiry_month,
            "exp_y": req.expiry_year,
            "brand": req.brand,
            "holder": req.cardholder_name
        }
    )
    return {"message": "Card added successfully"}

@router.delete("/cards/{card_id}")
async def delete_card(card_id: int, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    await database.execute(
        "DELETE FROM cards WHERE id = :id AND user_id = :uid",
        {"id": card_id, "uid": user_id}
    )
    return {"message": "Card removed"}