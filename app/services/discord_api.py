"""Async Discord HTTP helpers — token validation, basic profile fetch.

All requests go through the proxy assigned to the account when one is provided.
Captcha challenges are auto-solved via `app.services.captcha` when enabled.
"""
from __future__ import annotations

import asyncio
import base64
import json as _json
import logging
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import ClientError, ClientTimeout

from app.config import get_settings
from app.services.captcha import maybe_solve_for_response, solve_hcaptcha

logger = logging.getLogger(__name__)

# Discord build number used in X-Super-Properties.
# Update this if Discord starts rejecting the fingerprint.
_CLIENT_BUILD_NUMBER = 539147


def build_proxy_url(ip: str, port: str, login: str, password: str, scheme: str = "http") -> str:
    """Build a proxy URL aiohttp can use directly via `proxy=`."""
    return f"{scheme}://{login}:{password}@{ip}:{port}"


def _x_super_properties(ua: str) -> str:
    """Base64-encoded JSON fingerprint Discord expects on every request."""
    import uuid
    chrome_ver = "147.0.0.0"
    if "Chrome/" in ua:
        try:
            chrome_ver = ua.split("Chrome/")[1].split(" ")[0]
        except IndexError:
            pass
    payload = {
        "os": "Windows",
        "browser": "Chrome",
        "device": "",
        "system_locale": "ru-RU",
        "has_client_mods": False,
        "browser_user_agent": ua,
        "browser_version": chrome_ver,
        "os_version": "10",
        "referrer": "https://www.google.com/",
        "referring_domain": "www.google.com",
        "search_engine": "google",
        "referrer_current": "https://discord.com/",
        "referring_domain_current": "discord.com",
        "release_channel": "stable",
        "client_build_number": _CLIENT_BUILD_NUMBER,
        "client_event_source": None,
        "client_launch_id": str(uuid.uuid4()),
        "launch_signature": str(uuid.uuid4()),
        "client_heartbeat_session_id": str(uuid.uuid4()),
        "client_app_state": "focused",
    }
    return base64.b64encode(
        _json.dumps(payload, separators=(",", ":")).encode()
    ).decode()


# Per-process installation ID (stable within one session)
import os as _os, base64 as _b64, time as _time
_INSTALLATION_ID = (
    "1497753024896958545."
    + _b64.urlsafe_b64encode(_os.urandom(16)).decode().rstrip("=")
)

# Discord epoch for nonce generation
_DISCORD_EPOCH = 1420070400000

_CTX_CHAT_INPUT = base64.b64encode(b'{"location":"chat_input"}').decode()


def _make_nonce() -> str:
    """Generate a Discord snowflake nonce from current timestamp.
    Without this Discord may flag the message as suspicious."""
    return str((int(_time.time() * 1000) - _DISCORD_EPOCH) << 22)


def _headers(token: str) -> dict[str, str]:
    settings = get_settings()
    ua = settings.discord_user_agent
    return {
        "Authorization": token,
        "User-Agent": ua,
        "Content-Type": "application/json",
        "X-Super-Properties": _x_super_properties(ua),
        "X-Installation-ID": _INSTALLATION_ID,
        "X-Discord-Locale": "en-US",
        "X-Discord-Timezone": "Europe/Kiev",
        "X-Debug-Options": "bugReporterEnabled",
        "Origin": "https://discord.com",
        "Referer": "https://discord.com/channels/@me",
    }


def _login_headers(ua: str) -> dict[str, str]:
    """Headers for /auth/login — no Authorization, Referer points to login page."""
    return {
        "User-Agent": ua,
        "Content-Type": "application/json",
        "X-Super-Properties": _x_super_properties(ua),
        "X-Discord-Locale": "en-US",
        "Origin": "https://discord.com",
        "Referer": "https://discord.com/login",
    }


async def validate_token(
    token: str,
    proxy_url: str | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Hit `GET /users/@me`. Returns (is_valid, user_payload_or_none).

    Network errors and non-2xx responses are treated as invalid.
    """
    settings = get_settings()
    url = f"{settings.discord_api_base}/users/@me"
    timeout = ClientTimeout(total=settings.discord_http_timeout)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=_headers(token), proxy=proxy_url) as resp:
                if resp.status == 200:
                    return True, await resp.json()
                logger.info("Token validation failed (status=%s)", resp.status)
                return False, None
    except (ClientError, TimeoutError) as exc:
        logger.warning("Token validation network error: %s", exc)
        return False, None


async def check_needs_verification(
    token: str,
    *,
    proxy_url: str | None = None,
) -> bool:
    """GET /users/@me/referrals/eligibility — returns True if the account needs
    email verification (code 40002)."""
    settings = get_settings()
    url = f"{settings.discord_api_base}/users/@me/referrals/eligibility"
    timeout = ClientTimeout(total=settings.discord_http_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url, headers=_headers(token), proxy=proxy_url) as resp:
                if resp.status == 400:
                    data = await resp.json()
                    return isinstance(data, dict) and data.get("code") == 40002
                return False
    except (ClientError, TimeoutError) as exc:
        logger.warning("check_needs_verification error: %s", exc)
        return False


async def verify_resend(
    token: str,
    *,
    proxy_url: str | None = None,
) -> bool:
    """POST /auth/verify/resend — trigger Discord to send a verification email."""
    settings = get_settings()
    url = f"{settings.discord_api_base}/auth/verify/resend"
    timeout = ClientTimeout(total=settings.discord_http_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, headers=_headers(token), proxy=proxy_url) as resp:
                ok = resp.status < 400
                logger.info("verify_resend status=%s", resp.status)
                return ok
    except (ClientError, TimeoutError) as exc:
        logger.warning("verify_resend error: %s", exc)
        return False


async def get_or_create_dm_channel(
    token: str,
    recipient_id: str,
    *,
    proxy_url: str | None = None,
) -> str | None:
    """POST /users/@me/channels {recipient_id} — get or create a 1:1 DM channel.
    Returns the channel id string, or None on failure.
    """
    settings = get_settings()
    url = f"{settings.discord_api_base}/users/@me/channels"
    timeout = ClientTimeout(total=settings.discord_http_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(
                url,
                headers=_headers(token),
                json={"recipient_id": recipient_id},
                proxy=proxy_url,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("id")
                logger.info("get_or_create_dm_channel status=%s", resp.status)
                return None
    except (ClientError, TimeoutError) as exc:
        logger.warning("get_or_create_dm_channel error: %s", exc)
        return None


async def verify_with_token(
    auth_token: str,
    verify_token: str,
    *,
    proxy_url: str | None = None,
    _retry: bool = True,
) -> dict[str, Any] | None:
    """POST /auth/verify {token} — confirm email ownership.
    Auto-solves captcha if challenged. Returns Discord response (may contain new auth token)."""
    settings = get_settings()
    url = f"{settings.discord_api_base}/auth/verify"
    payload: dict[str, Any] = {"token": verify_token}
    timeout = ClientTimeout(total=settings.discord_http_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, headers=_headers(auth_token), json=payload, proxy=proxy_url) as resp:
                data = await resp.json()
                if resp.status == 200:
                    return data
                if _retry and isinstance(data, dict) and data.get("captcha_sitekey"):
                    rqdata = data.get("captcha_rqdata") or None
                    rqtoken = data.get("captcha_rqtoken", "")
                    solved = await solve_hcaptcha(
                        data["captcha_sitekey"],
                        "https://discord.com/verify",
                        rqdata=rqdata,
                        proxy_url=proxy_url,
                    )
                    if solved:
                        retry_payload = {**payload, "captcha_key": solved}
                        if rqtoken:
                            retry_payload["captcha_rqtoken"] = rqtoken
                        async with s.post(url, headers=_headers(auth_token), json=retry_payload, proxy=proxy_url) as r2:
                            data2 = await r2.json()
                            if r2.status == 200:
                                return data2
                            logger.info("verify_with_token captcha retry status=%s body=%.200s", r2.status, str(data2))
                            return None
                logger.info("verify_with_token status=%s body=%.200s", resp.status, str(data))
                return None
    except (ClientError, TimeoutError) as exc:
        logger.warning("verify_with_token error: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Phase-2 actions (chat / reactions / profile / invite / mfa)
# ─────────────────────────────────────────────────────────────────────────────


async def send_message(
    token: str,
    channel_id: str,
    content: str,
    *,
    reply_to: str | None = None,
    captcha_key: str | None = None,
    proxy_url: str | None = None,
    _retry: bool = True,
) -> dict[str, Any] | None:
    """POST /channels/{channel_id}/messages. Returns created message dict or None.
    Auto-solves captcha when challenged and a solver is configured."""
    settings = get_settings()
    url = f"{settings.discord_api_base}/channels/{channel_id}/messages"

    # Payload matches real browser: nonce, mobile_network_type, tts, flags required
    payload: dict[str, Any] = {
        "content": content,
        "nonce": _make_nonce(),
        "tts": False,
        "flags": 0,
        "mobile_network_type": "unknown",
    }
    if reply_to:
        payload["message_reference"] = {"message_id": reply_to}
    if captcha_key:
        payload["captcha_key"] = captcha_key

    # Headers: proper Referer + x-context-properties for channel messages
    headers = {
        **_headers(token),
        "Referer": f"https://discord.com/channels/@me/{channel_id}",
        "X-Context-Properties": _CTX_CHAT_INPUT,
    }

    timeout = ClientTimeout(total=settings.discord_http_timeout)
    body: dict[str, Any] | None = None
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload, proxy=proxy_url) as resp:
                if resp.status in (200, 201):
                    return await resp.json()
                try:
                    body = await resp.json()
                except (aiohttp.ContentTypeError, ValueError):
                    body = None
                logger.info(
                    "send_message status=%s keys=%s body=%.200s",
                    resp.status,
                    list(body.keys()) if isinstance(body, dict) else None,
                    str(body),
                )
    except (ClientError, TimeoutError) as exc:
        logger.warning("send_message network error: %s", exc)
        return None

    # Surface known Discord error codes back to callers via _discord_error key
    if isinstance(body, dict) and body.get("code"):
        return {"_discord_error": True, "code": body["code"], "message": body.get("message", "")}

    if _retry and not captcha_key:
        solved = await maybe_solve_for_response(
            body, page_url_hint=f"https://discord.com/channels/@me/{channel_id}"
        )
        if solved:
            logger.info("send_message retrying with solved captcha")
            return await send_message(
                token,
                channel_id,
                content,
                reply_to=reply_to,
                captcha_key=solved,
                proxy_url=proxy_url,
                _retry=False,
            )
    return None


async def send_message_with_files(
    token: str,
    channel_id: str,
    content: str,
    files: list[tuple[str, bytes, str | None]],
    *,
    reply_to: str | None = None,
    proxy_url: str | None = None,
) -> dict[str, Any] | None:
    """POST /channels/{id}/messages as `multipart/form-data`.

    `files` is a list of `(filename, bytes, mimetype_or_None)`. Discord pairs
    them with `payload_json` — the same JSON body `send_message` uses, just
    delivered alongside the file parts.
    """
    settings = get_settings()
    url = f"{settings.discord_api_base}/channels/{channel_id}/messages"

    payload: dict[str, Any] = {"content": content}
    if reply_to:
        payload["message_reference"] = {"message_id": reply_to}

    data = aiohttp.FormData()
    data.add_field("payload_json", _json.dumps(payload), content_type="application/json")
    for i, (filename, blob, mime) in enumerate(files):
        data.add_field(
            f"files[{i}]",
            blob,
            filename=filename or f"file{i}",
            content_type=mime or "application/octet-stream",
        )

    # Don't set Content-Type — aiohttp builds the multipart boundary itself.
    headers = {
        "Authorization": token,
        "User-Agent": settings.discord_user_agent,
    }
    timeout = ClientTimeout(total=settings.discord_http_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, data=data, proxy=proxy_url) as resp:
                if resp.status in (200, 201):
                    return await resp.json()
                body_preview = (await resp.text())[:200]
                logger.info(
                    "send_message_with_files failed status=%s body=%s",
                    resp.status,
                    body_preview,
                )
                return None
    except (ClientError, TimeoutError) as exc:
        logger.warning("send_message_with_files network error: %s", exc)
        return None


async def check_username(
    token: str,
    username: str,
    *,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    """POST /users/@me/pomelo-attempt — returns {taken: bool}."""
    settings = get_settings()
    url = f"{settings.discord_api_base}/users/@me/pomelo-attempt"
    timeout = ClientTimeout(total=settings.discord_http_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                headers=_headers(token),
                json={"username": username},
                proxy=proxy_url,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return {"taken": True, "status": resp.status}
    except (ClientError, TimeoutError) as exc:
        logger.warning("check_username network error: %s", exc)
        return {"taken": True, "error": str(exc)}


async def add_reaction(
    token: str,
    channel_id: str,
    message_id: str,
    emoji: str,
    *,
    proxy_url: str | None = None,
) -> bool:
    """PUT /channels/.../messages/.../reactions/{emoji}/@me. emoji is unicode or 'name:id'."""
    from urllib.parse import quote

    settings = get_settings()
    # Include location and type=0 params as real Discord client sends
    url = (
        f"{settings.discord_api_base}/channels/{channel_id}/messages/{message_id}"
        f"/reactions/{quote(emoji, safe=':')}/@me"
        "?location=Message%20Context%20Menu&type=0"
    )
    timeout = ClientTimeout(total=settings.discord_http_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.put(url, headers=_headers(token), proxy=proxy_url) as resp:
                if resp.status in (200, 204):
                    return True
                logger.info("add_reaction failed status=%s", resp.status)
                return False
    except (ClientError, TimeoutError) as exc:
        logger.warning("add_reaction network error: %s", exc)
        return False


async def patch_profile(
    token: str,
    *,
    username: str | None = None,
    global_name: str | None = None,
    bio: str | None = None,
    password: str | None = None,
    proxy_url: str | None = None,
) -> dict[str, Any] | None:
    """PATCH /users/@me (username/global_name) and /users/@me/profile (bio).

    Uses curl_cffi Chrome impersonation because Discord challenges profile edits
    with captcha on plain aiohttp (returns 400 captcha_required).
    Auto-solves captcha on the /users/@me PATCH if a solver is configured.
    """
    from curl_cffi.requests import AsyncSession

    settings = get_settings()
    api = settings.discord_api_base
    headers = _headers(token)
    proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else None
    result: dict[str, Any] = {}

    user_payload: dict[str, Any] = {}
    if username is not None:
        user_payload["username"] = username
        # Discord requires password when changing username
        if password:
            user_payload["password"] = password
    if global_name is not None:
        user_payload["global_name"] = global_name

    try:
        async with AsyncSession(impersonate="chrome124") as session:
            if user_payload:
                u = f"{api}/users/@me"
                resp = await session.patch(u, headers=headers, json=user_payload, proxies=proxies)
                body = resp.json() if resp.text else {}

                # Auto-solve captcha (same pattern as login flow)
                if resp.status_code == 400 and isinstance(body, dict) and body.get("captcha_sitekey"):
                    rqdata = body.get("captcha_rqdata") or None
                    rqtoken = body.get("captcha_rqtoken", "")
                    solved = await solve_hcaptcha(
                        body["captcha_sitekey"],
                        f"{api}/users/@me",
                        rqdata=rqdata,
                        proxy_url=proxy_url,
                    )
                    if solved:
                        retry_payload = {**user_payload, "captcha_key": solved}
                        if rqtoken:
                            retry_payload["captcha_rqtoken"] = rqtoken
                        await asyncio.sleep(1)
                        resp = await session.patch(u, headers=headers, json=retry_payload, proxies=proxies)
                        body = resp.json() if resp.text else {}

                if resp.status_code != 200:
                    logger.info("patch /users/@me failed status=%s body=%.200s", resp.status_code, str(body))
                    return None
                result.update(body)

            if bio is not None:
                p = f"{api}/users/@me/profile"
                resp = await session.patch(p, headers=headers, json={"bio": bio}, proxies=proxies)
                if resp.status_code != 200:
                    logger.info("patch /users/@me/profile failed status=%s", resp.status_code)
                    return None
                profile = resp.json()
                result["bio"] = profile.get("bio", bio)

        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("patch_profile error: %s", exc)
        return None


async def join_invite(
    token: str,
    invite_code: str,
    *,
    session_id: str | None = None,
    captcha_key: str | None = None,
    captcha_rqtoken: str | None = None,
    proxy_url: str | None = None,
) -> dict[str, Any] | None:
    """POST /invites/{invite_code} — joins the server using curl_cffi (Chrome TLS).

    From requests.md: payload always contains session_id (gateway WS session id or null).
    From join_server.py reference: uses AsyncSession(impersonate='chrome120') with preflight GET.
    """
    from curl_cffi.requests import AsyncSession

    settings = get_settings()
    api = settings.discord_api_base
    # Referer for invite join: /invite/{code}/login (as seen in real browser DevTools)
    invite_page = f"https://discord.com/invite/{invite_code}"
    headers = {**_headers(token), "Referer": f"{invite_page}/login"}
    proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else None
    invite_url = f"{api}/invites/{invite_code}"

    # Base payload — session_id is always sent (None if no gateway session available)
    base_payload: dict[str, Any] = {"session_id": session_id}

    try:
        async with AsyncSession(impersonate="chrome124") as session:
            # Preflight GET — get invite info (guild_id, channel_id) for x-context-properties
            guild_id: str = ""
            channel_id: str = ""
            channel_type: int = 0
            try:
                preflight_resp = await session.get(
                    invite_url,
                    params={
                        "inputValue": f"https://discord.gg/{invite_code}",
                        "with_counts": "true",
                        "with_expiration": "true",
                        "with_permissions": "true",
                    },
                    headers=headers,
                    proxies=proxies,
                )
                invite_info = preflight_resp.json() if preflight_resp.text else {}
                if preflight_resp.status_code != 200:
                    logger.info(
                        "join_invite preflight status=%s body=%.200s",
                        preflight_resp.status_code, str(invite_info),
                    )
                    # 40002 on preflight = unverified account — signal back to router
                    if isinstance(invite_info, dict) and invite_info.get("code") == 40002:
                        return {"_needs_verification": True}
                guild_id = invite_info.get("guild_id") or (invite_info.get("guild") or {}).get("id", "")
                channel = invite_info.get("channel") or {}
                channel_id = channel.get("id", "")
                channel_type = channel.get("type", 0)
                logger.info(
                    "join_invite preflight status=%s guild_id=%s channel_id=%s",
                    preflight_resp.status_code, guild_id, channel_id,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("join_invite preflight error: %s", e)
            await asyncio.sleep(1)

            # x-context-properties — required by Discord for invite joins
            ctx_props = _json.dumps({
                "location": "Accept Invite Page",
                "location_guild_id": guild_id,
                "location_channel_id": channel_id,
                "location_channel_type": channel_type,
            }, separators=(",", ":"))
            post_headers = {
                **headers,
                "X-Context-Properties": base64.b64encode(ctx_props.encode()).decode(),
            }

            # POST — accept invite
            resp = await session.post(invite_url, json=base_payload, headers=post_headers, proxies=proxies)
            body = resp.json() if resp.text else {}

            if resp.status_code in (200, 201):
                logger.info("join_invite OK code=%s new_member=%s", invite_code, body.get("new_member"))
                return body

            logger.info(
                "join_invite status=%s code=%s body=%.200s",
                resp.status_code, invite_code, str(body),
            )

            # Surface 40002 (unverified account) so the router can trigger verification
            if isinstance(body, dict) and body.get("code") == 40002:
                return {"_needs_verification": True}

            # Auto-solve captcha — up to 3 rounds (Discord sometimes chains challenges).
            # IMPORTANT: for /invites, captcha token goes in X-Captcha-Key HEADER
            # (not in the body as captcha_key — that's only for /auth/login).
            current_body = body
            for _attempt in range(3):
                if not (isinstance(current_body, dict) and current_body.get("captcha_sitekey")):
                    break
                rqdata = current_body.get("captcha_rqdata") or None
                logger.info(
                    "join_invite captcha attempt=%d has_proxy=%s",
                    _attempt + 1, bool(proxy_url),
                )
                solved = await solve_hcaptcha(
                    current_body["captcha_sitekey"],
                    f"https://discord.com/invite/{invite_code}",
                    rqdata=rqdata,
                    proxy_url=proxy_url,
                )
                if not solved:
                    logger.warning("join_invite: captcha solve failed at attempt %d", _attempt + 1)
                    break
                rqtoken = current_body.get("captcha_rqtoken", "")
                logger.info("join_invite: captcha solved attempt=%d, retrying with X-Captcha-Key + X-Captcha-Rqtoken headers", _attempt + 1)
                # Both X-Captcha-Key AND X-Captcha-Rqtoken go as headers.
                # Body must be {} (empty) — confirmed from browser DevTools.
                captcha_headers = {**post_headers, "X-Captcha-Key": solved}
                if rqtoken:
                    captcha_headers["X-Captcha-Rqtoken"] = rqtoken
                await asyncio.sleep(1)
                resp_n = await session.post(invite_url, json={}, headers=captcha_headers, proxies=proxies)
                body_n = resp_n.json() if resp_n.text else {}
                if resp_n.status_code in (200, 201):
                    logger.info("join_invite OK (after captcha attempt=%d) code=%s", _attempt + 1, invite_code)
                    return body_n
                logger.info("join_invite attempt=%d status=%s body=%.200s", _attempt + 1, resp_n.status_code, str(body_n))
                if isinstance(body_n, dict) and body_n.get("code") == 40002:
                    return {"_needs_verification": True}
                current_body = body_n  # use fresh challenge for next attempt

        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("join_invite error: %s", exc)
        return None


async def login_with_password(
    email: str,
    password: str,
    *,
    two_fa_secret: str | None = None,
    proxy_url: str | None = None,
) -> dict[str, Any] | None:
    """POST /auth/login — mirrors the reference implementation exactly.

    Uses curl_cffi AsyncSession(impersonate='chrome124') — ONE session for the
    full flow: pre-flight → login → captcha solve → retry → MFA TOTP.
    All steps share the same session cookies.

    `two_fa_secret` — pyotp base32 secret; when supplied and Discord returns an
    MFA ticket, the TOTP code is generated and submitted in the SAME session.

    Possible result shapes:
    - {"token": "...", "user_id": "..."}              → success
    - {"captcha_key": [...], "captcha_sitekey": "..."} → captcha — returned only
                                                          when solver is disabled
                                                          or solve failed
    """
    from curl_cffi.requests import AsyncSession

    settings = get_settings()
    api_url = f"{settings.discord_api_base}/auth/login"
    proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else None

    base_payload: dict[str, Any] = {
        "login": email,
        "password": password,
        "undelete": False,
        "login_source": None,
        "gift_code_sku_id": None,
    }

    try:
        async with AsyncSession(impersonate="chrome124") as session:
            # Pre-flight — associates session cookies with a real browser visit
            try:
                await session.get("https://discord.com/login", proxies=proxies)
            except Exception:  # noqa: BLE001 — non-fatal
                pass
            await asyncio.sleep(1)

            # First login attempt
            resp = await session.post(
                api_url,
                json=base_payload,
                headers={"Content-Type": "application/json"},
                proxies=proxies,
            )
            data: dict[str, Any] = resp.json()
            logger.info(
                "login_with_password status=%s keys=%s",
                resp.status_code,
                list(data.keys()) if isinstance(data, dict) else "?",
            )

            if resp.status_code not in (200, 400) or not isinstance(data, dict):
                return None

            # No captcha required → check for MFA, then return
            if "captcha_sitekey" not in data:
                return await _complete_mfa_in_session(session, data, two_fa_secret, proxies, settings)

            # Solve captcha — still inside the same session (cookies intact)
            sitekey = data.get("captcha_sitekey", "")
            rqdata = data.get("captcha_rqdata", "") or None
            rqtoken = data.get("captcha_rqtoken", "")
            session_id = data.get("captcha_session_id", "")

            logger.info(
                "login_with_password captcha: sitekey=%.12s rqdata=%s rqtoken=%s session_id=%s",
                sitekey, bool(rqdata), bool(rqtoken), bool(session_id),
            )

            solved = await solve_hcaptcha(
                sitekey,
                settings.captcha_default_page_url,
                rqdata=rqdata,
                proxy_url=proxy_url,
            )
            if not solved:
                logger.warning("login_with_password: captcha solve failed/disabled")
                return data  # surface captcha challenge to caller

            logger.info("login_with_password: captcha solved, retrying in same session")

            # Retry — only captcha_key + captcha_rqtoken.
            # Reference (discord-farm/disc/login.py) never sends captcha_session_id.
            retry_payload = {**base_payload, "captcha_key": solved}
            if rqtoken:
                retry_payload["captcha_rqtoken"] = rqtoken

            await asyncio.sleep(1)
            resp = await session.post(
                api_url,
                json=retry_payload,
                headers={"Content-Type": "application/json"},
                proxies=proxies,
            )
            data2: dict[str, Any] = resp.json()
            logger.info(
                "login_with_password (retry) status=%s keys=%s",
                resp.status_code,
                list(data2.keys()) if isinstance(data2, dict) else "?",
            )
            if not isinstance(data2, dict):
                return None

            # Handle MFA after captcha retry (same session)
            return await _complete_mfa_in_session(session, data2, two_fa_secret, proxies, settings)

    except Exception as exc:  # noqa: BLE001
        logger.warning("login_with_password error: %s", exc)
        return None


async def _complete_mfa_in_session(
    session: Any,
    data: dict[str, Any],
    two_fa_secret: str | None,
    proxies: dict | None,
    settings: Any,
) -> dict[str, Any] | None:
    """If `data` is an MFA challenge and we have the 2FA secret, complete TOTP
    in the existing curl_cffi session (required — Discord ties the MFA to the
    login session cookies)."""
    import pyotp

    if not (data.get("mfa") and data.get("ticket")):
        return data  # no MFA, or no ticket — return as-is

    if not two_fa_secret:
        logger.info("login_with_password: MFA required but no 2FA secret provided")
        return data  # caller will see mfa=True and handle manually

    ticket = data["ticket"]
    login_instance_id = data.get("login_instance_id")
    code = pyotp.TOTP(two_fa_secret).now()

    mfa_payload: dict[str, Any] = {
        "code": code,
        "ticket": ticket,
        "login_source": None,
        "gift_code_sku_id": None,
    }
    if login_instance_id:
        mfa_payload["login_instance_id"] = login_instance_id

    # Full headers without Authorization (matches reference login.py _build_headers("") pattern)
    mfa_headers = _login_headers(settings.discord_user_agent)
    # MFA endpoint doesn't use Origin and doesn't require the full login Referer path
    mfa_headers["Referer"] = "https://discord.com/login"
    mfa_headers.pop("Origin", None)

    await asyncio.sleep(1)
    resp = await session.post(
        f"{settings.discord_api_base}/auth/mfa/totp",
        json=mfa_payload,
        headers=mfa_headers,
        proxies=proxies,
    )
    mfa_data = resp.json()
    logger.info(
        "login_with_password MFA status=%s keys=%s body=%.200s",
        resp.status_code,
        list(mfa_data.keys()) if isinstance(mfa_data, dict) else "?",
        str(mfa_data)[:200],
    )
    return mfa_data if isinstance(mfa_data, dict) else None


async def mfa_totp(
    ticket: str,
    code: str,
    *,
    login_instance_id: str | None = None,
    proxy_url: str | None = None,
) -> dict[str, Any] | None:
    """POST /auth/mfa/totp — exchange MFA ticket + TOTP code for an auth token."""
    settings = get_settings()
    url = f"{settings.discord_api_base}/auth/mfa/totp"
    payload: dict[str, Any] = {
        "code": code,
        "ticket": ticket,
        "login_source": None,
        "gift_code_sku_id": None,
    }
    if login_instance_id:
        payload["login_instance_id"] = login_instance_id
    headers = _login_headers(settings.discord_user_agent)
    del headers["Origin"]  # not needed for mfa endpoint
    timeout = ClientTimeout(total=settings.discord_http_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload, proxy=proxy_url) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.info("mfa_totp failed status=%s", resp.status)
                return None
    except (ClientError, TimeoutError) as exc:
        logger.warning("mfa_totp network error: %s", exc)
        return None


async def enable_mfa(
    token: str,
    secret: str,
    totp_code: str,
    password: str,
    *,
    proxy_url: str | None = None,
) -> dict[str, Any] | None:
    """Enable TOTP 2FA using curl_cffi (Chrome TLS fingerprint).

    3-step flow from twofa.py reference + requests.md:
    1. POST /users/@me/mfa/totp/enable {secret, code}
       - 200 → success immediately
       - 401 code 60003 → need password confirmation
    2. POST /mfa/finish {ticket, mfa_type: "password", data: password}
    3. POST /users/@me/mfa/totp/enable {secret, code (fresh)} → {token, backup_codes}
    """
    from curl_cffi.requests import AsyncSession
    import pyotp as _pyotp

    settings = get_settings()
    api = settings.discord_api_base
    headers = _headers(token)
    proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else None

    try:
        async with AsyncSession(impersonate="chrome124") as session:
            # Preflight — match reference pattern (twofa.py does this)
            try:
                await session.get("https://discord.com/login", proxies=proxies)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(1)

            # Step 1
            resp = await session.post(
                f"{api}/users/@me/mfa/totp/enable",
                json={"secret": secret, "code": totp_code},
                headers=headers,
                proxies=proxies,
            )
            data = resp.json()

            if resp.status_code == 200:
                return data

            if not (resp.status_code == 401 and isinstance(data, dict) and data.get("code") == 60003):
                logger.info("enable_mfa step1 failed status=%s body=%.200s", resp.status_code, str(data))
                return None

            ticket = (data.get("mfa") or {}).get("ticket")
            if not ticket:
                logger.warning("enable_mfa: no ticket in 60003 response")
                return None

            await asyncio.sleep(1)

            # Step 2 — verify password
            resp2 = await session.post(
                f"{api}/mfa/finish",
                json={"ticket": ticket, "mfa_type": "password", "data": password},
                headers=headers,
                proxies=proxies,
            )
            if resp2.status_code != 200:
                logger.info(
                    "enable_mfa mfa/finish failed status=%s body=%.200s",
                    resp2.status_code, resp2.text,
                )
                return None

            await asyncio.sleep(1)

            # Step 3 — enable MFA with fresh TOTP code
            new_code = _pyotp.TOTP(secret).now()
            resp3 = await session.post(
                f"{api}/users/@me/mfa/totp/enable",
                json={"secret": secret, "code": new_code},
                headers=headers,
                proxies=proxies,
            )
            if resp3.status_code == 200:
                return resp3.json()
            logger.info(
                "enable_mfa step3 failed status=%s body=%.200s",
                resp3.status_code, resp3.text,
            )
            return None

    except Exception as exc:  # noqa: BLE001
        logger.warning("enable_mfa error: %s", exc)
        return None
