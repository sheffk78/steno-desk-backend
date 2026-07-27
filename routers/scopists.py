"""Scopist directory + assignment — mounted at /api/scopists.

Each scopist has a `share_token` used by the public scopist portal so the
scopist can view their assigned jobs and mark them complete without ever
creating an account."""
import secrets
import uuid
from typing import List

from fastapi import APIRouter, HTTPException, Request

from auth_core import get_current_user, require_active_subscription
from db import get_db, now_iso
from models import ScopistIn, ScopistOut

router = APIRouter()


def _strip(s: dict) -> dict:
    return {k: v for k, v in s.items() if k not in ("_id", "user_id")}


async def _attach_open_jobs(scopists: List[dict], user_id: str) -> List[dict]:
    if not scopists:
        return scopists
    ids = [s["id"] for s in scopists]
    counts: dict = {}
    pipe = [
        {"$match": {"user_id": user_id, "scopist_id": {"$in": ids},
                    "scopist_status": {"$in": ["Assigned", "In Progress"]}}},
        {"$group": {"_id": "$scopist_id", "n": {"$sum": 1}}},
    ]
    async for row in db.jobs.aggregate(pipe):
        counts[row["_id"]] = row["n"]
    for s in scopists:
        s["open_jobs"] = int(counts.get(s["id"], 0))
    return scopists


@router.get("", response_model=List[ScopistOut])
async def list_scopists(request: Request):
    user = await get_current_user(request)
    rows = await db.scopists.find(
        {"user_id": user["id"], "is_deleted": {"$ne": True}}, {"_id": 0}
    ).sort("created_at", -1).to_list(500)
    rows = await _attach_open_jobs(rows, user["id"])
    return [ScopistOut(**_strip(s)) for s in rows]


@router.post("", response_model=ScopistOut)
async def create_scopist(payload: ScopistIn, request: Request):
    user = await require_active_subscription(request)
    sid = str(uuid.uuid4())
    doc = {
        "id": sid,
        "user_id": user["id"],
        "first_name": payload.first_name.strip(),
        "last_name": payload.last_name.strip(),
        "email": payload.email,
        "rate_per_page": payload.rate_per_page,
        "notes": payload.notes,
        "share_token": secrets.token_urlsafe(24),
        "is_deleted": False,
        "created_at": now_iso(),
    }
    await db.scopists.insert_one(doc)
    doc["open_jobs"] = 0
    return ScopistOut(**_strip(doc))


@router.put("/{scopist_id}", response_model=ScopistOut)
async def update_scopist(scopist_id: str, payload: ScopistIn, request: Request):
    user = await get_current_user(request)
    res = await db.scopists.update_one(
        {"id": scopist_id, "user_id": user["id"]},
        {"$set": {
            "first_name": payload.first_name.strip(),
            "last_name": payload.last_name.strip(),
            "email": payload.email,
            "rate_per_page": payload.rate_per_page,
            "notes": payload.notes,
        }},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Scopist not found")
    s = await db.scopists.find_one({"id": scopist_id, "user_id": user["id"]}, {"_id": 0})
    s = (await _attach_open_jobs([s], user["id"]))[0]
    return ScopistOut(**_strip(s))


@router.delete("/{scopist_id}")
async def delete_scopist(scopist_id: str, request: Request):
    user = await get_current_user(request)
    await db.scopists.update_one(
        {"id": scopist_id, "user_id": user["id"]},
        {"$set": {"is_deleted": True}},
    )
    # Unassign from any in-progress jobs
    await db.jobs.update_many(
        {"user_id": user["id"], "scopist_id": scopist_id},
        {"$set": {"scopist_id": None, "scopist_status": None}},
    )
    return {"ok": True}


@router.post("/{scopist_id}/regenerate-token", response_model=ScopistOut)
async def regenerate_token(scopist_id: str, request: Request):
    user = await get_current_user(request)
    res = await db.scopists.update_one(
        {"id": scopist_id, "user_id": user["id"]},
        {"$set": {"share_token": secrets.token_urlsafe(24)}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Scopist not found")
    s = await db.scopists.find_one({"id": scopist_id, "user_id": user["id"]}, {"_id": 0})
    s = (await _attach_open_jobs([s], user["id"]))[0]
    return ScopistOut(**_strip(s))
