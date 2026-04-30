"""hCaptcha solver abstraction.

Discord challenges with hCaptcha. When a Discord JSON response includes
`captcha_key` (challenge required) and `captcha_sitekey`, we hand it off to
the configured solver and retry the original request with the resulting
token in the body's `captcha_key` field.

Supported providers — pick via env `CAPTCHA_PROVIDER`:
- `capsolver`   (capsolver.com — recommended, AI-based, ~$0.7/1k, ~5–15s)
- `twocaptcha`  (2captcha.com — human-backed, ~$3/1k, slower but proven)
- `capmonster`  (capmonster.cloud — same task shape as anticaptcha, ~$0.6/1k)
- `anticaptcha` (anti-captcha.com — mature, ~$2/1k)
- `disabled` / unset → no solving; functions just return the captcha challenge

All providers expose the same task model: HCaptchaTaskProxyless. We don't pass
the proxy through because most operators don't add proxies to their captcha
account; the captcha needs to *match* the page sitekey, not the IP.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp
from aiohttp import ClientError, ClientTimeout

from app.config import get_settings

logger = logging.getLogger(__name__)


# ── Public dispatcher ──────────────────────────────────────────────────────
async def solve_hcaptcha(sitekey: str, page_url: str) -> str | None:
    """Solve an hCaptcha and return the token string (use as `captcha_key`).

    Returns None if no provider is configured, the API key is missing, or the
    solver failed/timed out.
    """
    settings = get_settings()
    provider = (settings.captcha_provider or "").lower()
    if provider in ("", "disabled", "none", "off"):
        return None
    if not settings.captcha_api_key:
        logger.warning("captcha disabled — CAPTCHA_API_KEY is empty")
        return None

    try:
        if provider == "capsolver":
            return await _solve_anticaptcha_style(
                "https://api.capsolver.com", sitekey, page_url, settings.captcha_api_key
            )
        if provider == "capmonster":
            return await _solve_anticaptcha_style(
                "https://api.capmonster.cloud", sitekey, page_url, settings.captcha_api_key
            )
        if provider == "anticaptcha":
            return await _solve_anticaptcha_style(
                "https://api.anti-captcha.com", sitekey, page_url, settings.captcha_api_key
            )
        if provider in ("2captcha", "twocaptcha"):
            return await _solve_twocaptcha(sitekey, page_url, settings.captcha_api_key)
    except (ClientError, TimeoutError) as exc:
        logger.warning("captcha solver network error: %s", exc)
        return None

    logger.warning("Unknown CAPTCHA_PROVIDER=%r — disabling solver", provider)
    return None


# ── Anti-Captcha / CapMonster / CapSolver share one schema ────────────────
async def _solve_anticaptcha_style(
    base_url: str,
    sitekey: str,
    page_url: str,
    api_key: str,
) -> str | None:
    settings = get_settings()
    create_payload = {
        "clientKey": api_key,
        "task": {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": sitekey,
        },
    }
    timeout = ClientTimeout(total=settings.captcha_timeout)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(f"{base_url}/createTask", json=create_payload) as r:
            data = await r.json()
        if not isinstance(data, dict) or data.get("errorId"):
            logger.warning("captcha createTask failed: %s", data)
            return None
        task_id = data.get("taskId")
        if not task_id:
            return None

        for attempt in range(settings.captcha_poll_attempts):
            await asyncio.sleep(settings.captcha_poll_interval)
            async with s.post(
                f"{base_url}/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
            ) as r:
                data = await r.json()
            if not isinstance(data, dict):
                continue
            status = data.get("status")
            if status == "ready":
                return ((data.get("solution") or {}).get("gRecaptchaResponse"))
            if data.get("errorId"):
                logger.warning("captcha solver failed: %s", data)
                return None

    logger.warning("captcha solver timed out (task=%s)", task_id)
    return None


# ── 2Captcha (different schema) ───────────────────────────────────────────
async def _solve_twocaptcha(
    sitekey: str, page_url: str, api_key: str
) -> str | None:
    settings = get_settings()
    timeout = ClientTimeout(total=settings.captcha_timeout)
    base = "https://2captcha.com"
    submit = (
        f"{base}/in.php?key={api_key}&method=hcaptcha"
        f"&sitekey={sitekey}&pageurl={page_url}&json=1"
    )
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(submit) as r:
            data = await r.json()
        if not isinstance(data, dict) or data.get("status") != 1:
            logger.warning("2captcha submit failed: %s", data)
            return None
        request_id = data.get("request")

        for attempt in range(settings.captcha_poll_attempts):
            await asyncio.sleep(settings.captcha_poll_interval)
            async with s.get(
                f"{base}/res.php?key={api_key}&action=get&id={request_id}&json=1"
            ) as r:
                data = await r.json()
            if not isinstance(data, dict):
                continue
            if data.get("status") == 1:
                return data.get("request")
            if data.get("request") and data["request"] != "CAPCHA_NOT_READY":
                logger.warning("2captcha solver error: %s", data)
                return None

    logger.warning("2captcha solver timed out (request=%s)", request_id)
    return None


# ── Helper used by discord_api.py to opportunistically solve a challenge ──
async def maybe_solve_for_response(
    body: object,
    *,
    page_url_hint: str = "https://discord.com/login",
) -> str | None:
    """If `body` is a Discord JSON response carrying a captcha challenge, hand
    off to the solver and return the resulting token. Otherwise return None.

    Caller is expected to retry the original request with `captcha_key=<token>`
    in the request body when a token is returned.
    """
    if not isinstance(body, dict):
        return None
    if not body.get("captcha_key"):
        return None
    sitekey = body.get("captcha_sitekey")
    if not sitekey:
        return None
    return await solve_hcaptcha(sitekey, page_url_hint)
