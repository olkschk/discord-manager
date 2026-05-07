"""AI-driven identity generation (username, display name, bio).

Supports Anthropic and OpenAI selected via `AI_PROVIDER` env var.
"""
from __future__ import annotations

import json
import logging
import re
from typing import TypedDict

from app.config import get_settings

logger = logging.getLogger(__name__)


class Identity(TypedDict):
    username: str
    global_name: str
    bio: str


_PROMPT = (
    "Generate one Discord user identity for a crypto/Web3 community. "
    "The persona should feel like a real crypto enthusiast aged 18–35 — a degen, trader, NFT collector, or DeFi user. "
    "Use crypto slang naturally: ape in, ngmi/wagmi, gm, ser, fren, degen, rekt, moon, bag, alpha, rug, based, chad, nfa, dyor, hodl, on-chain, L2, defi, mint, airdrop, narrative, flip. "
    "Avoid explicit content, real brand names (Binance, Coinbase etc.), and slurs. "
    "Return ONLY a JSON object with these exact keys:\n"
    '- "username": 2-15 chars, lowercase a-z / digits / underscore only, '
    "no leading/trailing underscore — crypto-style nick (e.g. eth_maxi, sol_degen, nft_ape)\n"
    '- "global_name": display name, 2-30 chars, can include spaces, capitals, numbers — '
    "crypto-themed (e.g. 0xSatoshi, CryptoNomad, DegenKing, WagmiSer)\n"
    '- "bio": 30-180 chars, casual crypto one-liner — trading, airdrops, NFTs, alpha, vibes\n\n'
    'Example shape: {"username":"sol_degen_77","global_name":"0xDegen",'
    '"bio":"Full-time degen. Chasing airdrops and on-chain alpha. ngmi if you\'re not in DeFi. gm ser"}'
)


async def generate_identity() -> Identity:
    """Dispatches to the provider configured by AI_PROVIDER."""
    settings = get_settings()
    provider = settings.ai_provider.lower()
    if provider == "anthropic":
        return await _generate_anthropic()
    if provider == "openai":
        return await _generate_openai()
    raise RuntimeError(f"Unknown AI_PROVIDER: {settings.ai_provider!r}")


async def _generate_anthropic() -> Identity:
    import anthropic

    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    resp = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=400,
        messages=[{"role": "user", "content": _PROMPT}],
    )
    text = resp.content[0].text  # type: ignore[union-attr]
    return _parse_identity(text)


async def _generate_openai() -> Identity:
    from openai import AsyncOpenAI

    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    resp = await client.chat.completions.create(
        model=settings.openai_model,
        messages=[{"role": "user", "content": _PROMPT}],
        response_format={"type": "json_object"},
    )
    text = resp.choices[0].message.content or ""
    return _parse_identity(text)


def _parse_identity(text: str) -> Identity:
    """Robust extraction of the JSON object from arbitrary model output."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError(f"AI returned non-JSON: {text[:200]!r}")
    data = json.loads(text[start : end + 1])

    raw_username = str(data.get("username", "")).lower()
    username = re.sub(r"[^a-z0-9_]", "", raw_username).strip("_")[:15]
    if not username:
        raise RuntimeError("AI returned empty/invalid username")

    global_name = str(data.get("global_name", "")).strip()[:30]
    bio = str(data.get("bio", "")).strip()[:180]
    return Identity(username=username, global_name=global_name, bio=bio)
