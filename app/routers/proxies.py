"""Proxy API: bulk-add, list."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, status
from pymongo.errors import DuplicateKeyError

from app.database import proxies as proxies_coll
from app.models.proxy import parse_proxy_line
from app.security import encrypt, require_login

router = APIRouter(
    prefix="/api/proxies",
    dependencies=[Depends(require_login)],
    tags=["proxies"],
)
logger = logging.getLogger(__name__)


@router.post("/add")
async def add_proxies(
    payload: str = Form(...),
    user: str = Depends(require_login),
) -> dict:
    """Multi-add. Accepts `ip:port:login:pass` or `login:pass@ip:port` per line."""
    # Early sanity check: if the first non-empty line has '@' in its first
    # colon-segment, the user likely pasted account lines by mistake.
    # (Proxy @ format is  login:pass@ip:port  — the @ is NOT in segment 0.)
    first = next((l.strip() for l in payload.splitlines() if l.strip()), "")
    if first:
        first_segment = first.split(":")[0]
        if "@" in first_segment:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Выглядит как строки аккаунтов (email@...) — вставь их в раздел Accounts",
            )

    added, skipped = 0, 0
    errors: list[str] = []

    for raw_line in payload.splitlines():
        parsed = parse_proxy_line(raw_line)
        if parsed is None:
            if raw_line.strip():
                errors.append(f"Bad format: {raw_line[:60]!r}")
                skipped += 1
            continue

        ip, port, login, password = parsed
        try:
            await proxies_coll().insert_one(
                {
                    "owner": user,
                    "ip": ip,
                    "port": port,
                    "login": login,
                    "password": encrypt(password),
                    "assigned": False,
                }
            )
            added += 1
        except DuplicateKeyError:
            errors.append(f"Duplicate proxy: {ip}:{port}")
            skipped += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to insert proxy %s:%s", ip, port)
            errors.append(f"{ip}:{port}: {exc.__class__.__name__}")
            skipped += 1

    return {"added": added, "skipped": skipped, "errors": errors}
