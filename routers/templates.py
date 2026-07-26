"""Invoice templates — mounted at /api/templates.

Templates are saved snapshots of an invoice's line items + notes. The reporter
can one-click "Create from template" to spin up a draft invoice for a repeat
client.
"""
import uuid
from typing import List

from fastapi import APIRouter, HTTPException, Request

from auth_core import get_current_user, require_active_subscription
from db import calc_invoice_total, db, next_invoice_number, now_iso, serialize_invoice
from models import InvoiceOut, InvoiceTemplateIn, InvoiceTemplateOut

router = APIRouter()


def _strip(t: dict) -> dict:
    return {k: v for k, v in t.items() if k not in ("_id", "user_id")}


@router.get("", response_model=List[InvoiceTemplateOut])
async def list_templates(request: Request):
    user = await get_current_user(request)
    rows = await db.invoice_templates.find(
        {"user_id": user["id"]}, {"_id": 0}
    ).sort("created_at", -1).to_list(500)
    return [InvoiceTemplateOut(**_strip(t)) for t in rows]


@router.post("", response_model=InvoiceTemplateOut)
async def create_template(payload: InvoiceTemplateIn, request: Request):
    user = await require_active_subscription(request)
    tid = str(uuid.uuid4())
    doc = {
        "id": tid,
        "user_id": user["id"],
        "name": payload.name.strip(),
        "client_id": payload.client_id,
        "line_items": [li.model_dump() for li in payload.line_items],
        "notes": payload.notes,
        "payment_instructions": payload.payment_instructions,
        "created_at": now_iso(),
    }
    await db.invoice_templates.insert_one(doc)
    return InvoiceTemplateOut(**_strip(doc))


@router.get("/{template_id}", response_model=InvoiceTemplateOut)
async def get_template(template_id: str, request: Request):
    user = await get_current_user(request)
    t = await db.invoice_templates.find_one({"id": template_id, "user_id": user["id"]}, {"_id": 0})
    if not t:
        raise HTTPException(404, "Template not found")
    return InvoiceTemplateOut(**_strip(t))


@router.put("/{template_id}", response_model=InvoiceTemplateOut)
async def update_template(template_id: str, payload: InvoiceTemplateIn, request: Request):
    user = await get_current_user(request)
    res = await db.invoice_templates.update_one(
        {"id": template_id, "user_id": user["id"]},
        {"$set": {
            "name": payload.name.strip(),
            "client_id": payload.client_id,
            "line_items": [li.model_dump() for li in payload.line_items],
            "notes": payload.notes,
            "payment_instructions": payload.payment_instructions,
        }},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Template not found")
    return await get_template(template_id, request)


@router.delete("/{template_id}")
async def delete_template(template_id: str, request: Request):
    user = await get_current_user(request)
    await db.invoice_templates.delete_one({"id": template_id, "user_id": user["id"]})
    return {"ok": True}


@router.post("/{template_id}/create-invoice", response_model=InvoiceOut)
async def create_invoice_from_template(template_id: str, request: Request):
    """Spin up a Draft invoice from this template. Uses the template's
    `client_id` for billing snapshot; due date defaults to user's
    `default_net_days` from today."""
    user = await require_active_subscription(request)
    t = await db.invoice_templates.find_one({"id": template_id, "user_id": user["id"]}, {"_id": 0})
    if not t:
        raise HTTPException(404, "Template not found")
    if not t.get("client_id"):
        raise HTTPException(400, "Template has no client. Edit the template and set a client first.")

    cl = await db.clients.find_one({"id": t["client_id"], "user_id": user["id"]}, {"_id": 0}) or {}
    billed_to_name = (cl.get("name") or "").strip()
    billed_to_email = cl.get("contact_email") or None
    billed_to_address = cl.get("billing_address") or None
    contact = (cl.get("contact_name") or "").strip()
    if contact:
        billed_to_name = (f"{billed_to_name} (Attn: {contact})").strip()

    from datetime import date, timedelta
    today = date.today()
    net_days = int(user.get("default_net_days") or 30)
    due = today + timedelta(days=net_days)

    iid = str(uuid.uuid4())
    invoice_number = await next_invoice_number(user["id"])
    items_raw = list(t.get("line_items") or [])
    total = calc_invoice_total(items_raw)
    doc = {
        "id": iid,
        "user_id": user["id"],
        "invoice_number": invoice_number,
        "job_id": None,
        "client_id": t["client_id"],
        "invoice_date": today.isoformat(),
        "due_date": due.isoformat(),
        "line_items": items_raw,
        "notes": t.get("notes"),
        "payment_instructions": t.get("payment_instructions"),
        "billed_to_name": billed_to_name,
        "billed_to_email": billed_to_email,
        "billed_to_address": billed_to_address,
        "status": "Draft",
        "total": total,
        "sent_at": None,
        "paid_at": None,
        "voided_at": None,
        "created_at": now_iso(),
        "from_template_id": template_id,
    }
    await db.invoices.insert_one(doc)
    return InvoiceOut(**serialize_invoice(doc))
