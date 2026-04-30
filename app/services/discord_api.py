"""Async Discord HTTP helpers — token validation, basic profile fetch.

All requests go through the proxy assigned to the account when one is provided.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
from aiohttp import ClientError, ClientTimeout

from app.config import get_settings

logger = logging.getLogger(__name__)


def build_proxy_url(ip: str, port: str, login: str, password: str, scheme: str = "http") -> str:
    """Build a proxy URL aiohttp can use directly via `proxy=`."""
    return f"{scheme}://{login}:{password}@{ip}:{port}"


def _headers(token: str) -> dict[str, str]:
    settings = get_settings()
    return {
        "Authorization": token,
        "User-Agent": settings.discord_user_agent,
        "Content-Type": "application/json",
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
    proxy_url: str | None = None,
) -> dict[str, Any] | None:
    """POST /channels/{channel_id}/messages. Returns created message dict or None."""
    settings = get_settings()
    url = f"{settings.discord_api_base}/channels/{channel_id}/messages"
    payload: dict[str, Any] = {"content": content}
    if reply_to:
        payload["message_reference"] = {"message_id": reply_to}

    timeout = ClientTimeout(total=settings.discord_http_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=_headers(token), json=payload, proxy=proxy_url) as resp:
                if resp.status in (200, 201):
                    return await resp.json()
                body_preview = (await resp.text())[:200]
                logger.info("send_message failed status=%s body=%s", resp.status, body_preview)
                return None
    except (ClientError, TimeoutError) as exc:
        logger.warning("send_message network error: %s", exc)
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
    proxy_url: str | None = None,
) -> dict[str, Any] | None:
    """POST /invites/{invite_code} — joins the server."""
    settings = get_settings()
    url = f"{settings.discord_api_base}/invites/{invite_code}"
    timeout = ClientTimeout(total=settings.discord_http_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=_headers(token), proxy=proxy_url) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.info("join_invite failed status=%s code=%s", resp.status, invite_code)
                return None
    except (ClientError, TimeoutError) as exc:
        logger.warning("join_invite network error: %s", exc)
        return None


async def login_with_password(
    email: str,
    password: str,
    *,
    captcha_key: str | None = None,
    proxy_url: str | None = None,
) -> dict[str, Any] | None:
    """POST /auth/login. Returns Discord's response dict (token, ticket, captcha info)
    or None on transport failure. Does NOT require an existing token.

    Possible result shapes (HTTP 200 or 400):
    - {"token": "...", "user_id": "..."}                 → success
    - {"mfa": True, "ticket": "...", "token": null}      → TOTP required
    - {"captcha_key": [...], "captcha_sitekey": "..."}   → captcha required (not solvable here)
    """
    settings = get_settings()
    url = f"{settings.discord_api_base}/auth/login"
    payload: dict[str, Any] = {
        "login": email,
        "password": password,
        "undelete": False,
        "login_source": None,
        "gift_code_sku_id": None,
    }
    if captcha_key:
        payload["captcha_key"] = captcha_key

    headers = {
        "User-Agent": settings.discord_user_agent,
        "Content-Type": "application/json",
    }
    timeout = ClientTimeout(total=settings.discord_http_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload, proxy=proxy_url) as resp:
                if resp.status not in (200, 400):
                    logger.info("login_with_password unexpected status=%s", resp.status)
                    return None
                body = await resp.json()
                if isinstance(body, dict):
                    logger.info(
                        "login_with_password status=%s keys=%s",
                        resp.status,
                        list(body.keys()),
                    )
                    return body
                return None
    except (ClientError, TimeoutError) as exc:
        logger.warning("login_with_password network error: %s", exc)
        return None


async def mfa_totp(
    ticket: str,
    code: str,
    *,
    proxy_url: str | None = None,
) -> dict[str, Any] | None:
    """POST /auth/mfa/totp — exchange MFA ticket + TOTP code for an auth token."""
    settings = get_settings()
    url = f"{settings.discord_api_base}/auth/mfa/totp"
    payload = {
        "code": code,
        "ticket": ticket,
        "login_source": None,
        "gift_code_sku_id": None,
    }
    headers = {
        "User-Agent": settings.discord_user_agent,
        "Content-Type": "application/json",
    }
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
