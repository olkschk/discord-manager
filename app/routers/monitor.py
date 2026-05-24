"""Monitor configuration: topics CRUD + donor selection + status snapshot."""
from __future__ import annotations

import asyncio
import logging

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from app.database import chat_channels, discords, proxies as proxies_coll, topics
from app.security import decrypt, require_login

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/monitor",
    dependencies=[Depends(require_login)],
    tags=["monitor"],
)


class TopicBody(BaseModel):
    channel_id: str = Field(..., min_length=1)
    label: str | None = None


async def _backfill_task(channel_id: str, owner: str) -> None:
    """Background task: fetch last 100 messages for a newly added topic channel."""
    from app.services.discord_api import build_proxy_url
    from app.services.topic_listener import backfill_channel

    # Prefer the donor account; fall back to any valid account for this owner
    donor = await discords().find_one({"owner": owner, "is_donor": True, "token_valid": True})
    if donor is None:
        donor = await discords().find_one({"owner": owner, "token_valid": True})
    if donor is None:
        logger.info("backfill %s: no usable account for owner %s", channel_id, owner)
        return

    try:
        token = decrypt(donor["discord_token"])
    except ValueError:
        logger.warning("backfill %s: could not decrypt token", channel_id)
        return

    proxy_url: str | None = None
    if donor.get("proxy_id"):
        proxy = await proxies_coll().find_one({"_id": donor["proxy_id"]})
        if proxy:
            try:
                proxy_url = build_proxy_url(
                    proxy["ip"], proxy["port"], proxy["login"], decrypt(proxy["password"])
                )
            except (ValueError, KeyError):
                pass

    await backfill_channel(channel_id, token, proxy_url)


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
    from app.services.topic_listener import notify_topics_changed

    channel_id = body.channel_id.strip()
    label = body.label
    try:
        res = await topics().insert_one({"owner": user, "channel_id": channel_id, "label": label})
    except DuplicateKeyError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Topic already registered")

    # Auto-add to saved chat channels if not already there
    existing = await chat_channels().find_one({"owner": user, "channel_id": channel_id})
    if not existing:
        await chat_channels().insert_one({
            "owner": user,
            "channel_id": channel_id,
            "label": label or channel_id,
        })

    # Signal gateway listener to start watching this channel immediately
    notify_topics_changed()

    # Backfill last 100 messages in the background (non-blocking)
    asyncio.create_task(_backfill_task(channel_id, user))

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
