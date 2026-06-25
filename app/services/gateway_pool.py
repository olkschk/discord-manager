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
import random
import time

from bson import ObjectId

from app.database import discords, proxies as proxies_coll
from app.security import decrypt
from app.services.activity_templates import build_random_activity
from app.services.discord_api import build_proxy_url, set_user_status
from app.services.gateway_client import GatewayConnection

logger = logging.getLogger(__name__)


_connections: dict[str, GatewayConnection] = {}
_rotation_tasks: dict[str, asyncio.Task] = {}
_supervisor_tasks: dict[str, asyncio.Task] = {}
_locks: dict[str, asyncio.Lock] = {}
_global_lock = asyncio.Lock()


def _get_lock(account_id: str) -> asyncio.Lock:
    """Per-account lock so connecting one account doesn't block all others."""
    if account_id not in _locks:
        _locks[account_id] = asyncio.Lock()
    return _locks[account_id]

_SUPERVISOR_INTERVAL = 30  # seconds between liveness checks

# How often a random activity is swapped for another one.
_ROTATION_MIN_SECONDS = 10 * 60
_ROTATION_MAX_SECONDS = 60 * 60


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
    """Return the existing connection or open a new one (per-account lock)."""
    lock = _get_lock(account_id)
    async with lock:
        existing = _connections.get(account_id)
        if existing is not None and existing.ws is not None and not existing.ws.closed:
            return existing
        conn = await _open_connection(account_id)
        if conn is None:
            return None
        _connections[account_id] = conn
    _start_supervisor(account_id)
    return conn


def _stop_supervisor(account_id: str) -> None:
    task = _supervisor_tasks.pop(account_id, None)
    if task is not None and not task.done():
        task.cancel()


async def _supervisor_loop(account_id: str) -> None:
    """Watch the gateway connection and reconnect if it drops, restoring presence from DB."""
    try:
        while True:
            await asyncio.sleep(_SUPERVISOR_INTERVAL)

            # Check liveness without holding the lock (cheap read).
            conn = _connections.get(account_id)
            if conn is not None and conn.ws is not None and not conn.ws.closed:
                continue

            logger.info("supervisor: gateway dead for %s — reconnecting", account_id)
            new_conn = await _open_connection(account_id)
            if new_conn is None:
                logger.warning("supervisor: reconnect failed for %s — will retry", account_id)
                continue

            # Double-check under the lock: if another coroutine already restored
            # the connection (e.g. get_or_create was called concurrently), close
            # our redundant connection rather than overwriting theirs.
            async with _get_lock(account_id):
                current = _connections.get(account_id)
                if current is not None and current.ws is not None and not current.ws.closed:
                    logger.info("supervisor: connection already restored for %s — discarding duplicate", account_id)
                    await new_conn.close()
                    continue
                _connections[account_id] = new_conn

            # Restore last known presence from DB.
            acc = await discords().find_one({"_id": ObjectId(account_id)})
            if acc:
                activity = acc.get("activity")
                activities = [activity] if isinstance(activity, dict) else []
                status = acc.get("status", "online")
                await new_conn.update_presence(status=status, activities=activities)
                logger.info("supervisor: presence restored for %s status=%s", account_id, status)

    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("supervisor crashed for %s", account_id)


def _start_supervisor(account_id: str) -> None:
    existing = _supervisor_tasks.get(account_id)
    if existing is not None and not existing.done():
        return  # already running
    _supervisor_tasks[account_id] = asyncio.create_task(
        _supervisor_loop(account_id), name=f"gw-supervisor-{account_id}"
    )


async def close_one(account_id: str) -> None:
    stop_activity_rotation(account_id)
    _stop_supervisor(account_id)
    async with _get_lock(account_id):
        conn = _connections.pop(account_id, None)
    _locks.pop(account_id, None)
    if conn is not None:
        await conn.close()


async def close_all() -> None:
    for account_id in list(_rotation_tasks):
        stop_activity_rotation(account_id)
    for account_id in list(_supervisor_tasks):
        _stop_supervisor(account_id)
    async with _global_lock:
        items = list(_connections.items())
        _connections.clear()
        _locks.clear()
    for _, conn in items:
        try:
            await conn.close()
        except Exception:  # noqa: BLE001
            logger.exception("error closing gateway connection")


_THREE_HOURS_MS = 3 * 3600 * 1000


# ── Activity rotation (random activity swapped every 10-60 minutes) ────────
async def _rotation_loop(account_id: str, start_offset_ms: int | None) -> None:
    """Rotate activity every 10-60 min.

    If the activity's displayed timer would hit 3 h, force-reset to 0
    (re-apply with start = now) regardless of the rotation schedule.
    """
    try:
        while True:
            delay = random.randint(_ROTATION_MIN_SECONDS, _ROTATION_MAX_SECONDS)
            await asyncio.sleep(delay)

            # Check whether the timer is approaching 3 h and reset if so.
            acc = await discords().find_one({"_id": ObjectId(account_id)})
            activity = (acc or {}).get("activity")
            ts_start = (
                activity.get("timestamps", {}).get("start", 0)
                if isinstance(activity, dict)
                else 0
            )
            elapsed_ms = int(time.time() * 1000) - (ts_start or 0)
            if ts_start and elapsed_ms >= _THREE_HOURS_MS:
                # Timer hit 3 h — restart from 0
                act = await build_random_activity(start_offset_ms=0)
            else:
                act = await build_random_activity(start_offset_ms=start_offset_ms)

            await set_activity(account_id, activity=act)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("activity rotation crashed for %s", account_id)


def start_activity_rotation(
    account_id: str,
    start_offset_ms: int | None = None,
) -> None:
    """(Re)start the background task that periodically swaps the account's activity."""
    existing = _rotation_tasks.get(account_id)
    if existing is not None and not existing.done():
        existing.cancel()
    _rotation_tasks[account_id] = asyncio.create_task(
        _rotation_loop(account_id, start_offset_ms),
        name=f"activity-rotation-{account_id}",
    )


def stop_activity_rotation(account_id: str) -> None:
    task = _rotation_tasks.pop(account_id, None)
    if task is not None and not task.done():
        task.cancel()


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
    # Preserve current status (online/idle/dnd/invisible) when setting activity
    acc = await discords().find_one({"_id": ObjectId(account_id)})
    current_status = (acc or {}).get("status", "online")
    await conn.set_presence(activity=activity, activity_type=activity_type, activity_name=activity_name, status=current_status)
    stored = activity or {"type": activity_type, "name": activity_name}
    await discords().update_one(
        {"_id": ObjectId(account_id)},
        {"$set": {"activity": stored}},
    )
    return True


async def clear_activity(account_id: str) -> bool:
    stop_activity_rotation(account_id)
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
    """Set presence status via gateway PRESENCE_UPDATE + DB persistence.

    No settings-proto PATCH — that would overwrite field 11 and clear
    the custom status text. The supervisor restores status from DB on
    reconnect, so persistence is handled without settings-proto.
    """
    if status not in VALID_STATUSES:
        return False
    if not ObjectId.is_valid(account_id):
        return False
    acc = await discords().find_one({"_id": ObjectId(account_id)})
    if acc is None:
        return False

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
