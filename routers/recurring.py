"""Recurring invoice schedules — mounted at /api/recurring.

Tiny domain model:
  - frequency: 'monthly' (day_of_month) or 'weekly' (day_of_week, 0=Mon..6=Sun)
  - next_run_date drives generation; on each run we materialize a Draft
    invoice and bump the schedule forward.

A background loop in `db.py` (started in server.startup) ticks every 30
minutes and calls `run_due_recurrings()`.
"""
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, HTTPException, Request

from auth_core import get_current_user, require_active_subscription
from db import calc_invoice_total, db, next_invoice_number, now_iso, serialize_invoice
from models import InvoiceOut, RecurringIn, RecurringOut

logger = logging.getLogger(__name__)
router = APIRouter()


def _strip(r: dict) -> dict:
    return {k: v for k, v in r.items() if k not in ("_id", "user_id")}


def _bump_next_run(prev_iso: str, freq: str, dom: int = 1, dow: int = 1) -> str:
    """Compute the next run date given the schedule.
    monthly → first occurrence on/after prev_iso of next month with day=dom (cap 28).
    weekly  → +7 days from prev_iso (keeps the original weekday alignment).
    """
    d = date.fromisoformat(prev_iso)
    if freq == "weekly":
        return (d + timedelta(days=7)).isoformat()
    # monthly
    year = d.year + (1 if d.month == 12 else 0)
    month = 1 if d.month == 12 else d.month + 1
    day = min(max(int(dom or 1), 1), 28)
    return date(year, month, day).isoformat()


@router.get("", response_model=List[RecurringOut])
async def list_recurring(request: Request):
    user = await get_current_user(request)
    rows = await db.recurring_invoices.find(
        {"user_id": user["id"]}, {"_id": 0}
    ).sort("created_at", -1).to_list(500)
    return [RecurringOut(**_strip(r)) for r in rows]


@router.post("", response_model=RecurringOut)
async def create_recurring(payload: RecurringIn, request: Request):
    user = await require_active_subscription(request)
    rid = str(uuid.uuid4())
    doc = {
        "id": rid,
        "user_id": user["id"],
        "name": payload.name.strip(),
        "client_id": payload.client_id,
        "frequency": payload.frequency,
        "day_of_month": int(payload.day_of_month or 1),
        "day_of_week": int(payload.day_of_week or 1),
        "next_run_date": payload.next_run_date,
        "line_items": [li.model_dump() for li in payload.line_items],
        "notes": payload.notes,
        "payment_instructions": payload.payment_instructions,
        "active": bool(payload.active),
        "created_at": now_iso(),
        "last_run_at": None,
        "last_invoice_id": None,
        "runs_count": 0,
    }
    await db.recurring_invoices.insert_one(doc)
    return RecurringOut(**_strip(doc))


@router.get("/{recurring_id}", response_model=RecurringOut)
async def get_recurring(recurring_id: str, request: Request):
    user = await get_current_user(request)
    r = await db.recurring_invoices.find_one({"id": recurring_id, "user_id": user["id"]}, {"_id": 0})
    if not r:
        raise HTTPException(404, "Recurring schedule not found")
    return RecurringOut(**_strip(r))


@router.put("/{recurring_id}", response_model=RecurringOut)
async def update_recurring(recurring_id: str, payload: RecurringIn, request: Request):
    user = await get_current_user(request)
    res = await db.recurring_invoices.update_one(
        {"id": recurring_id, "user_id": user["id"]},
        {"$set": {
            "name": payload.name.strip(),
            "client_id": payload.client_id,
            "frequency": payload.frequency,
            "day_of_month": int(payload.day_of_month or 1),
            "day_of_week": int(payload.day_of_week or 1),
            "next_run_date": payload.next_run_date,
            "line_items": [li.model_dump() for li in payload.line_items],
            "notes": payload.notes,
            "payment_instructions": payload.payment_instructions,
            "active": bool(payload.active),
        }},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Recurring schedule not found")
    return await get_recurring(recurring_id, request)


@router.delete("/{recurring_id}")
async def delete_recurring(recurring_id: str, request: Request):
    user = await get_current_user(request)
    await db.recurring_invoices.delete_one({"id": recurring_id, "user_id": user["id"]})
    return {"ok": True}


async def _materialize_invoice(rec: dict) -> str:
    """Create a Draft invoice from a recurring schedule. Returns invoice id."""
    user = await db.users.find_one({"id": rec["user_id"]}, {"_id": 0, "password_hash": 0}) or {}
    cl = await db.clients.find_one({"id": rec["client_id"], "user_id": rec["user_id"]}, {"_id": 0}) or {}
    billed_to_name = (cl.get("name") or "").strip()
    billed_to_email = cl.get("contact_email") or None
    billed_to_address = cl.get("billing_address") or None
    contact = (cl.get("contact_name") or "").strip()
    if contact:
        billed_to_name = (f"{billed_to_name} (Attn: {contact})").strip()

    today = date.today()
    net_days = int(user.get("default_net_days") or 30)
    due = today + timedelta(days=net_days)

    iid = str(uuid.uuid4())
    invoice_number = await next_invoice_number(rec["user_id"])
    items = list(rec.get("line_items") or [])
    total = calc_invoice_total(items)
    doc = {
        "id": iid,
        "user_id": rec["user_id"],
        "invoice_number": invoice_number,
        "job_id": None,
        "client_id": rec["client_id"],
        "invoice_date": today.isoformat(),
        "due_date": due.isoformat(),
        "line_items": items,
        "notes": rec.get("notes"),
        "payment_instructions": rec.get("payment_instructions"),
        "billed_to_name": billed_to_name,
        "billed_to_email": billed_to_email,
        "billed_to_address": billed_to_address,
        "status": "Draft",
        "total": total,
        "sent_at": None,
        "paid_at": None,
        "voided_at": None,
        "created_at": now_iso(),
        "from_recurring_id": rec["id"],
    }
    await db.invoices.insert_one(doc)
    return iid


@router.post("/{recurring_id}/run-now", response_model=InvoiceOut)
async def run_now(recurring_id: str, request: Request):
    """Manual trigger — generate one Draft invoice immediately and advance the schedule."""
    user = await require_active_subscription(request)
    rec = await db.recurring_invoices.find_one({"id": recurring_id, "user_id": user["id"]}, {"_id": 0})
    if not rec:
        raise HTTPException(404, "Recurring schedule not found")
    iid = await _materialize_invoice(rec)
    next_date = _bump_next_run(rec["next_run_date"], rec["frequency"],
                               rec.get("day_of_month", 1), rec.get("day_of_week", 1))
    await db.recurring_invoices.update_one(
        {"id": recurring_id, "user_id": user["id"]},
        {"$set": {
            "next_run_date": next_date,
            "last_run_at": now_iso(),
            "last_invoice_id": iid,
        }, "$inc": {"runs_count": 1}},
    )
    inv = await db.invoices.find_one({"id": iid}, {"_id": 0})
    return InvoiceOut(**serialize_invoice(inv))


async def run_due_recurrings() -> int:
    """Internal scheduler tick — materialize all schedules whose next_run_date
    is on/before today. Returns number of invoices created."""
    today = date.today().isoformat()
    cur = db.recurring_invoices.find({"active": True, "next_run_date": {"$lte": today}}, {"_id": 0})
    created = 0
    async for rec in cur:
        try:
            iid = await _materialize_invoice(rec)
            next_date = _bump_next_run(rec["next_run_date"], rec["frequency"],
                                       rec.get("day_of_month", 1), rec.get("day_of_week", 1))
            await db.recurring_invoices.update_one(
                {"id": rec["id"], "user_id": rec["user_id"]},
                {"$set": {
                    "next_run_date": next_date,
                    "last_run_at": now_iso(),
                    "last_invoice_id": iid,
                }, "$inc": {"runs_count": 1}},
            )
            created += 1
        except Exception as e:
            logger.error(f"recurring run failed for {rec.get('id')}: {e}")
    if created:
        logger.info(f"recurring: created {created} draft invoice(s)")
    return created
