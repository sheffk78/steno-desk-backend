"""Clients directory — mounted at /api/clients"""
import uuid
from typing import List

from fastapi import APIRouter, HTTPException, Request

from auth_core import get_current_user, require_active_subscription
from db import db, now_iso, strip
from models import ClientIn, ClientOut

router = APIRouter()


@router.get("", response_model=List[ClientOut])
async def list_clients(request: Request):
    user = await get_current_user(request)
    items = await db.clients.find(
        {"user_id": user["id"], "is_deleted": {"$ne": True}}, {"_id": 0}
    ).sort("name", 1).to_list(2000)
    for c in items:
        cur = db.jobs.find({"user_id": user["id"], "client_id": c["id"]}, {"_id": 0, "job_date": 1})
        jobs = await cur.to_list(2000)
        c["job_count"] = len(jobs)
        c["last_job_date"] = max((j.get("job_date", "") for j in jobs), default=None) or None
    return [ClientOut(**c) for c in items]


@router.post("", response_model=ClientOut)
async def create_client(payload: ClientIn, request: Request):
    user = await require_active_subscription(request)
    cid = str(uuid.uuid4())
    doc = {
        "id": cid,
        "user_id": user["id"],
        **payload.model_dump(),
        "created_at": now_iso(),
    }
    await db.clients.insert_one(doc)
    out = strip(doc)
    out["job_count"] = 0
    out["last_job_date"] = None
    out.pop("user_id", None)
    return ClientOut(**out)


@router.get("/_lookup")
async def lookup_all_clients(request: Request):
    """Returns ALL clients including soft-deleted (id → name + is_deleted).
    Used by Jobs/Invoices/Dashboard so historical rows resolve to `[Deleted
    Client]` instead of an empty cell.

    Declared BEFORE `/{client_id}` so the literal path matches first."""
    user = await get_current_user(request)
    rows = await db.clients.find(
        {"user_id": user["id"]}, {"_id": 0, "id": 1, "name": 1, "is_deleted": 1}
    ).to_list(5000)
    return [{"id": r["id"], "name": r["name"], "is_deleted": bool(r.get("is_deleted"))} for r in rows]


@router.get("/{client_id}", response_model=ClientOut)
async def get_client(client_id: str, request: Request):
    user = await get_current_user(request)
    c = await db.clients.find_one({"id": client_id, "user_id": user["id"]}, {"_id": 0})
    if not c:
        raise HTTPException(404, "Client not found")
    jobs = await db.jobs.find({"user_id": user["id"], "client_id": client_id}, {"_id": 0, "job_date": 1}).to_list(2000)
    c["job_count"] = len(jobs)
    c["last_job_date"] = max((j.get("job_date", "") for j in jobs), default=None) or None
    c.pop("user_id", None)
    return ClientOut(**c)


@router.put("/{client_id}", response_model=ClientOut)
async def update_client(client_id: str, payload: ClientIn, request: Request):
    user = await get_current_user(request)
    res = await db.clients.update_one(
        {"id": client_id, "user_id": user["id"]},
        {"$set": payload.model_dump()},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Client not found")
    return await get_client(client_id, request)


@router.delete("/{client_id}")
async def delete_client(client_id: str, request: Request):
    """Soft-delete — preserve historical job/invoice references. The client
    record stays so that lookups by id still resolve to a real name (with
    the `is_deleted: true` flag); UI surfaces show `[Deleted Client]` when
    a referenced client is flagged deleted."""
    user = await get_current_user(request)
    res = await db.clients.update_one(
        {"id": client_id, "user_id": user["id"]},
        {"$set": {"is_deleted": True, "deleted_at": now_iso()}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Client not found")
    # Cascade soft-delete to linked attorneys.
    await db.attorneys.update_many(
        {"user_id": user["id"], "client_id": client_id},
        {"$set": {"is_deleted": True, "deleted_at": now_iso()}},
    )
    return {"ok": True}
