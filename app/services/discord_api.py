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
_CLIENT_BUILD_NUMBER = 537475


def build_proxy_url(ip: str, port: str, login: str, password: str, scheme: str = "http") -> str:
    """Build a proxy URL aiohttp can use directly via `proxy=`."""
    return f"{scheme}://{login}:{password}@{ip}:{port}"


def _x_super_properties(ua: str) -> str:
    """Base64-encoded JSON fingerprint Discord expects on every request."""
    # Extract Chrome version from UA string or fall back to fixed version.
    chrome_ver = "120.0.0.0"
    if "Chrome/" in ua:
        try:
            chrome_ver = ua.split("Chrome/")[1].split(" ")[0]
        except IndexError:
            pass
    payload = {
        "os": "Windows",
        "browser": "Chrome",
        "device": "",
        "system_locale": "en-US",
        "browser_user_agent": ua,
        "browser_version": chrome_ver,
        "os_version": "10",
        "referrer": "https://discord.com/",
        "referring_domain": "discord.com",
        "release_channel": "stable",
        "client_build_number": _CLIENT_BUILD_NUMBER,
        "client_event_source": None,
    }
    return base64.b64encode(
        _json.dumps(payload, separators=(",", ":")).encode()
    ).decode()


def _headers(token: str) -> dict[str, str]:
    settings = get_settings()
    ua = settings.discord_user_agent
    return {
        "Authorization": token,
        "User-Agent": ua,
        "Content-Type": "application/json",
        "X-Super-Properties": _x_super_properties(ua),
        "X-Discord-Locale": "en-US",
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
    payload: dict[str, Any] = {"content": content}
    if reply_to:
        payload["message_reference"] = {"message_id": reply_to}
    if captcha_key:
        payload["captcha_key"] = captcha_key

    timeout = ClientTimeout(total=settings.discord_http_timeout)
    body: dict[str, Any] | None = None
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=_headers(token), json=payload, proxy=proxy_url) as resp:
                if resp.status in (200, 201):
                    return await resp.json()
                try:
                    body = await resp.json()
                except (aiohttp.ContentTypeError, ValueError):
                    body = None
                logger.info(
                    "send_message status=%s keys=%s",
                    resp.status,
                    list(body.keys()) if isinstance(body, dict) else None,
                )
    except (ClientError, TimeoutError) as exc:
        logger.warning("send_message network error: %s", exc)
        return None

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
    url = (
        f"{settings.discord_api_base}/channels/{channel_id}/messages/{message_id}"
        f"/reactions/{quote(emoji, safe=':')}/@me"
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
    proxy_url: str | None = None,
) -> dict[str, Any] | None:
    """PATCH /users/@me (username/global_name) and /users/@me/profile (bio)."""
    settings = get_settings()
    timeout = ClientTimeout(total=settings.discord_http_timeout)
    result: dict[str, Any] = {}

    user_payload: dict[str, Any] = {}
    if username is not None:
        user_payload["username"] = username
    if global_name is not None:
        user_payload["global_name"] = global_name

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if user_payload:
                u = f"{settings.discord_api_base}/users/@me"
                async with session.patch(u, headers=_headers(token), json=user_payload, proxy=proxy_url) as resp:
                    if resp.status != 200:
                        logger.info("patch /users/@me failed status=%s", resp.status)
                        return None
                    result.update(await resp.json())

            if bio is not None:
                p = f"{settings.discord_api_base}/users/@me/profile"
                async with session.patch(p, headers=_headers(token), json={"bio": bio}, proxy=proxy_url) as resp:
                    if resp.status != 200:
                        logger.info("patch /users/@me/profile failed status=%s", resp.status)
                        return None
                    profile = await resp.json()
                    result["bio"] = profile.get("bio", bio)

        return result
    except (ClientError, TimeoutError) as exc:
        logger.warning("patch_profile network error: %s", exc)
        return None


async def join_invite(
    token: str,
    invite_code: str,
    *,
    captcha_key: str | None = None,
    proxy_url: str | None = None,
    _retry: bool = True,
) -> dict[str, Any] | None:
    """POST /invites/{invite_code} — joins the server. Auto-solves captcha when configured."""
    settings = get_settings()
    url = f"{settings.discord_api_base}/invites/{invite_code}"
    body_payload: dict[str, Any] = {}
    if captcha_key:
        body_payload["captcha_key"] = captcha_key

    timeout = ClientTimeout(total=settings.discord_http_timeout)
    body: dict[str, Any] | None = None
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                headers=_headers(token),
                json=body_payload if body_payload else None,
                proxy=proxy_url,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                # Try to parse the body so we can detect captcha challenges
                try:
                    body = await resp.json()
                except (aiohttp.ContentTypeError, ValueError):
                    body = None
                logger.info(
                    "join_invite status=%s code=%s body_keys=%s",
                    resp.status,
                    invite_code,
                    list(body.keys()) if isinstance(body, dict) else None,
                )
    except (ClientError, TimeoutError) as exc:
        logger.warning("join_invite network error: %s", exc)
        return None

    if _retry and not captcha_key:
        solved = await maybe_solve_for_response(
            body, page_url_hint=f"https://discord.com/invite/{invite_code}"
        )
        if solved:
            logger.info("join_invite retrying with solved captcha")
            return await join_invite(
                token,
                invite_code,
                captcha_key=solved,
                proxy_url=proxy_url,
                _retry=False,
            )
    return None


async def login_with_password(
    email: str,
    password: str,
    *,
    proxy_url: str | None = None,
) -> dict[str, Any] | None:
    """POST /auth/login — mirrors the reference implementation exactly.

    Uses curl_cffi AsyncSession(impersonate='chrome120') so Discord sees a real
    Chrome TLS fingerprint. One session is kept alive for the ENTIRE flow
    (pre-flight → login → captcha solve → retry → MFA is handled by the caller)
    so Discord's session cookies survive the captcha round-trip.

    Possible result shapes:
    - {"token": "...", "user_id": "..."}              → success (no 2FA)
    - {"mfa": True, "ticket": "...", "token": null}   → TOTP required
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

            # No captcha required → return directly
            if "captcha_sitekey" not in data:
                return data

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
            return data2 if isinstance(data2, dict) else None

    except Exception as exc:  # noqa: BLE001
        logger.warning("login_with_password error: %s", exc)
        return None


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
    """POST /users/@me/mfa/totp/enable. Returns {backup_codes, token} on success."""
    settings = get_settings()
    url = f"{settings.discord_api_base}/users/@me/mfa/totp/enable"
    payload = {"code": totp_code, "secret": secret, "password": password}
    timeout = ClientTimeout(total=settings.discord_http_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=_headers(token), json=payload, proxy=proxy_url) as resp:
                if resp.status == 200:
                    return await resp.json()
                body_preview = (await resp.text())[:200]
                logger.info("enable_mfa failed status=%s body=%s", resp.status, body_preview)
                return None
    except (ClientError, TimeoutError) as exc:
        logger.warning("enable_mfa network error: %s", exc)
        return None
