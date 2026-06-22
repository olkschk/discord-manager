"""hCaptcha solver abstraction — supports hCaptcha Enterprise (Discord's mode).

Discord uses hCaptcha **Enterprise**: its 400 challenge response includes
`captcha_rqdata` (and `captcha_rqtoken`). Solving without passing rqdata back
to the solver produces ERROR_INVALID_TASK_DATA — that's the real cause of
provider "policy violation" errors when targeting Discord.

Integration:
- `solve_hcaptcha(sitekey, page_url, *, rqdata, user_agent)` — public entry
- `maybe_solve_for_response(body, *, page_url_hint)` — used inside discord_api;
  inspects a Discord JSON body and extracts sitekey + rqdata automatically

Supported providers (CAPTCHA_PROVIDER env):
  capsolver    — capsolver.com   (recommended, AI, ~$0.7/1k, ~5–15s)
  twocaptcha   — 2captcha.com    (human-backed, ~$3/1k)
  capmonster   — capmonster.cloud (~$0.6/1k)
  anticaptcha  — anti-captcha.com (~$2/1k)
  disabled / unset — no auto-solve; challenges surface as errors
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote_plus, urlparse

import aiohttp
from app.services.discord_api import _get_session
from aiohttp import ClientError, ClientTimeout

from app.config import get_settings

logger = logging.getLogger(__name__)


# ── Public dispatcher ──────────────────────────────────────────────────────
async def solve_hcaptcha(
    sitekey: str,
    page_url: str,
    *,
    rqdata: str | None = None,
    user_agent: str | None = None,
    proxy_url: str | None = None,
) -> str | None:
    """Solve an hCaptcha challenge and return the token (use as `captcha_key`).

    Pass `rqdata` (from Discord's `captcha_rqdata` field) to enable Enterprise
    mode — without it Discord will reject the solved token with a new challenge.

    Returns None if no provider is configured, key is missing, or solver fails.
    """
    settings = get_settings()
    provider = (settings.captcha_provider or "").lower()
    if provider in ("", "disabled", "none", "off"):
        return None
    if not settings.captcha_api_key:
        logger.warning("captcha disabled — CAPTCHA_API_KEY is empty")
        return None

    ua = user_agent or settings.discord_user_agent
    key = settings.captcha_api_key

    try:
        if provider == "capsolver":
            return await _solve_anticaptcha_style(
                "https://api.capsolver.com", sitekey, page_url, key,
                rqdata=rqdata, user_agent=ua, proxy_url=proxy_url,
            )
        if provider == "capmonster":
            return await _solve_anticaptcha_style(
                "https://api.capmonster.cloud", sitekey, page_url, key,
                rqdata=rqdata, user_agent=ua, proxy_url=proxy_url,
            )
        if provider == "anticaptcha":
            return await _solve_anticaptcha_style(
                "https://api.anti-captcha.com", sitekey, page_url, key,
                rqdata=rqdata, user_agent=ua, proxy_url=proxy_url,
            )
        if provider in ("2captcha", "twocaptcha"):
            return await _solve_twocaptcha(
                sitekey, page_url, key, rqdata=rqdata, user_agent=ua
            )
    except (ClientError, TimeoutError) as exc:
        logger.warning("captcha solver network error: %s", exc)
        return None

    logger.warning("Unknown CAPTCHA_PROVIDER=%r — no solver active", provider)
    return None


# ── Anti-Captcha / CapMonster / CapSolver — shared schema ─────────────────
async def _solve_anticaptcha_style(
    base_url: str,
    sitekey: str,
    page_url: str,
    api_key: str,
    *,
    rqdata: str | None = None,
    user_agent: str | None = None,
    proxy_url: str | None = None,
) -> str | None:
    settings = get_settings()
    is_enterprise = bool(rqdata)

    # Use proxy-aware task type when a proxy is supplied — the solver sends
    # the hCaptcha request from the same IP as the Discord request, which
    # improves solve quality for IP-bound challenges.
    if proxy_url:
        parsed = urlparse(proxy_url)
        task: dict[str, object] = {
            "type": "HCaptchaTask",
            "websiteURL": page_url,
            "websiteKey": sitekey,
            "proxyType": parsed.scheme or "http",
            "proxyAddress": parsed.hostname or "",
            "proxyPort": parsed.port or 3128,
        }
        if parsed.username:
            task["proxyLogin"] = parsed.username
            task["proxyPassword"] = parsed.password or ""
    else:
        task = {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": sitekey,
        }

    if is_enterprise:
        task["enterprisePayload"] = {"rqdata": rqdata}
    if user_agent:
        task["userAgent"] = user_agent

    create_payload = {"clientKey": api_key, "task": task}
    provider_host = base_url.split("//")[-1].split("/")[0]
    logger.info(
        "captcha createTask provider=%s type=%s sitekey=%.12s enterprise=%s",
        provider_host, task["type"], sitekey, is_enterprise,
    )

    timeout = ClientTimeout(total=settings.captcha_timeout)
    s = _get_session()
    async with s.post(f"{base_url}/createTask", json=create_payload) as r:
        data = await r.json()
    if not isinstance(data, dict) or data.get("errorId"):
        logger.warning("captcha createTask failed: %s", data)
        return None
    task_id = data.get("taskId")
    if not task_id:
        return None

    for _ in range(settings.captcha_poll_attempts):
        await asyncio.sleep(settings.captcha_poll_interval)
        async with s.post(
            f"{base_url}/getTaskResult",
            json={"clientKey": api_key, "taskId": task_id},
        ) as r:
            data = await r.json()
        if not isinstance(data, dict):
            continue
        if data.get("status") == "ready":
            token = (data.get("solution") or {}).get("gRecaptchaResponse")
            logger.info("captcha solved task=%s", task_id)
            return token
        if data.get("errorId"):
            logger.warning("captcha solver error: %s", data)
            return None

    logger.warning("captcha solver timed out task=%s", task_id)
    return None


# ── 2Captcha — different GET-based schema ─────────────────────────────────
async def _solve_twocaptcha(
    sitekey: str,
    page_url: str,
    api_key: str,
    *,
    rqdata: str | None = None,
    user_agent: str | None = None,
) -> str | None:
    settings = get_settings()
    timeout = ClientTimeout(total=settings.captcha_timeout)
    base = "https://2captcha.com"

    parts = [
        f"key={api_key}",
        "method=hcaptcha",
        f"sitekey={sitekey}",
        f"pageurl={quote_plus(page_url)}",
        "json=1",
    ]
    if rqdata:
        parts.append(f"data={quote_plus(rqdata)}")
    if user_agent:
        parts.append(f"userAgent={quote_plus(user_agent)}")

    logger.info(
        "captcha 2captcha submit sitekey=%s enterprise=%s",
        sitekey[:12], bool(rqdata),
    )

    s = _get_session()
    async with s.get(f"{base}/in.php?" + "&".join(parts)) as r:
        data = await r.json()
    if not isinstance(data, dict) or data.get("status") != 1:
        logger.warning("2captcha submit failed: %s", data)
        return None
    request_id = data.get("request")

    for _ in range(settings.captcha_poll_attempts):
        await asyncio.sleep(settings.captcha_poll_interval)
        async with s.get(
            f"{base}/res.php?key={api_key}&action=get&id={request_id}&json=1"
        ) as r:
            data = await r.json()
        if not isinstance(data, dict):
            continue
        if data.get("status") == 1:
            logger.info("2captcha solved request=%s", request_id)
            return data.get("request")
        if data.get("request") and data["request"] != "CAPCHA_NOT_READY":
            logger.warning("2captcha solver error: %s", data)
            return None

    logger.warning("2captcha solver timed out request=%s", request_id)
    return None


# ── Helper consumed by discord_api.py ─────────────────────────────────────
async def maybe_solve_for_response(
    body: object,
    *,
    page_url_hint: str = "https://discord.com/login",
    proxy_url: str | None = None,
) -> str | None:
    """Inspect a Discord JSON response for a captcha challenge and solve it.

    Discord's challenge shape:
        captcha_key, captcha_sitekey, captcha_service ("hcaptcha"),
        captcha_rqdata, captcha_rqtoken   ← Enterprise fields

    `captcha_rqdata` is forwarded to the solver as the Enterprise payload.
    Without it the solver returns a token Discord will reject immediately.

    `proxy_url` is forwarded to the solver so the captcha is solved from the
    same IP as the Discord request (Anti-Captcha/CapSolver proxy task mode).
    """
    if not isinstance(body, dict):
        return None
    if not body.get("captcha_key"):
        return None
    sitekey = body.get("captcha_sitekey")
    if not sitekey:
        return None
    rqdata = body.get("captcha_rqdata")
    return await solve_hcaptcha(
        sitekey, page_url_hint, rqdata=rqdata, proxy_url=proxy_url
    )
