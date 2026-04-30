"""Web-app login user. Password is bcrypt-hashed (one-way)."""
from __future__ import annotations

from bson import ObjectId
from pydantic import BaseModel, Field

from app.models.common import MONGO_MODEL_CONFIG, PyObjectId


class WebUser(BaseModel):
    model_config = MONGO_MODEL_CONFIG

    id: PyObjectId = Field(default_factory=ObjectId, alias="_id")
    login: str
    password: str  # bcrypt hash
