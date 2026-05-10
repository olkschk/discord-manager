"""Saved chat-message templates: list / create / delete."""
from __future__ import annotations

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.database import templates as templates_coll
from app.security import require_login

router = APIRouter(
    prefix="/api/templates",
    dependencies=[Depends(require_login)],
    tags=["templates"],
)


class TemplateBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    image: str | None = None


@router.get("")
async def list_templates(user: str = Depends(require_login)) -> list[dict]:
    out: list[dict] = []
    async for t in templates_coll().find({"owner": user}).sort("_id", -1):
        out.append({"id": str(t["_id"]), "text": t.get("text", ""), "image": t.get("image")})
    return out


@router.post("")
async def create_template(
    body: TemplateBody,
    user: str = Depends(require_login),
) -> dict:
    res = await templates_coll().insert_one({"owner": user, "text": body.text, "image": body.image})
    return {"id": str(res.inserted_id), "text": body.text, "image": body.image}


@router.delete("/{template_id}")
async def delete_template(
    template_id: str,
    user: str = Depends(require_login),
) -> dict:
    if not ObjectId.is_valid(template_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid template id")
    res = await templates_coll().delete_one({"_id": ObjectId(template_id), "owner": user})
    if res.deleted_count == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found")
    return {"deleted": True}
