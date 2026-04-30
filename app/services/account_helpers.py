"""Cross-cutting helpers used by routers that need to act *as* a Discord account."""
from __future__ import annotations

from bson import ObjectId

from app.database import discords, proxies as proxies_coll
from app.security import decrypt
from app.services.discord_api import build_proxy_url


async def load_account_token_and_proxy(
    account_id: str | ObjectId,
) -> tuple[dict, str, str | None] | None:
    """Resolve an account by id, decrypt its token, build its proxy URL.

    Returns `(account_doc, token, proxy_url_or_none)` or `None` on:
      - invalid id / not found
      - token ciphertext unreadable

    Bad proxy ciphertext is non-fatal — the account is returned with proxy_url=None.
    """
    if isinstance(account_id, str):
        if not ObjectId.is_valid(account_id):
            return None
        account_id = ObjectId(account_id)

    acc = await discords().find_one({"_id": account_id})
    if acc is None:
        return None

    try:
        token = decrypt(acc["discord_token"])
    except ValueError:
        return None

    proxy_url: str | None = None
    if acc.get("proxy_id"):
        proxy = await proxies_coll().find_one({"_id": acc["proxy_id"]})
        if proxy is not None:
            try:
                proxy_url = build_proxy_url(
                    proxy["ip"], proxy["port"], proxy["login"], decrypt(proxy["password"])
                )
            except ValueError:
                proxy_url = None

    return acc, token, proxy_url
