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
from app.services.discord_api import build_proxy_url
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
    account_id: str, activity_type: int, activity_name: str
) -> bool:
    conn = await get_or_create(account_id)
    if conn is None:
        return False
    await conn.set_presence(activity_type=activity_type, activity_name=activity_name)
    await discords().update_one(
        {"_id": ObjectId(account_id)},
        {"$set": {"activity": {"type": activity_type, "name": activity_name}}},
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
