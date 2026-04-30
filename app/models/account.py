"""Discord account model + multi-add line parser."""
from __future__ import annotations

from bson import ObjectId
from pydantic import BaseModel, EmailStr, Field

from app.models.common import MONGO_MODEL_CONFIG, PyObjectId


class DiscordAccount(BaseModel):
    """Stored representation. `password`, `discord_token`, `two_fa_secret` are Fernet-encrypted."""
    model_config = MONGO_MODEL_CONFIG

    id: PyObjectId = Field(default_factory=ObjectId, alias="_id")
    email: EmailStr
    password: str
    discord_token: str

    proxy_id: PyObjectId | None = None
    two_fa_backup_codes: list[str] | None = None
    two_fa_secret: str | None = None

    token_valid: bool = False
    joined_voice: bool = False
    joined_stream: bool = False
    joined_server: bool = False

    name: str | None = None
    username: str | None = None
    bio: str | None = None


def parse_account_line(line: str) -> tuple[str, str, str] | None:
    """Parse one `mail:pass:token` line. Returns (email, password, token) or None."""
    line = line.strip()
    if not line:
        return None
    parts = line.split(":", 2)  # token may contain ':' so only split twice
    if len(parts) != 3:
        return None
    email, password, token = (p.strip() for p in parts)
    if not email or not password or not token:
        return None
    return email, password, token
