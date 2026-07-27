"""Jobs CRUD — mounted at /api/jobs"""
import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from auth_core import get_current_user, require_active_subscription
from db import get_db, now_iso
from models import JobIn, JobOut

router = APIRouter()


@router.get("", response_model=List[JobOut])
async def list_jobs(
    request: Request,
    status_filter: Optional[str] = Query(None, alias="status"),
    q: Optional[str] = None,
):
    user = await get_current_user(request)
    query = {"user_id": user["id"]}
    if status_filter and status_filter != "All":
        query["status"] = status_filter
    if q:
        query["$or"] = [
            {"witness": {"$regex": q, "$options": "i"}},
            {"case_caption": {"$regex": q, "$options": "i"}},
            {"case_number": {"$regex": q, "$options": "i"}},
        ]
    items = await db.jobs.find(query, {"_id": 0}).sort("job_date", -1).to_list(2000)
    return [JobOut(**{k: v for k, v in j.items() if k != "user_id"}) for j in items]


@router.post("", response_model=JobOut)
async def create_job(payload: JobIn, request: Request):
    user = await require_active_subscription(request)
    jid = str(uuid.uuid4())
    doc = {"id": jid, "user_id": user["id"], **payload.model_dump(), "invoice_id": None, "created_at": now_iso()}
    await db.jobs.insert_one(doc)
    return JobOut(**{k: v for k, v in doc.items() if k != "user_id"})


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: str, request: Request):
    user = await get_current_user(request)
    j = await db.jobs.find_one({"id": job_id, "user_id": user["id"]}, {"_id": 0})
    if not j:
        raise HTTPException(404, "Job not found")
    return JobOut(**{k: v for k, v in j.items() if k != "user_id"})


@router.put("/{job_id}", response_model=JobOut)
async def update_job(job_id: str, payload: JobIn, request: Request):
    user = await get_current_user(request)
    res = await db.jobs.update_one(
        {"id": job_id, "user_id": user["id"]},
        {"$set": payload.model_dump()},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Job not found")
    return await get_job(job_id, request)


@router.delete("/{job_id}")
async def delete_job(job_id: str, request: Request):
    user = await get_current_user(request)
    await db.jobs.delete_one({"id": job_id, "user_id": user["id"]})
    return {"ok": True}
