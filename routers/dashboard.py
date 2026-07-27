"""Dashboard summary — mounted at /api/dashboard"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request

from auth_core import get_current_user
from db import get_db

router = APIRouter()


@router.get("/summary")
async def dashboard_summary(request: Request):
    user = await get_current_user(request)
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).date().isoformat()
    next_month = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_end = (next_month - timedelta(days=1)).date().isoformat()

    invs = await db.invoices.find({"user_id": user["id"]}, {"_id": 0}).to_list(10000)

    billed = sum(
        float(i.get("total") or 0) for i in invs
        if i.get("invoice_date") and month_start <= i["invoice_date"] <= month_end
    )
    collected = sum(
        float(i.get("total") or 0) for i in invs
        if i.get("status") == "Paid"
        and i.get("paid_at")
        and month_start <= i["paid_at"][:10] <= month_end
    )
    outstanding_invs = [i for i in invs if i.get("status") in ("Draft", "Sent")]
    outstanding_count = len(outstanding_invs)
    outstanding_total = round(sum(float(i.get("total") or 0) for i in outstanding_invs), 2)

    today = now.date().isoformat()
    in_14 = (now + timedelta(days=14)).date().isoformat()
    upcoming = await db.jobs.find(
        {"user_id": user["id"], "job_date": {"$gte": today, "$lte": in_14}},
        {"_id": 0},
    ).sort("job_date", 1).to_list(50)
    upcoming_clean = [{k: v for k, v in j.items() if k != "user_id"} for j in upcoming]

    # Map client names for upcoming jobs and recent invoices. Surface soft-
    # deleted clients with a `[Deleted]` suffix so historical rows still
    # show *something* per V1 spec.
    name_map: dict = {}
    needed_ids = (
        {j.get("client_id") for j in upcoming_clean if j.get("client_id")}
        | {i.get("client_id") for i in invs if i.get("client_id")}
    )
    if needed_ids:
        async for c in db.clients.find(
            {"user_id": user["id"], "id": {"$in": list(needed_ids)}},
            {"_id": 0, "id": 1, "name": 1, "is_deleted": 1},
        ):
            name_map[c["id"]] = (
                f"{c['name']} [Deleted]" if c.get("is_deleted") else c["name"]
            )
    for j in upcoming_clean:
        j["client_name"] = name_map.get(j.get("client_id"), "")

    recent_raw = sorted(
        [i for i in invs if i.get("status") != "Void"],
        key=lambda i: i.get("created_at", ""),
        reverse=True,
    )[:5]
    recent_invoices = [
        {
            "id": i["id"],
            "invoice_number": i.get("invoice_number"),
            "client_name": name_map.get(i.get("client_id"), i.get("billed_to_name") or ""),
            "invoice_date": i.get("invoice_date"),
            "due_date": i.get("due_date"),
            "total": i.get("total") or 0,
            "status": i.get("status"),
        }
        for i in recent_raw
    ]

    return {
        "billed_this_month": round(billed, 2),
        "collected_this_month": round(collected, 2),
        "outstanding_count": outstanding_count,
        "outstanding_total": outstanding_total,
        "upcoming_jobs": upcoming_clean,
        "recent_invoices": recent_invoices,
    }


@router.get("/inbox")
async def dashboard_inbox(request: Request):
    """Smart suggestions: jobs that need invoicing + draft invoices ready to send.

    - `ready_jobs`: Completed jobs without an invoice_id, ordered oldest first
      so the most-overdue billing surfaces first. Includes the client's
      default rates so the UI can show "Original 4.50/pg · Appearance 250"
      hints next to each job.
    - `draft_invoices`: Draft invoices with a billed_to_email, ordered by
      invoice_date asc. Includes the resolved recipient (ordering attorney
      email when available, else billed_to_email).
    """
    user = await get_current_user(request)

    # ----- Ready to invoice: Completed jobs without invoice_id ---------------
    ready_raw = await db.jobs.find(
        {
            "user_id": user["id"],
            "status": "Completed",
            "$or": [{"invoice_id": None}, {"invoice_id": {"$exists": False}}],
        },
        {"_id": 0, "user_id": 0},
    ).sort("job_date", 1).to_list(500)

    client_ids = list({j.get("client_id") for j in ready_raw if j.get("client_id")})
    client_map: dict = {}
    if client_ids:
        async for c in db.clients.find(
            {"user_id": user["id"], "id": {"$in": client_ids}},
            {"_id": 0, "id": 1, "name": 1, "is_deleted": 1, "rates": 1, "contact_email": 1},
        ):
            client_map[c["id"]] = c

    ready_jobs = []
    for j in ready_raw:
        cl = client_map.get(j.get("client_id")) or {}
        ready_jobs.append({
            **j,
            "client_name": (
                f"{cl.get('name', '')} [Deleted]" if cl.get("is_deleted")
                else cl.get("name", "")
            ),
            "client_rates": cl.get("rates") or {},
            "client_contact_email": cl.get("contact_email"),
        })

    # ----- Drafts ready to send ---------------------------------------------
    # Return ALL Draft invoices (not just those with a billing email) so the
    # UI can show a "Add a billing email" warning chip + disable the checkbox
    # on the drafts that aren't sendable yet. Better UX than silently hiding.
    draft_raw = await db.invoices.find(
        {"user_id": user["id"], "status": "Draft"},
        {"_id": 0, "user_id": 0},
    ).sort("invoice_date", 1).to_list(500)

    # Resolve ordering-attorney emails per job (so bulk send can prefer them).
    job_ids = list({i.get("job_id") for i in draft_raw if i.get("job_id")})
    job_map: dict = {}
    atty_ids: set = set()
    if job_ids:
        async for j in db.jobs.find(
            {"user_id": user["id"], "id": {"$in": job_ids}},
            {"_id": 0, "id": 1, "ordering_attorney_id": 1},
        ):
            job_map[j["id"]] = j
            if j.get("ordering_attorney_id"):
                atty_ids.add(j["ordering_attorney_id"])
    atty_map: dict = {}
    if atty_ids:
        async for a in db.attorneys.find(
            {"user_id": user["id"], "id": {"$in": list(atty_ids)}},
            {"_id": 0, "id": 1, "first_name": 1, "last_name": 1, "email": 1},
        ):
            atty_map[a["id"]] = a

    inv_client_ids = list({i.get("client_id") for i in draft_raw if i.get("client_id")})
    if inv_client_ids:
        async for c in db.clients.find(
            {"user_id": user["id"], "id": {"$in": inv_client_ids}},
            {"_id": 0, "id": 1, "name": 1, "is_deleted": 1, "contact_email": 1},
        ):
            client_map.setdefault(c["id"], c)

    draft_invoices = []
    for i in draft_raw:
        cl = client_map.get(i.get("client_id")) or {}
        job = job_map.get(i.get("job_id") or "") or {}
        atty = atty_map.get(job.get("ordering_attorney_id") or "") or {}
        recipient = (atty.get("email") or i.get("billed_to_email") or "").strip()
        draft_invoices.append({
            "id": i["id"],
            "invoice_number": i.get("invoice_number"),
            "invoice_date": i.get("invoice_date"),
            "due_date": i.get("due_date"),
            "total": i.get("total") or 0,
            "client_id": i.get("client_id"),
            "client_name": (
                f"{cl.get('name', '')} [Deleted]" if cl.get("is_deleted")
                else (cl.get("name") or i.get("billed_to_name") or "")
            ),
            "recipient_email": recipient,
            "recipient_name": (
                f"{atty.get('first_name','')} {atty.get('last_name','')}".strip()
                if atty.get("email") else (cl.get("name") or "")
            ),
        })

    return {
        "ready_jobs": ready_jobs,
        "draft_invoices": draft_invoices,
    }
