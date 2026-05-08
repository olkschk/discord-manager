"""Voice control: connect / disconnect accounts to/from a voice channel."""
from __future__ import annotations

import asyncio
import logging
import random

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.config import get_settings
from app.database import discords, voice_channels
from app.security import decrypt, require_login
from app.services import gateway_pool
from app.services.voice_player import list_sounds, play_sound, stop_playing
from app.services.stream_watcher import start_watching, stop_watching

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


# ── Audio playback ───────────────────────────────────────────────────────────
@router.get("/sounds")
async def get_sounds() -> list[str]:
    """List audio files available in the sounds directory."""
    return list_sounds(get_settings().sounds_dir)


class PlaySoundBody(BaseModel):
    account_id: str
    guild_id: str
    channel_id: str
    sound_file: str  # filename only (relative to sounds_dir)


@router.post("/play")
async def play_sound_endpoint(body: PlaySoundBody) -> dict:
    """Connect account to voice channel and play a sound file."""
    import os
    settings = get_settings()

    # Validate sound file (prevent path traversal)
    safe_name = os.path.basename(body.sound_file)
    sound_path = os.path.join(settings.sounds_dir, safe_name)

    # Load account token + proxy
    acc = await discords().find_one({"_id": ObjectId(body.account_id)})
    if acc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    try:
        token = decrypt(acc["discord_token"])
    except ValueError:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Token unreadable")

    proxy_url: str | None = None
    if acc.get("proxy_id"):
        from app.database import proxies as proxies_coll
        from app.services.discord_api import build_proxy_url
        proxy = await proxies_coll().find_one({"_id": acc["proxy_id"]})
        if proxy:
            try:
                proxy_url = build_proxy_url(
                    proxy["ip"], proxy["port"], proxy["login"], decrypt(proxy["password"])
                )
            except ValueError:
                proxy_url = None

    # Close existing gateway_pool connection — discord.py-self will take over
    await gateway_pool.close_one(body.account_id)

    return await play_sound(
        body.account_id, token,
        body.guild_id, body.channel_id,
        sound_path, proxy_url=proxy_url,
    )


@router.post("/stop")
async def stop_sound_endpoint(body: dict) -> dict:
    """Stop playback for an account and disconnect."""
    account_id = body.get("account_id", "")
    await stop_playing(account_id)
    return {"ok": True}


# ── Stream watching ───────────────────────────────────────────────────────────
class WatchStreamBody(BaseModel):
    account_id: str
    guild_id: str
    channel_id: str
    streamer_user_id: str | None = None  # Discord user ID of streamer (speeds up connect)


@router.post("/watch-stream")
async def watch_stream_endpoint(body: WatchStreamBody) -> dict:
    """Connect account to voice channel and watch an ongoing Go Live stream."""
    acc = await discords().find_one({"_id": ObjectId(body.account_id)})
    if acc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    try:
        token = decrypt(acc["discord_token"])
    except ValueError:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Token unreadable")

    proxy_url: str | None = None
    if acc.get("proxy_id"):
        from app.database import proxies as proxies_coll
        from app.services.discord_api import build_proxy_url
        proxy = await proxies_coll().find_one({"_id": acc["proxy_id"]})
        if proxy:
            try:
                proxy_url = build_proxy_url(
                    proxy["ip"], proxy["port"], proxy["login"], decrypt(proxy["password"])
                )
            except ValueError:
                proxy_url = None

    result = await start_watching(
        body.account_id, token,
        body.guild_id, body.channel_id,
        streamer_user_id=body.streamer_user_id,
        proxy_url=proxy_url,
    )

    # Mark joined_voice=True (connected to voice as stream viewer)
    if result.get("ok"):
        await discords().update_one(
            {"_id": ObjectId(body.account_id)},
            {"$set": {
                "joined_voice": True,
                "joined_stream": True,
                "voice_guild_id": body.guild_id,
                "voice_channel_id": body.channel_id,
            }},
        )
    return result


@router.post("/stop-stream")
async def stop_stream_endpoint(body: dict) -> dict:
    """Stop watching a stream and disconnect the account."""
    account_id = body.get("account_id", "")
    await stop_watching(account_id)
    await discords().update_one(
        {"_id": ObjectId(account_id)},
        {"$set": {"joined_voice": False, "joined_stream": False, "voice_channel_id": None}},
    )
    return {"ok": True}


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
