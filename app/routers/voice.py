"""Voice control: connect / disconnect accounts to/from a voice channel."""
from __future__ import annotations

import asyncio
import logging
import random

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.database import voice_channels
from app.security import require_login
from app.services import gateway_pool

router = APIRouter(
    prefix="/api/voice",
    dependencies=[Depends(require_login)],
    tags=["voice"],
)
logger = logging.getLogger(__name__)


class VoiceJoinBody(BaseModel):
    account_ids: list[str] = Field(..., min_length=1)
    guild_id: str
    channel_id: str
    delay_min: float = Field(0, ge=0, le=60)
    delay_max: float = Field(0, ge=0, le=60)


@router.post("/join")
async def join_channel(body: VoiceJoinBody) -> dict:
    """Connect each account to the given voice channel, with a randomised
    delay between accounts so the channel doesn't fill in one tick."""
    if body.delay_max < body.delay_min:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "delay_max < delay_min")

    results: list[dict] = []
    for i, acc_id in enumerate(body.account_ids):
        if i > 0 and body.delay_max > 0:
            await asyncio.sleep(random.uniform(body.delay_min, body.delay_max))
        ok = await gateway_pool.join_voice(acc_id, body.guild_id, body.channel_id)
        results.append({"account_id": acc_id, "ok": ok})
    return {"results": results}


class VoiceLeaveBody(BaseModel):
    account_ids: list[str] = Field(..., min_length=1)


@router.post("/leave")
async def leave_channel(body: VoiceLeaveBody) -> dict:
    results: list[dict] = []
    for acc_id in body.account_ids:
        ok = await gateway_pool.leave_voice(acc_id)
        results.append({"account_id": acc_id, "ok": ok})
    return {"results": results}


@router.post("/disconnect")
async def disconnect(body: VoiceLeaveBody) -> dict:
    """Close the gateway WebSocket for each account (frees the connection)."""
    for acc_id in body.account_ids:
        await gateway_pool.close_one(acc_id)
    return {"closed": len(body.account_ids)}


# ── Voice channel templates ───────────────────────────────────────────────────
class VoiceChannelBody(BaseModel):
    guild_id: str = Field(..., min_length=1)
    channel_id: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1, max_length=64)


@router.get("/channels")
async def list_voice_channels() -> list[dict]:
    """List all saved voice channel templates."""
    out: list[dict] = []
    async for ch in voice_channels().find().sort("label", 1):
        out.append({
            "id": str(ch["_id"]),
            "guild_id": ch["guild_id"],
            "channel_id": ch["channel_id"],
            "label": ch.get("label", ""),
        })
    return out


@router.post("/channels")
async def create_voice_channel(body: VoiceChannelBody) -> dict:
    res = await voice_channels().insert_one({
        "guild_id": body.guild_id,
        "channel_id": body.channel_id,
        "label": body.label,
    })
    return {"id": str(res.inserted_id), "guild_id": body.guild_id, "channel_id": body.channel_id, "label": body.label}


@router.delete("/channels/{channel_doc_id}")
async def delete_voice_channel(channel_doc_id: str) -> dict:
    if not ObjectId.is_valid(channel_doc_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid id")
    res = await voice_channels().delete_one({"_id": ObjectId(channel_doc_id)})
    if res.deleted_count == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return {"deleted": True}
