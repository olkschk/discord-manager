"""Proxy model + parser for the two accepted input formats."""
from __future__ import annotations

import re

from bson import ObjectId
from pydantic import BaseModel, Field

from app.models.common import MONGO_MODEL_CONFIG, PyObjectId


class Proxy(BaseModel):
    """Stored representation. `password` is Fernet-encrypted."""
    model_config = MONGO_MODEL_CONFIG

    id: PyObjectId = Field(default_factory=ObjectId, alias="_id")
    ip: str
    port: str
    login: str
    password: str
    assigned: bool = False


_RE_COLON = re.compile(r"^([^:@\s]+):(\d{1,5}):([^:@\s]+):(.+)$")
_RE_AT = re.compile(r"^([^:@\s]+):([^:@\s]+)@([^:@\s]+):(\d{1,5})$")


def parse_proxy_line(line: str) -> tuple[str, str, str, str] | None:
    """Parse one proxy line. Returns (ip, port, login, password) or None.

    Accepts: `ip:port:login:pass`  OR  `login:pass@ip:port`.
    """
    line = line.strip()
    if not line:
        return None

    if (m := _RE_COLON.match(line)) is not None:
        ip, port, login, password = m.groups()
        return ip, port, login, password

    if (m := _RE_AT.match(line)) is not None:
        login, password, ip, port = m.groups()
        return ip, port, login, password

    return None
