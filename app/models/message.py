"""Message captured by the donor account from a monitored topic."""
from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId
from pydantic import BaseModel, Field

from app.models.common import MONGO_MODEL_CONFIG, PyObjectId


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Message(BaseModel):
    model_config = MONGO_MODEL_CONFIG

    id: PyObjectId = Field(default_factory=ObjectId, alias="_id")
    text: str
    image: str | None = None
    sender: str = Field(alias="from")
    topic: str
    timestamp: datetime = Field(default_factory=_utcnow)
