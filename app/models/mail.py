"""Mail (Rambler) account model — stored encrypted."""
from __future__ import annotations

from bson import ObjectId
from pydantic import BaseModel, EmailStr, Field

from app.models.common import MONGO_MODEL_CONFIG, PyObjectId


class Mail(BaseModel):
    """Stored representation. `password` is Fernet-encrypted."""
    model_config = MONGO_MODEL_CONFIG

    id: PyObjectId = Field(default_factory=ObjectId, alias="_id")
    email: EmailStr
    password: str
