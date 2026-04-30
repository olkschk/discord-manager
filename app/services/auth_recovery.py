"""Discord auth recovery flows on top of `discord_api` and `imap_client`.

- new-device email verification: follow the link Discord mailed, then retry login
- password reset: POST /auth/forgot, fetch the email, POST /auth/reset with the
  token from the email link
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import ClientError, ClientTimeout

from app.config import get_settings
from app.services.imap_client import fetch_recent

logger = logging.getLogger(__name__)


# Token in reset/verify links is in the URL fragment OR query string, e.g.
#   https://discord.com/reset#token=ABC...
#   https://discord.com/verify?token=ABC
_TOKEN_RE = re.compile(r"token=([A-Za-z0-9._\-+/=%]+)", re.IGNORECASE)


def extract_token_from_url(url: str) -> str | None:
    """Pull the `token` query/fragment value out of a Discord verification URL."""
    parts = urlparse(url)
    for src in (parts.fragment, parts.query):
        if not src:
            continue
        m = _TOKEN_RE.search(src)
        if m:
            return m.group(1)
    return None


async def follow_link(url: str, *, proxy_url: str | None = None) -> bool:
    """GET a Discord verification link with a Chromium-ish UA. Used to
    register a new device / authorise a login. Returns True on 2xx/3xx."""
    settings = get_settings()
    headers = {
        "User-Agent": settings.discord_user_agent,
        "Accept": "text/html,application/xhtml+xml",
    }
    timeout = ClientTimeout(total=settings.discord_http_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url, headers=headers, proxy=proxy_url, allow_redirects=True) as resp:
                ok = resp.status < 400
                logger.info("follow_link %s status=%s", url[:80], resp.status)
                return ok
    except (ClientError, TimeoutError) as exc:
        logger.warning("follow_link network error: %s", exc)
        return False


async def fetch_latest_link(
    email: str, password: str, *, must_contain: str | None = None
) -> str | None:
    """Return the latest Discord verification link from inbox, or None."""
    try:
        entries = await fetch_recent(email, password, limit=10, only_discord=True)
    except Exception as exc:  # noqa: BLE001 — bubble up as None
        logger.warning("fetch_latest_link IMAP error: %s", exc)
        return None
    for e in entries:
        link = e.get("link")
        if not link:
            continue
        if must_contain and must_contain not in link:
            continue
        return link
    return None


async def forgot_password(
    email: str, *, proxy_url: str | None = None
) -> bool:
    """POST /auth/forgot — Discord mails a reset link. Returns True on 2xx."""
    settings = get_settings()
    url = f"{settings.discord_api_base}/auth/forgot"
    payload = {"login": email}
    headers = {
        "User-Agent": settings.discord_user_agent,
        "Content-Type": "application/json",
    }
    timeout = ClientTimeout(total=settings.discord_http_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, headers=headers, json=payload, proxy=proxy_url) as resp:
                ok = resp.status < 400
                logger.info("forgot_password status=%s", resp.status)
                return ok
    except (ClientError, TimeoutError) as exc:
        logger.warning("forgot_password network error: %s", exc)
        return False


async def reset_password_with_token(
    token: str, new_password: str, *, proxy_url: str | None = None
) -> dict[str, Any] | None:
    """POST /auth/reset with the token Discord embedded in the email link."""
    settings = get_settings()
    url = f"{settings.discord_api_base}/auth/reset"
    payload = {"token": token, "password": new_password}
    headers = {
        "User-Agent": settings.discord_user_agent,
        "Content-Type": "application/json",
    }
    timeout = ClientTimeout(total=settings.discord_http_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, headers=headers, json=payload, proxy=proxy_url) as resp:
                if resp.status < 400:
                    return await resp.json()
                body_preview = (await resp.text())[:200]
                logger.info("reset_password status=%s body=%s", resp.status, body_preview)
                return None
    except (ClientError, TimeoutError) as exc:
        logger.warning("reset_password network error: %s", exc)
        return None


def needs_email_verification(login_response: dict[str, Any]) -> bool:
    """Detect Discord's 'new location locked' / 'verify by email' shape.

    Discord's exact response varies. Common indicators:
    - errors._errors with code ACCOUNT_LOGIN_VERIFICATION_EMAIL
    - top-level "code" 50035 plus a `message` mentioning verification
    """
    if not isinstance(login_response, dict):
        return False
    errors = login_response.get("errors")
    if isinstance(errors, dict):
        login_block = errors.get("login")
        if isinstance(login_block, dict):
            inner = login_block.get("_errors", [])
            if isinstance(inner, list):
                for e in inner:
                    if isinstance(e, dict) and "VERIFICATION" in str(e.get("code", "")):
                        return True
    msg = str(login_response.get("message", "")).lower()
    return "verify" in msg and "email" in msg
