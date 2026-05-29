"""Stage channel control: join as audience, request to speak, leave."""
from __future__ import annotations

import asyncio
import logging
import random

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.database import discords, proxies as proxies_coll, stage_channels
from app.security import decrypt, require_login
import os

from app.services import gateway_pool
from app.services.discord_api import (
    build_proxy_url,
    create_stage_instance,
    delete_stage_instance,
    request_to_speak,
    set_voice_suppress,
)
from app.services.stage_watcher import start_stage, stop_stage
from app.services.voice_player import play_sound, stop_playing

router = APIRouter(
    prefix="/api/stage",
    dependencies=[Depends(require_login)],
    tags=["stage"],
)
logger = logging.getLogger(__name__)


async def _get_token_and_proxy(account_id: str, user: str) -> tuple[str, str | None]:
    acc = await discords().find_one({"_id": ObjectId(account_id), "owner": user})
    if acc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    try:
        token = decrypt(acc["discord_token"])
    except ValueError:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Token unreadable")

    proxy_url: str | None = None
    if acc.get("proxy_id"):
        proxy = await proxies_coll().find_one({"_id": acc["proxy_id"]})
        if proxy:
            try:
                proxy_url = build_proxy_url(
                    proxy["ip"], proxy["port"], proxy["login"], decrypt(proxy["password"])
                )
            except ValueError:
                proxy_url = None
    return token, proxy_url


class StageJoinBody(BaseModel):
    account_ids: list[str] = Field(..., min_length=1)
    guild_id: str
    channel_id: str
    delay_min: float = Field(0, ge=0, le=60)
    delay_max: float = Field(0, ge=0, le=60)


@router.post("/join")
async def join_stage(
    body: StageJoinBody,
    user: str = Depends(require_login),
) -> dict:
    """Connect each account to the Stage channel as audience (full voice WS handshake)."""
    if body.delay_max < body.delay_min:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "delay_max < delay_min")

    results: list[dict] = []
    for i, acc_id in enumerate(body.account_ids):
        if i > 0 and body.delay_max > 0:
            await asyncio.sleep(random.uniform(body.delay_min, body.delay_max))
        try:
            token, proxy_url = await _get_token_and_proxy(acc_id, user)
        except HTTPException as e:
            results.append({"account_id": acc_id, "ok": False, "error": e.detail})
            continue

        result = await start_stage(acc_id, token, body.guild_id, body.channel_id, proxy_url)
        ok = result.get("ok", False)
        if ok:
            await discords().update_one(
                {"_id": ObjectId(acc_id)},
                {"$set": {
                    "joined_voice": True,
                    "voice_guild_id": body.guild_id,
                    "voice_channel_id": body.channel_id,
                }},
            )
        results.append({"account_id": acc_id, "ok": ok})
    return {"results": results}


class StageLeaveBody(BaseModel):
    account_ids: list[str] = Field(..., min_length=1)


@router.post("/leave")
async def leave_stage(body: StageLeaveBody) -> dict:
    for acc_id in body.account_ids:
        await stop_stage(acc_id)
        await discords().update_one(
            {"_id": ObjectId(acc_id)},
            {"$set": {"joined_voice": False, "voice_channel_id": None, "voice_guild_id": None}},
        )
    return {"stopped": len(body.account_ids)}


class StageInstanceBody(BaseModel):
    account_id: str
    guild_id: str
    channel_id: str
    topic: str = Field(..., min_length=1, max_length=120)


@router.post("/start")
async def start_stage_instance(
    body: StageInstanceBody,
    user: str = Depends(require_login),
) -> dict:
    """Create a Stage instance (start the stage) and become the speaker."""
    token, proxy_url = await _get_token_and_proxy(body.account_id, user)

    result = await create_stage_instance(
        token, body.channel_id, body.topic, proxy_url=proxy_url
    )
    if not result.get("ok"):
        return result

    # Unsuppress self to become speaker — retry a few times in case voice state
    # isn't registered yet (account must be connected to the channel first)
    suppress_result: dict = {"ok": False}
    for attempt in range(3):
        await asyncio.sleep(0.6 * (attempt + 1))
        suppress_result = await set_voice_suppress(
            token, body.guild_id, body.channel_id, suppress=False, proxy_url=proxy_url
        )
        if suppress_result.get("ok"):
            break
        if suppress_result.get("status") == 404 and attempt < 2:
            logger.warning("set_voice_suppress attempt %d: voice state not ready, retrying", attempt + 1)
            continue
        break

    return {"ok": True, "stage": result.get("stage"), "speaker": suppress_result.get("ok")}


@router.post("/end")
async def end_stage_instance(
    body: StageInstanceBody,
    user: str = Depends(require_login),
) -> dict:
    """Delete the Stage instance (end the stage)."""
    token, proxy_url = await _get_token_and_proxy(body.account_id, user)
    return await delete_stage_instance(token, body.channel_id, proxy_url=proxy_url)


class SpeakRequestBody(BaseModel):
    account_id: str
    guild_id: str
    channel_id: str
    request: bool = True


@router.post("/request-speak")
async def request_speak_endpoint(
    body: SpeakRequestBody,
    user: str = Depends(require_login),
) -> dict:
    """Request or withdraw speaker status on a Stage channel."""
    token, proxy_url = await _get_token_and_proxy(body.account_id, user)
    return await request_to_speak(
        token, body.guild_id, body.channel_id,
        request=body.request,
        proxy_url=proxy_url,
    )


# ── Audio playback ───────────────────────────────────────────────────────────
class StagePlayBody(BaseModel):
    account_id: str
    guild_id: str
    channel_id: str
    sound_file: str
    loop: bool = False


@router.post("/play")
async def stage_play_sound(
    body: StagePlayBody,
    user: str = Depends(require_login),
) -> dict:
    """Connect account to stage, unsuppress (become speaker), then play sound."""
    from app.config import get_settings
    settings = get_settings()

    safe_name = os.path.basename(body.sound_file)
    sound_path = os.path.join(settings.sounds_dir, safe_name)

    token, proxy_url = await _get_token_and_proxy(body.account_id, user)

    # Stop existing stage watcher so voice_player can take over the connection
    await stop_stage(body.account_id)
    await gateway_pool.close_one(body.account_id)

    result = await play_sound(
        body.account_id, token,
        body.guild_id, body.channel_id,
        sound_path, proxy_url=proxy_url, loop=body.loop,
    )

    if result.get("ok"):
        # Unsuppress to become speaker — voice state now exists after play_sound connected
        await asyncio.sleep(0.5)
        suppress_result = await set_voice_suppress(
            token, body.guild_id, body.channel_id, suppress=False, proxy_url=proxy_url
        )
        if not suppress_result.get("ok"):
            logger.warning("stage_play: set_voice_suppress failed: %s", suppress_result)
        result["speaker"] = suppress_result.get("ok", False)

    return result


@router.post("/stop-play")
async def stage_stop_sound(body: dict) -> dict:
    account_id = body.get("account_id", "")
    await stop_playing(account_id)
    return {"ok": True}


# ── Saved stage channel templates ──────────────────────────────────────────────
class StageChannelBody(BaseModel):
    guild_id: str = Field(..., min_length=1)
    channel_id: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1, max_length=64)


@router.get("/channels")
async def list_stage_channels(user: str = Depends(require_login)) -> list[dict]:
    out: list[dict] = []
    async for ch in stage_channels().find({"owner": user}).sort("label", 1):
        out.append({
            "id": str(ch["_id"]),
            "guild_id": ch["guild_id"],
            "channel_id": ch["channel_id"],
            "label": ch.get("label", ""),
        })
    return out


@router.post("/channels")
async def create_stage_channel(
    body: StageChannelBody,
    user: str = Depends(require_login),
) -> dict:
    res = await stage_channels().insert_one({
        "owner": user,
        "guild_id": body.guild_id,
        "channel_id": body.channel_id,
        "label": body.label,
    })
    return {
        "id": str(res.inserted_id),
        "guild_id": body.guild_id,
        "channel_id": body.channel_id,
        "label": body.label,
    }


@router.delete("/channels/{channel_doc_id}")
async def delete_stage_channel(
    channel_doc_id: str,
    user: str = Depends(require_login),
) -> dict:
    if not ObjectId.is_valid(channel_doc_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid id")
    res = await stage_channels().delete_one({"_id": ObjectId(channel_doc_id), "owner": user})
    if res.deleted_count == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return {"deleted": True}
