"""Discord Go Live stream watcher — adapted from discord-farm/disc/stream.py.

Gateway flow:
  IDENTIFY → READY → op4 VOICE_STATE (join channel) →
  VOICE_SERVER_UPDATE #1 (regular c-waw voice) →
  STREAM_CREATE / VOICE_STATE_UPDATE (get stream_key) →
  op20 STREAM_WATCH on gateway →
  VOICE_SERVER_UPDATE #2 / STREAM_SERVER_UPDATE (stream warsaw server) →
  stream voice WebSocket (WebRTC viewer handshake)
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import struct
import time
import uuid
from urllib.parse import quote

import aiohttp

from app.services.discord_api import _headers as _build_headers_from_token, _x_super_properties
from app.config import get_settings

logger = logging.getLogger(__name__)

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
DISCORD_API = "https://discord.com/api/v9"

_VIEWPORTS = [(1280, 720), (1366, 768), (1440, 900), (1920, 1080)]

# Track active watchers: account_id → StreamWatcher
_watchers: dict[str, "StreamWatcher"] = {}
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
        "client_build_number": s.discord_gateway_url and 539147 or 539147,
        "client_event_source": None,
    }


class StreamWatcher:
    def __init__(
        self,
        token: str,
        guild_id: str,
        channel_id: str,
        *,
        proxy: str | None = None,
        streamer_user_id: str | None = None,
    ):
        self.token = token
        self.guild_id = str(guild_id)
        self.channel_id = str(channel_id)
        self.proxy = proxy
        self._streamer_user_id = str(streamer_user_id) if streamer_user_id else None

        self._user_id: str | None = None
        self._session_id: str | None = None
        self._voice_endpoint: str | None = None
        self._voice_token: str | None = None
        self._stream_endpoint: str | None = None
        self._stream_token: str | None = None
        self._stream_key: str | None = None
        self._gw_hb_interval = 41.25
        self._gw_last_seq = None
        self._gw_ws = None
        self.stream_ready = asyncio.Event()
        self._hb_session_id = str(uuid.uuid4())
        self._uptime_start = time.time()
        self._event_seq = random.randint(400, 600)
        self._vp_w, self._vp_h = random.choice(_VIEWPORTS)

    def _ws_kwargs(self) -> dict:
        return {"proxy": self.proxy} if self.proxy else {}

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _next_seq(self) -> int:
        self._event_seq += 1
        return self._event_seq

    def _uptime_ms(self) -> int:
        return int((time.time() - self._uptime_start) * 1000)

    def _api_headers(self) -> dict:
        s = get_settings()
        ua = s.discord_user_agent
        return {
            "Authorization": self.token,
            "User-Agent": ua,
            "Content-Type": "application/json",
            "X-Super-Properties": _x_super_properties(ua),
            "X-Discord-Locale": "en-US",
            "X-Discord-Timezone": "Europe/Kiev",
        }

    def _gen_sdp(self) -> str:
        import secrets
        chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        ufrag = "".join(random.choices(chars, k=4))
        pwd = "".join(random.choices(chars + "+/", k=24))
        fp = ":".join(f"{b:02X}" for b in secrets.token_bytes(32))
        return (
            f"a=extmap-allow-mixed\na=ice-ufrag:{ufrag}\na=ice-pwd:{pwd}\n"
            f"a=ice-options:trickle\na=fingerprint:sha-256 {fp}\n"
            f"a=rtpmap:111 opus/48000/2\na=rtpmap:96 VP8/90000\na=rtpmap:97 rtx/90000"
        )

    async def _udp_discovery(self, ip: str, port: int, ssrc: int):
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()

        class _Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                if not future.done():
                    future.set_result(data)
            def error_received(self, exc):
                if not future.done():
                    future.set_exception(exc)

        transport, _ = await loop.create_datagram_endpoint(_Proto, remote_addr=(ip, port))
        pkt = struct.pack(">HHI", 1, 70, ssrc) + b"\x00" * 66
        transport.sendto(pkt)
        data = await asyncio.wait_for(future, timeout=10.0)
        our_ip = data[8:72].rstrip(b"\x00").decode("utf-8")
        our_port = struct.unpack_from(">H", data, 72)[0]
        return transport, our_ip, our_port

    async def _voice_ws(
        self, session: aiohttp.ClientSession,
        endpoint: str, token: str,
        is_stream: bool = False,
        server_id: str | None = None,
        channel_id: str | None = None,
    ):
        url = f"wss://{endpoint}/?v=9"
        label = "[stream-ws]" if is_stream else "[voice-ws]"
        sid = server_id or self.guild_id
        cid = channel_id or self.channel_id
        hb_task = keepalive_task = udp_transport = None
        seq_ack = select_protocol_sent = 0
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
                        stream_type = "screen" if is_stream else "video"
                        await ws.send_json({"op": 0, "d": {
                            "server_id": sid, "channel_id": cid,
                            "user_id": str(self._user_id),
                            "session_id": self._session_id, "token": token,
                            "max_dave_protocol_version": 1, "video": True,
                            "streams": [{"type": stream_type, "rid": "100", "quality": 100}],
                        }})

                    elif op == 2:  # READY
                        d = payload["d"]
                        if not is_stream:
                            ssrc = d["ssrc"]
                            try:
                                udp_transport, our_ip, our_port = await self._udp_discovery(
                                    d.get("ip", "127.0.0.1"), d.get("port", 0), ssrc
                                )
                            except Exception:
                                our_ip, our_port = "127.0.0.1", 0
                            await ws.send_json({"op": 1, "d": {
                                "protocol": "udp",
                                "data": {"address": our_ip, "port": our_port, "mode": "xsalsa20_poly1305_lite"},
                            }})
                        else:
                            await ws.send_json({"op": 16, "d": {}})

                    elif op == 16:  # Server version → SELECT_PROTOCOL (stream)
                        if is_stream and not select_protocol_sent:
                            select_protocol_sent = 1
                            sdp = self._gen_sdp()
                            await ws.send_json({"op": 1, "d": {
                                "protocol": "webrtc", "data": sdp, "sdp": sdp,
                                "codecs": [
                                    {"name": "opus", "type": "audio", "priority": 1000, "payload_type": 111, "rtx_payload_type": None},
                                    {"name": "VP8",  "type": "video", "priority": 1000, "payload_type": 96,  "rtx_payload_type": 97},
                                ],
                                "rtc_connection_id": rtc_conn_id,
                            }})

                    elif op == 4:  # SESSION_DESCRIPTION
                        if not is_stream:
                            if udp_transport:
                                counter = 0
                                async def _udp_kp(t=udp_transport):
                                    nonlocal counter
                                    while True:
                                        await asyncio.sleep(5)
                                        try:
                                            t.sendto(struct.pack("<I", counter & 0xFFFFFFFF))
                                            counter += 1
                                        except Exception:
                                            pass
                                keepalive_task = asyncio.create_task(_udp_kp())
                            if self._gw_ws and self._stream_key:
                                try:
                                    await self._gw_ws.send_json({"op": 20, "d": {"stream_key": self._stream_key}})
                                    self.stream_ready.set()
                                    await ws.send_json({"op": 15, "d": {"any": 100}})
                                except Exception as e:
                                    logger.error("[stream] op20 error: %s", e)
                        else:
                            self.stream_ready.set()
                            logger.info("[stream] Stream viewer connected ✓")

                    elif op == 9:
                        break

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("%s error: %s", label, exc)
        finally:
            for t in (hb_task, keepalive_task):
                if t:
                    t.cancel()
            if udp_transport:
                udp_transport.close()

    async def _run_gateway(self, session: aiohttp.ClientSession):
        async with session.ws_connect(GATEWAY_URL, **self._ws_kwargs()) as ws:
            self._gw_ws = ws
            gw_hb_task = voice_task = None

            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                payload = json.loads(msg.data)
                op = payload["op"]
                if payload.get("s"):
                    self._gw_last_seq = payload["s"]

                if op == 10:  # HELLO
                    self._gw_hb_interval = payload["d"]["heartbeat_interval"] / 1000
                    async def _gw_hb(ws=ws):
                        while True:
                            await asyncio.sleep(self._gw_hb_interval)
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
                        await asyncio.sleep(random.uniform(0.5, 1.5))
                        await ws.send_json({"op": 4, "d": {
                            "guild_id": self.guild_id,
                            "channel_id": self.channel_id,
                            "self_mute": True,
                            "self_deaf": False,
                        }})

                    elif event == "GUILD_CREATE":
                        for vs in data.get("voice_states", []):
                            if (vs.get("self_stream")
                                and str(vs.get("channel_id")) == self.channel_id
                                and str(vs.get("user_id")) != str(self._user_id)
                                and not self._stream_key):
                                self._stream_key = f"guild:{self.guild_id}:{self.channel_id}:{vs['user_id']}"

                    elif event == "VOICE_STATE_UPDATE":
                        uid = str(data.get("user_id", ""))
                        if uid == str(self._user_id):
                            self._session_id = data["session_id"]
                            if self._streamer_user_id and not self._stream_key:
                                self._stream_key = f"guild:{self.guild_id}:{self.channel_id}:{self._streamer_user_id}"
                            if self._stream_key:
                                try:
                                    await ws.send_json({"op": 20, "d": {"stream_key": self._stream_key}})
                                except Exception as e:
                                    logger.error("[stream] op20 early error: %s", e)
                        elif (data.get("self_stream")
                              and str(data.get("channel_id")) == self.channel_id
                              and not self._stream_key):
                            self._stream_key = f"guild:{self.guild_id}:{self.channel_id}:{uid}"

                    elif event in ("VOICE_SERVER_UPDATE", "STREAM_SERVER_UPDATE"):
                        ep = data.get("endpoint", "")
                        tok = data.get("token", "")
                        raw_gid = data.get("guild_id")
                        if not ep or not tok:
                            continue
                        if self._voice_endpoint is None:
                            self._voice_endpoint = ep
                            self._voice_token = tok
                            if voice_task and not voice_task.done():
                                voice_task.cancel()
                            voice_task = asyncio.create_task(
                                self._voice_ws(session, ep, tok, is_stream=False)
                            )
                        elif not self._stream_endpoint:
                            self._stream_endpoint = ep
                            srv_id = str(raw_gid) if raw_gid else self.guild_id
                            stream_cid = str(int(srv_id) - 1) if raw_gid else self.channel_id
                            asyncio.create_task(
                                self._voice_ws(session, ep, tok, is_stream=True,
                                               server_id=srv_id, channel_id=stream_cid)
                            )

                    elif event == "STREAM_CREATE":
                        key = data.get("stream_key", "")
                        if self.channel_id in key or self.guild_id in key:
                            self._stream_key = key

                elif op in (7, 9):
                    break

            for t in (gw_hb_task, voice_task):
                if t:
                    t.cancel()

    async def run(self):
        connector = aiohttp.TCPConnector()
        async with aiohttp.ClientSession(connector=connector) as session:
            while True:
                self._voice_endpoint = self._voice_token = None
                self._stream_endpoint = self._stream_token = self._stream_key = None
                self.stream_ready.clear()
                try:
                    await self._run_gateway(session)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("[StreamWatcher] error: %s — reconnecting in 5s", exc)
                await asyncio.sleep(5)


async def start_watching(
    account_id: str,
    token: str,
    guild_id: str,
    channel_id: str,
    streamer_user_id: str | None = None,
    proxy_url: str | None = None,
) -> dict:
    """Start watching a Go Live stream. Returns after stream_ready or timeout."""
    await stop_watching(account_id)

    watcher = StreamWatcher(
        token, guild_id, channel_id,
        proxy=proxy_url,
        streamer_user_id=streamer_user_id,
    )
    _watchers[account_id] = watcher

    task = asyncio.create_task(watcher.run(), name=f"stream-{account_id}")
    _watcher_tasks[account_id] = task

    try:
        await asyncio.wait_for(asyncio.shield(watcher.stream_ready.wait()), timeout=30)
        logger.info("stream_ready for account %s", account_id)
        return {"ok": True}
    except asyncio.TimeoutError:
        # Still running — stream might start later when streamer goes live
        logger.info("stream_ready timeout for %s — staying connected", account_id)
        return {"ok": True, "note": "connected to voice, waiting for streamer to go live"}


async def stop_watching(account_id: str) -> None:
    task = _watcher_tasks.pop(account_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    _watchers.pop(account_id, None)
    logger.info("stream watcher stopped for %s", account_id)
