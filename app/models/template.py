"""Saved chat-message template."""
from __future__ import annotations

from bson import ObjectId
from pydantic import BaseModel, Field

from app.models.common import MONGO_MODEL_CONFIG, PyObjectId


class Template(BaseModel):
    model_config = MONGO_MODEL_CONFIG

    id: PyObjectId = Field(default_factory=ObjectId, alias="_id")
    text: str
    image: str | None = None
