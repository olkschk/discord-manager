"""Monitor configuration: topics CRUD + donor selection + status snapshot."""
from __future__ import annotations

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from app.database import discords, topics
from app.security import require_login

router = APIRouter(
    prefix="/api/monitor",
    dependencies=[Depends(require_login)],
    tags=["monitor"],
)


class TopicBody(BaseModel):
    channel_id: str = Field(..., min_length=1)
    label: str | None = None


@router.get("/topics")
async def list_topics(user: str = Depends(require_login)) -> list[dict]:
    out: list[dict] = []
    async for t in topics().find({"owner": user}).sort("_id", -1):
        out.append({"id": str(t["_id"]), "channel_id": t["channel_id"], "label": t.get("label")})
    return out


@router.post("/topics")
async def add_topic(
    body: TopicBody,
    user: str = Depends(require_login),
) -> dict:
    channel_id = body.channel_id.strip()
    label = body.label
    try:
        res = await topics().insert_one({"owner": user, "channel_id": channel_id, "label": label})
    except DuplicateKeyError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Topic already registered")
    return {"id": str(res.inserted_id), "channel_id": channel_id, "label": label}


@router.delete("/topics/{topic_id}")
async def delete_topic(
    topic_id: str,
    user: str = Depends(require_login),
) -> dict:
    if not ObjectId.is_valid(topic_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid topic id")
    res = await topics().delete_one({"_id": ObjectId(topic_id), "owner": user})
    if res.deleted_count == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return {"deleted": True}


class DonorBody(BaseModel):
    account_id: str | None = None  # None clears the donor


@router.post("/donor")
async def set_donor(
    body: DonorBody,
    user: str = Depends(require_login),
) -> dict:
    """Atomically set exactly one donor account per owner. Pass null to clear."""
    # Clear only this owner's current donor
    await discords().update_many({"owner": user, "is_donor": True}, {"$set": {"is_donor": False}})
    if body.account_id is None:
        return {"donor": None}
    if not ObjectId.is_valid(body.account_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid account id")
    res = await discords().update_one(
        {"_id": ObjectId(body.account_id), "owner": user},
        {"$set": {"is_donor": True}},
    )
    if res.matched_count == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return {"donor": body.account_id}


@router.get("/donor")
async def get_donor(user: str = Depends(require_login)) -> dict:
    donor = await discords().find_one({"owner": user, "is_donor": True})
    if donor is None:
        return {"donor": None}
    return {
        "donor": {
            "id": str(donor["_id"]),
            "email": donor["email"],
            "username": donor.get("username"),
            "token_valid": donor.get("token_valid", False),
        }
    }
