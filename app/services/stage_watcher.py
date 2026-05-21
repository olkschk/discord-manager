"""Discord Stage channel audience watcher.

Flow:
  Gateway: IDENTIFY → READY → op4 VOICE_STATE_UPDATE (join stage) →
  VOICE_SERVER_UPDATE → voice WebSocket handshake (stay as audience).

Without completing the voice WS handshake Discord disconnects the
account within ~3 seconds of receiving VOICE_SERVER_UPDATE.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid

import aiohttp

from app.config import get_settings
from app.services.discord_api import _x_super_properties

logger = logging.getLogger(__name__)

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

_watchers: dict[str, "StageWatcher"] = {}
_watcher_tasks: dict[str, asyncio.Task] = {}


def _build_identify_props() -> dict:
    s = get_settings()
    ua = s.discord_user_agent
    chrome_ver = "147.0.0.0"
    if "Chrome/" in ua:
        try:
            chrome_ver = ua.split("Chrome/")[1].split(" ")[0]
        except IndexError:
            pass
    return {
        "os": "Windows", "browser": "Chrome", "device": "",
        "system_locale": "ru-RU", "has_client_mods": False,
        "browser_user_agent": ua,
        "browser_version": chrome_ver,
        "os_version": "10", "referrer": "", "referring_domain": "",
        "release_channel": "stable",
        "client_build_number": 539147,
        "client_event_source": None,
    }


class StageWatcher:
    def __init__(
        self,
        token: str,
        guild_id: str,
        channel_id: str,
        *,
        proxy: str | None = None,
    ):
        self.token = token
        self.guild_id = str(guild_id)
        self.channel_id = str(channel_id)
        self.proxy = proxy

        self._user_id: str | None = None
        self._session_id: str | None = None
        self._gw_last_seq = None
        self.connected = asyncio.Event()

    def _ws_kwargs(self) -> dict:
        return {"proxy": self.proxy} if self.proxy else {}

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _gen_sdp(self) -> str:
        import secrets
        chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        ufrag = "".join(random.choices(chars, k=4))
        pwd = "".join(random.choices(chars + "+/", k=24))
        fp = ":".join(f"{b:02X}" for b in secrets.token_bytes(32))
        return (
            f"a=extmap-allow-mixed\na=ice-ufrag:{ufrag}\na=ice-pwd:{pwd}\n"
            f"a=ice-options:trickle\na=fingerprint:sha-256 {fp}\n"
            f"a=rtpmap:111 opus/48000/2"
        )

    async def _voice_ws(
        self,
        session: aiohttp.ClientSession,
        endpoint: str,
        token: str,
    ) -> None:
        """Connect to Discord voice server using WebRTC protocol (no UDP discovery needed)."""
        url = f"wss://{endpoint}/?v=9"
        hb_task = None
        seq_ack = 0
        select_sent = False
        rtc_conn_id = str(uuid.uuid4())

        try:
            async with session.ws_connect(url, **self._ws_kwargs()) as ws:
                async for msg in ws:
                    if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        break
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        seq_ack += 1
                        continue

                    payload = json.loads(msg.data)
                    op = payload.get("op")
                    seq_ack += 1

                    if op == 8:  # HELLO
                        interval = payload["d"]["heartbeat_interval"] / 1000
                        async def _hb(ws=ws, iv=interval):
                            nonlocal seq_ack
                            while True:
                                await asyncio.sleep(iv)
                                await ws.send_json({"op": 3, "d": {"t": self._now_ms(), "seq_ack": seq_ack}})
                        hb_task = asyncio.create_task(_hb())
                        await ws.send_json({"op": 0, "d": {
                            "server_id": self.guild_id,
                            "channel_id": self.channel_id,
                            "user_id": str(self._user_id),
                            "session_id": self._session_id,
                            "token": token,
                            "max_dave_protocol_version": 1,
                            "video": False,
                            "streams": [],
                        }})
                        logger.info("stage_watcher voice WS IDENTIFY sent guild=%s", self.guild_id)

                    elif op == 2:  # READY — acknowledge, wait for op 16 to send SELECT_PROTOCOL
                        await ws.send_json({"op": 16, "d": {}})
                        logger.info("stage_watcher voice WS READY ack sent")

                    elif op == 16:  # Server version info — now send SELECT_PROTOCOL (WebRTC)
                        if not select_sent:
                            select_sent = True
                            sdp = self._gen_sdp()
                            await ws.send_json({"op": 1, "d": {
                                "protocol": "webrtc",
                                "data": sdp,
                                "sdp": sdp,
                                "codecs": [
                                    {"name": "opus", "type": "audio", "priority": 1000, "payload_type": 111, "rtx_payload_type": None},
                                ],
                                "rtc_connection_id": rtc_conn_id,
                            }})
                            logger.info("stage_watcher voice WS SELECT_PROTOCOL (webrtc) sent")

                    elif op == 4:  # SESSION_DESCRIPTION — fully connected as audience
                        self.connected.set()
                        logger.info("stage_watcher connected as audience guild=%s channel=%s", self.guild_id, self.channel_id)

                    elif op == 9:  # disconnect
                        break

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("stage_watcher voice WS error: %s", exc)
        finally:
            if hb_task:
                hb_task.cancel()

    async def _run_gateway(self, session: aiohttp.ClientSession) -> None:
        async with session.ws_connect(GATEWAY_URL, **self._ws_kwargs()) as ws:
            gw_hb_task = voice_task = None

            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                payload = json.loads(msg.data)
                op = payload["op"]
                if payload.get("s"):
                    self._gw_last_seq = payload["s"]

                if op == 10:  # HELLO
                    interval = payload["d"]["heartbeat_interval"] / 1000
                    async def _gw_hb(ws=ws, iv=interval):
                        while True:
                            await asyncio.sleep(iv)
                            try:
                                await ws.send_json({"op": 1, "d": self._gw_last_seq})
                            except Exception:
                                return
                    gw_hb_task = asyncio.create_task(_gw_hb())
                    await ws.send_json({"op": 2, "d": {
                        "token": self.token,
                        "capabilities": 1734653,
                        "properties": _build_identify_props(),
                        "presence": {"status": "online", "since": 0, "activities": [], "afk": False},
                        "compress": False,
                        "client_state": {"guild_versions": {}},
                    }})

                elif op == 0:
                    event = payload.get("t")
                    data = payload.get("d", {})

                    if event == "READY":
                        self._user_id = data["user"]["id"]
                        self._session_id = data["session_id"]
                        await asyncio.sleep(random.uniform(0.5, 1.2))
                        await ws.send_json({"op": 4, "d": {
                            "guild_id": self.guild_id,
                            "channel_id": self.channel_id,
                            "self_mute": True,
                            "self_deaf": False,
                        }})
                        logger.info("stage_watcher gateway READY user_id=%s, joining stage channel=%s", self._user_id, self.channel_id)

                    elif event == "VOICE_STATE_UPDATE":
                        uid = str(data.get("user_id", ""))
                        if uid == str(self._user_id):
                            self._session_id = data.get("session_id", self._session_id)
                            logger.info("stage_watcher VOICE_STATE_UPDATE channel_id=%s suppress=%s", data.get("channel_id"), data.get("suppress"))

                    elif event == "VOICE_SERVER_UPDATE":
                        ep = data.get("endpoint", "")
                        tok = data.get("token", "")
                        if ep and tok:
                            logger.info("stage_watcher VOICE_SERVER_UPDATE endpoint=%s", ep)
                            if voice_task and not voice_task.done():
                                voice_task.cancel()
                            voice_task = asyncio.create_task(
                                self._voice_ws(session, ep, tok)
                            )

                elif op in (7, 9):
                    break

            for t in (gw_hb_task, voice_task):
                if t:
                    t.cancel()

    async def run(self) -> None:
        connector = aiohttp.TCPConnector()
        async with aiohttp.ClientSession(connector=connector) as session:
            while True:
                self.connected.clear()
                try:
                    await self._run_gateway(session)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("stage_watcher error: %s — reconnecting in 5s", exc)
                await asyncio.sleep(5)


async def start_stage(
    account_id: str,
    token: str,
    guild_id: str,
    channel_id: str,
    proxy_url: str | None = None,
) -> dict:
    await stop_stage(account_id)

    watcher = StageWatcher(token, guild_id, channel_id, proxy=proxy_url)
    _watchers[account_id] = watcher

    task = asyncio.create_task(watcher.run(), name=f"stage-{account_id}")
    _watcher_tasks[account_id] = task

    try:
        await asyncio.wait_for(asyncio.shield(watcher.connected.wait()), timeout=20)
        return {"ok": True}
    except asyncio.TimeoutError:
        logger.warning("stage_watcher timeout for %s — still running", account_id)
        return {"ok": True, "note": "connected to gateway, voice handshake pending"}


async def stop_stage(account_id: str) -> None:
    task = _watcher_tasks.pop(account_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    _watchers.pop(account_id, None)
    logger.info("stage_watcher stopped for %s", account_id)
