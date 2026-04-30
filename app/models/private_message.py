"""Direct message received by one of our accounts."""
from __future__ import annotations

from bson import ObjectId
from pydantic import BaseModel, Field

from app.models.common import MONGO_MODEL_CONFIG, PyObjectId


class PrivateMessage(BaseModel):
    model_config = MONGO_MODEL_CONFIG

    id: PyObjectId = Field(default_factory=ObjectId, alias="_id")
    text: str
    image: str | None = None
    sender: str = Field(alias="from")
    to: str  # email of the receiving account
    is_read: bool = False
