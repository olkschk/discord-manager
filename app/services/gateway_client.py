"""Discord gateway (WebSocket) client — minimal: IDENTIFY, heartbeat,
PRESENCE_UPDATE, VOICE_STATE_UPDATE.

Used to set custom activities and to make accounts appear in voice channels.
We don't transport audio — joining the voice channel as "online" is enough for
the spec ("Join voice channel"). Streaming-watch is *not* implemented.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp
from aiohttp import ClientWebSocketResponse, WSMsgType

from app.config import get_settings

logger = logging.getLogger(__name__)


# Discord gateway op codes
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_PRESENCE_UPDATE = 3
OP_VOICE_STATE_UPDATE = 4
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11


class GatewayConnection:
    """Single per-account gateway connection. Long-lived once connected."""

    def __init__(self, token: str, *, proxy_url: str | None = None) -> None:
        self.token = token
        self.proxy_url = proxy_url
        self.session: aiohttp.ClientSession | None = None
        self.ws: ClientWebSocketResponse | None = None
        self.heartbeat_interval: float = 41.25
        self.last_seq: int | None = None
        self.user_id: str | None = None
        self.session_id: str | None = None  # populated from READY event
        self._heartbeat_task: asyncio.Task | None = None
        self._reader_task: asyncio.Task | None = None
        self._closed = False

    async def connect(self) -> bool:
        """Open WS, complete HELLO + IDENTIFY + READY. Returns True on success."""
        settings = get_settings()
        self.session = aiohttp.ClientSession()
        try:
            self.ws = await self.session.ws_connect(
                settings.discord_gateway_url,
                proxy=self.proxy_url,
                heartbeat=None,  # we send our own heartbeats per Discord protocol
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("gateway connect failed: %s", exc)
            await self.session.close()
            self.session = None
            return False

        # HELLO
        msg = await self.ws.receive(timeout=15)
        if msg.type != WSMsgType.TEXT:
            await self._teardown()
            return False
        data = json.loads(msg.data)
        if data.get("op") != OP_HELLO:
            await self._teardown()
            return False
        self.heartbeat_interval = data["d"]["heartbeat_interval"] / 1000.0

        # IDENTIFY
        await self._send(
            {
                "op": OP_IDENTIFY,
                "d": {
                    "token": self.token,
                    "intents": 0,
                    "properties": {
                        "$os": settings.gateway_identify_os,
                        "$browser": settings.gateway_identify_browser,
                        "$device": "",
                    },
                },
            }
        )

        # Wait for READY (skipping any other dispatches)
        ready = False
        for _ in range(50):
            msg = await self.ws.receive(timeout=20)
            if msg.type != WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if data.get("s") is not None:
                self.last_seq = data["s"]
            op = data.get("op")
            if op == OP_DISPATCH and data.get("t") == "READY":
                d = data.get("d") or {}
                self.user_id = (d.get("user") or {}).get("id")
                self.session_id = d.get("session_id")  # needed for join_invite payload
                ready = True
                break
            if op == OP_INVALID_SESSION:
                logger.warning("gateway INVALID_SESSION during identify")
                await self._teardown()
                return False

        if not ready:
            await self._teardown()
            return False

        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="gw-heartbeat"
        )
        self._reader_task = asyncio.create_task(self._reader_loop(), name="gw-reader")
        logger.info("gateway READY user_id=%s", self.user_id)
        return True

    async def _send(self, payload: dict[str, Any]) -> None:
        if self.ws is None or self.ws.closed:
            return
        try:
            await self.ws.send_str(json.dumps(payload))
        except (aiohttp.ClientError, ConnectionResetError) as exc:
            logger.warning("gateway send failed: %s", exc)

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._closed and self.ws is not None and not self.ws.closed:
                await asyncio.sleep(self.heartbeat_interval)
                await self._send({"op": OP_HEARTBEAT, "d": self.last_seq})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("heartbeat loop crashed")

    async def _reader_loop(self) -> None:
        try:
            while not self._closed and self.ws is not None and not self.ws.closed:
                msg = await self.ws.receive()
                if msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("s") is not None:
                        self.last_seq = data["s"]
                elif msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSING, WSMsgType.ERROR):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("gateway reader crashed")

    # ── Public actions ───────────────────────────────────────────────────
    async def set_presence(
        self,
        *,
        activity: dict | None = None,
        activity_type: int = 0,
        activity_name: str = "",
        status: str = "online",
    ) -> None:
        """Send PRESENCE_UPDATE. Pass a full `activity` dict (preferred) or simple type+name."""
        act = activity or {"name": activity_name, "type": activity_type}
        await self._send(
            {
                "op": OP_PRESENCE_UPDATE,
                "d": {
                    "since": None,
                    "activities": [act],
                    "status": status,
                    "afk": False,
                },
            }
        )

    async def clear_presence(self) -> None:
        await self._send(
            {
                "op": OP_PRESENCE_UPDATE,
                "d": {"since": None, "activities": [], "status": "online", "afk": False},
            }
        )

    async def join_voice(
        self, guild_id: str, channel_id: str, *, mute: bool = False, deaf: bool = True
    ) -> None:
        await self._send(
            {
                "op": OP_VOICE_STATE_UPDATE,
                "d": {
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "self_mute": mute,
                    "self_deaf": deaf,
                },
            }
        )

    async def leave_voice(self, guild_id: str) -> None:
        await self._send(
            {
                "op": OP_VOICE_STATE_UPDATE,
                "d": {
                    "guild_id": guild_id,
                    "channel_id": None,
                    "self_mute": False,
                    "self_deaf": False,
                },
            }
        )

    async def _teardown(self) -> None:
        if self.ws and not self.ws.closed:
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self.session and not self.session.closed:
            await self.session.close()
        self.ws = None
        self.session = None

    async def close(self) -> None:
        self._closed = True
        for task in (self._heartbeat_task, self._reader_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self._teardown()
