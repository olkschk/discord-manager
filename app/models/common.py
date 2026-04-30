"""Shared model primitives (ObjectId handling, base config)."""
from __future__ import annotations

from typing import Annotated, Any

from bson import ObjectId
from pydantic import BeforeValidator, ConfigDict


def _validate_object_id(value: Any) -> ObjectId:
    if isinstance(value, ObjectId):
        return value
    if isinstance(value, str) and ObjectId.is_valid(value):
        return ObjectId(value)
    raise ValueError(f"Invalid ObjectId: {value!r}")


# Use as: id: PyObjectId = Field(default_factory=ObjectId, alias="_id")
PyObjectId = Annotated[ObjectId, BeforeValidator(_validate_object_id)]


MONGO_MODEL_CONFIG = ConfigDict(
    populate_by_name=True,
    arbitrary_types_allowed=True,
    json_encoders={ObjectId: str},
)
