"""Process-wide pool of `GatewayConnection`s, keyed by Discord account id.

Connections are opened on demand (set_activity / join_voice / set_status) and
held open until explicitly cleared or until the app shuts down. Each account
gets at most one connection.

Activity and voice state only persist for the lifetime of the WebSocket — when
the process restarts, operators re-apply via the UI. (We could restore on
startup; leaving that for a Phase 6 refinement.)
"""
from __future__ import annotations

import asyncio
import logging

from bson import ObjectId

from app.database import discords, proxies as proxies_coll
from app.security import decrypt
from app.services.discord_api import build_proxy_url, set_user_status
from app.services.gateway_client import GatewayConnection

logger = logging.getLogger(__name__)


_connections: dict[str, GatewayConnection] = {}
_lock = asyncio.Lock()


async def _resolve_proxy(acc: dict) -> str | None:
    proxy_id = acc.get("proxy_id")
    if proxy_id is None:
        return None
    proxy = await proxies_coll().find_one({"_id": proxy_id})
    if proxy is None:
        return None
    try:
        return build_proxy_url(
            proxy["ip"], proxy["port"], proxy["login"], decrypt(proxy["password"])
        )
    except ValueError:
        return None


async def _open_connection(account_id: str) -> GatewayConnection | None:
    if not ObjectId.is_valid(account_id):
        return None
    acc = await discords().find_one({"_id": ObjectId(account_id)})
    if acc is None:
        return None
    try:
        token = decrypt(acc["discord_token"])
    except ValueError:
        return None
    proxy_url = await _resolve_proxy(acc)
    conn = GatewayConnection(token, proxy_url=proxy_url)
    if not await conn.connect():
        return None
    return conn


async def get_or_create(account_id: str) -> GatewayConnection | None:
    """Return the existing connection or open a new one."""
    async with _lock:
        existing = _connections.get(account_id)
        if existing is not None and existing.ws is not None and not existing.ws.closed:
            return existing
        conn = await _open_connection(account_id)
        if conn is None:
            return None
        _connections[account_id] = conn
        return conn


async def close_one(account_id: str) -> None:
    async with _lock:
        conn = _connections.pop(account_id, None)
    if conn is not None:
        await conn.close()


async def close_all() -> None:
    async with _lock:
        items = list(_connections.items())
        _connections.clear()
    for _, conn in items:
        try:
            await conn.close()
        except Exception:  # noqa: BLE001
            logger.exception("error closing gateway connection")


def is_connected(account_id: str) -> bool:
    conn = _connections.get(account_id)
    return conn is not None and conn.ws is not None and not conn.ws.closed


# ── High-level operations (orchestrate WS + DB persistence) ─────────────
async def set_activity(
    account_id: str,
    activity_type: int = 0,
    activity_name: str = "",
    activity: dict | None = None,
) -> bool:
    """Set activity. Pass a full `activity` dict (Spotify/game with icons) or simple type+name."""
    conn = await get_or_create(account_id)
    if conn is None:
        return False
    await conn.set_presence(activity=activity, activity_type=activity_type, activity_name=activity_name)
    stored = activity or {"type": activity_type, "name": activity_name}
    await discords().update_one(
        {"_id": ObjectId(account_id)},
        {"$set": {"activity": stored}},
    )
    return True


async def clear_activity(account_id: str) -> bool:
    conn = _connections.get(account_id)
    if conn is None:
        # Nothing to clear at WS level; just clear the DB record.
        await discords().update_one(
            {"_id": ObjectId(account_id)}, {"$unset": {"activity": ""}}
        )
        return True
    await conn.clear_presence()
    await discords().update_one(
        {"_id": ObjectId(account_id)}, {"$unset": {"activity": ""}}
    )
    return True


VALID_STATUSES = {"online", "idle", "dnd", "invisible"}


async def set_status(account_id: str, status: str) -> bool:
    """Set presence status (online/idle/dnd/invisible) via the settings-proto endpoint
    (the same call the official client makes — gateway PRESENCE_UPDATE alone does not stick).
    Also nudges the gateway connection (if any) so it doesn't override it on next heartbeat.
    """
    if status not in VALID_STATUSES:
        return False
    if not ObjectId.is_valid(account_id):
        return False
    acc = await discords().find_one({"_id": ObjectId(account_id)})
    if acc is None:
        return False
    try:
        token = decrypt(acc["discord_token"])
    except ValueError:
        return False
    proxy_url = await _resolve_proxy(acc)

    # Discord's settings-proto endpoint silently normalizes a persisted "invisible"
    # status to "dnd" — invisible only exists as a gateway-side presence value, not
    # a stored setting. Skip the PATCH for it; the gateway PRESENCE_UPDATE below is
    # what actually makes the account appear offline to others.
    if status != "invisible":
        out = await set_user_status(token, status, proxy_url=proxy_url)
        if not out.get("ok"):
            return False

    # A status only shows to other users while a gateway session is broadcasting
    # presence — open one (or reuse the existing one) and push the new status.
    conn = await get_or_create(account_id)
    if conn is not None:
        activities = [acc["activity"]] if acc.get("activity") else []
        await conn.update_presence(status=status, activities=activities)

    await discords().update_one(
        {"_id": ObjectId(account_id)},
        {"$set": {"status": status}},
    )
    return True


async def join_voice(
    account_id: str, guild_id: str, channel_id: str
) -> bool:
    conn = await get_or_create(account_id)
    if conn is None:
        return False
    await conn.join_voice(guild_id, channel_id)
    await discords().update_one(
        {"_id": ObjectId(account_id)},
        {
            "$set": {
                "joined_voice": True,
                "voice_guild_id": guild_id,
                "voice_channel_id": channel_id,
            }
        },
    )
    return True


async def set_mute(account_id: str, mute: bool) -> bool:
    """Send VOICE_STATE_UPDATE with new self_mute value, keeping current channel."""
    acc = await discords().find_one({"_id": ObjectId(account_id)})
    if not acc or not acc.get("voice_guild_id") or not acc.get("voice_channel_id"):
        return False
    conn = await get_or_create(account_id)
    if conn is None:
        return False
    await conn.join_voice(acc["voice_guild_id"], acc["voice_channel_id"], mute=mute, deaf=False)
    await discords().update_one(
        {"_id": ObjectId(account_id)},
        {"$set": {"voice_muted": mute}},
    )
    return True


async def join_stage(
    account_id: str, guild_id: str, channel_id: str
) -> bool:
    conn = await get_or_create(account_id)
    if conn is None:
        return False
    await conn.join_stage(guild_id, channel_id)
    await discords().update_one(
        {"_id": ObjectId(account_id)},
        {
            "$set": {
                "joined_voice": True,
                "voice_guild_id": guild_id,
                "voice_channel_id": channel_id,
            }
        },
    )
    return True


async def leave_voice(account_id: str) -> bool:
    conn = _connections.get(account_id)
    if conn is not None:
        acc = await discords().find_one({"_id": ObjectId(account_id)})
        guild_id = (acc or {}).get("voice_guild_id")
        if guild_id:
            await conn.leave_voice(guild_id)
    await discords().update_one(
        {"_id": ObjectId(account_id)},
        {
            "$set": {
                "joined_voice": False,
                "voice_channel_id": None,
                "voice_guild_id": None,
            }
        },
    )
    return True
