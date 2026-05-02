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


async def authorize_ip(token: str, *, proxy_url: str | None = None) -> bool:
    """POST /auth/authorize-ip with the token extracted from the email link.

    This is the correct endpoint for new-device verification. The flow from
    requests.md is:
    1. GET click.discord.com/... → follows redirects → lands on
       discord.com/authorize-ip#token=<TOKEN>
    2. Extract token from the URL fragment.
    3. POST /auth/authorize-ip with {token: <TOKEN>}.
    """
    settings = get_settings()
    url = f"{settings.discord_api_base}/auth/authorize-ip"
    headers = {
        "User-Agent": settings.discord_user_agent,
        "Content-Type": "application/json",
    }
    timeout = ClientTimeout(total=settings.discord_http_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, headers=headers, json={"token": token}, proxy=proxy_url) as resp:
                ok = resp.status < 400
                logger.info("authorize_ip status=%s", resp.status)
                return ok
    except (ClientError, TimeoutError) as exc:
        logger.warning("authorize_ip network error: %s", exc)
        return False


async def follow_verify_link(url: str, *, proxy_url: str | None = None) -> bool:
    """Follow a Discord email link and call /auth/authorize-ip IF it's a device-verify link.

    Returns True only when the link led to authorize-ip and the POST succeeded.
    Returns False if the link is a reset/other link — caller should try a different email.
    """
    from curl_cffi.requests import AsyncSession

    settings = get_settings()
    proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else None
    final_url: str = url

    try:
        async with AsyncSession(impersonate="chrome124") as s:
            resp = await s.get(url, proxies=proxies, allow_redirects=True)
            final_url = str(resp.url)
            logger.info("follow_verify_link final_url=%.120s", final_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("follow_verify_link GET error: %s", exc)
        final_url = url

    # Only proceed if this is a device-authorization link — not a reset/other link.
    if "authorize-ip" not in final_url:
        logger.warning(
            "follow_verify_link: not an authorize-ip link (got %s…) — skipping",
            final_url[:80],
        )
        return False

    token = extract_token_from_url(final_url)
    if not token:
        logger.warning("follow_verify_link: no token in URL %s", final_url[:100])
        return False

    return await authorize_ip(token, proxy_url=proxy_url)


# Keep old name as alias for any callers.
async def follow_link(url: str, *, proxy_url: str | None = None) -> bool:
    return await follow_verify_link(url, proxy_url=proxy_url)


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


async def find_and_authorize_ip(
    email: str, password: str, *, proxy_url: str | None = None
) -> bool:
    """Try every link in the recent Discord inbox until one leads to authorize-ip.

    Needed because multiple Discord emails may be present (reset, verify, etc.)
    and the IMAP fetcher returns them newest-first. We follow each link and skip
    any that don't resolve to discord.com/authorize-ip#token=...
    """
    try:
        entries = await fetch_recent(email, password, limit=10, only_discord=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("find_and_authorize_ip IMAP error: %s", exc)
        return False

    for e in entries:
        link = e.get("link")
        if not link:
            continue
        logger.info("find_and_authorize_ip trying link subject=%r", e.get("subject", "?")[:60])
        if await follow_verify_link(link, proxy_url=proxy_url):
            return True
    logger.warning("find_and_authorize_ip: no authorize-ip link found in %d entries", len(entries))
    return False


async def full_verify_account(
    auth_token: str,
    email: str,
    mail_password: str,
    *,
    proxy_url: str | None = None,
) -> str | None:
    """Verify Discord account email via IMAP.

    Flow:
    1. POST /auth/verify/resend — triggers verification email
    2. Wait 7s then poll IMAP for the email with a discord.com/verify link
    3. Follow click.discord.com redirect → extract token from #token= fragment
    4. POST /auth/verify {token} — with captcha auto-solve if needed
    5. Returns the NEW Discord auth token on success (Discord rotates it), or None.
    """
    from app.services.discord_api import verify_resend, verify_with_token

    logger.info("full_verify_account: sending verification email to %s", email)

    if not await verify_resend(auth_token, proxy_url=proxy_url):
        logger.warning("full_verify_account: verify/resend failed")
        return None

    # Wait for email, then try multiple times
    import asyncio
    await asyncio.sleep(7)

    for attempt in range(4):
        if attempt > 0:
            await asyncio.sleep(5)
        try:
            entries = await fetch_recent(email, mail_password, limit=10, only_discord=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("full_verify_account IMAP error: %s", exc)
            continue

        for e in entries:
            link = e.get("link")
            if not link:
                continue
            # Follow the click link → we need discord.com/verify#token= (not authorize-ip)
            from curl_cffi.requests import AsyncSession
            settings = get_settings()
            proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else None
            final_url = link
            try:
                async with AsyncSession(impersonate="chrome124") as s:
                    resp = await s.get(link, proxies=proxies, allow_redirects=True)
                    final_url = str(resp.url)
            except Exception:  # noqa: BLE001
                pass

            logger.info("full_verify_account link → %s", final_url[:120])
            if "verify" not in final_url:
                continue

            token = extract_token_from_url(final_url)
            if not token:
                continue

            result = await verify_with_token(auth_token, token, proxy_url=proxy_url)
            if result and isinstance(result, dict):
                new_token = result.get("token")
                if new_token:
                    logger.info("full_verify_account: email verified, new token received")
                    return new_token
                # 200 but no token — verification still succeeded
                logger.info("full_verify_account: verified (no token rotation)")
                return auth_token

    logger.warning("full_verify_account: no verify link found after retries")
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
