"""Attorneys (linked to clients) — mounted at /api/attorneys"""
import uuid
from typing import List, Optional

from fastapi import APIRouter, Request

from auth_core import get_current_user, require_active_subscription
from db import db, now_iso
from models import AttorneyIn, AttorneyOut

router = APIRouter()


@router.get("", response_model=List[AttorneyOut])
async def list_attorneys(request: Request, client_id: Optional[str] = None):
    user = await get_current_user(request)
    q = {"user_id": user["id"], "is_deleted": {"$ne": True}}
    if client_id:
        q["client_id"] = client_id
    items = await db.attorneys.find(q, {"_id": 0}).sort("last_name", 1).to_list(2000)
    return [AttorneyOut(**{k: v for k, v in a.items() if k != "user_id"}) for a in items]


@router.post("", response_model=AttorneyOut)
async def create_attorney(payload: AttorneyIn, request: Request):
    user = await require_active_subscription(request)
    aid = str(uuid.uuid4())
    doc = {"id": aid, "user_id": user["id"], **payload.model_dump(), "created_at": now_iso()}
    await db.attorneys.insert_one(doc)
    out = {k: v for k, v in doc.items() if k not in ("_id", "user_id")}
    return AttorneyOut(**out)


@router.delete("/{attorney_id}")
async def delete_attorney(attorney_id: str, request: Request):
    user = await get_current_user(request)
    await db.attorneys.update_one(
        {"id": attorney_id, "user_id": user["id"]},
        {"$set": {"is_deleted": True, "deleted_at": now_iso()}},
    )
    return {"ok": True}
