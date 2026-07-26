"""Invoices, payments, PDFs, send — mounted at /api/invoices"""
import io
import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from auth_core import get_current_user, require_active_subscription
from db import calc_invoice_total, db, next_invoice_number, now_iso, serialize_invoice
from email_service import send_invoice_email
from models import (
    BulkGenerateIn,
    BulkSendIn,
    InvoiceIn,
    InvoiceOut,
    PaymentIn,
    PaymentOut,
    SendInvoiceIn,
)
from pdf_generator import generate_invoice_pdf
from storage_service import get_object

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("", response_model=List[InvoiceOut])
async def list_invoices(request: Request, status_filter: Optional[str] = Query(None, alias="status")):
    user = await get_current_user(request)
    query = {"user_id": user["id"]}
    if status_filter and status_filter != "All":
        query["status"] = status_filter
    items = await db.invoices.find(query, {"_id": 0}).sort("created_at", -1).to_list(2000)
    return [InvoiceOut(**serialize_invoice(i)) for i in items]


@router.post("", response_model=InvoiceOut)
async def create_invoice(payload: InvoiceIn, request: Request):
    user = await require_active_subscription(request)
    iid = str(uuid.uuid4())
    invoice_number = await next_invoice_number(user["id"])
    items_raw = [li.model_dump() for li in payload.line_items]
    total = calc_invoice_total(items_raw)
    cl = await db.clients.find_one({"id": payload.client_id, "user_id": user["id"]}, {"_id": 0}) or {}
    billed_to_name = (cl.get("name") or "").strip()
    billed_to_email = cl.get("contact_email") or None
    billed_to_address = cl.get("billing_address") or None
    contact = (cl.get("contact_name") or "").strip()
    if contact:
        billed_to_name = (f"{billed_to_name} (Attn: {contact})").strip()
    doc = {
        "id": iid,
        "user_id": user["id"],
        "invoice_number": invoice_number,
        "job_id": payload.job_id,
        "client_id": payload.client_id,
        "invoice_date": payload.invoice_date,
        "due_date": payload.due_date,
        "line_items": items_raw,
        "notes": payload.notes,
        "payment_instructions": payload.payment_instructions,
        "billed_to_name": billed_to_name,
        "billed_to_email": billed_to_email,
        "billed_to_address": billed_to_address,
        "status": "Draft",
        "total": total,
        "sent_at": None,
        "paid_at": None,
        "voided_at": None,
        "created_at": now_iso(),
    }
    await db.invoices.insert_one(doc)
    if payload.job_id:
        await db.jobs.update_one(
            {"id": payload.job_id, "user_id": user["id"]},
            {"$set": {"invoice_id": iid, "status": "Invoiced"}},
        )
    return InvoiceOut(**serialize_invoice(doc))


@router.get("/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(invoice_id: str, request: Request):
    user = await get_current_user(request)
    inv = await db.invoices.find_one({"id": invoice_id, "user_id": user["id"]}, {"_id": 0})
    if not inv:
        raise HTTPException(404, "Invoice not found")
    return InvoiceOut(**serialize_invoice(inv))


@router.put("/{invoice_id}", response_model=InvoiceOut)
async def update_invoice(invoice_id: str, payload: InvoiceIn, request: Request):
    user = await get_current_user(request)
    items_raw = [li.model_dump() for li in payload.line_items]
    total = calc_invoice_total(items_raw)
    res = await db.invoices.update_one(
        {"id": invoice_id, "user_id": user["id"]},
        {"$set": {
            "client_id": payload.client_id,
            "job_id": payload.job_id,
            "invoice_date": payload.invoice_date,
            "due_date": payload.due_date,
            "line_items": items_raw,
            "notes": payload.notes,
            "payment_instructions": payload.payment_instructions,
            "total": total,
        }},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Invoice not found")
    return await get_invoice(invoice_id, request)


@router.delete("/{invoice_id}")
async def delete_invoice(invoice_id: str, request: Request):
    user = await get_current_user(request)
    inv = await db.invoices.find_one({"id": invoice_id, "user_id": user["id"]}, {"_id": 0})
    if inv and inv.get("job_id"):
        await db.jobs.update_one(
            {"id": inv["job_id"], "user_id": user["id"]},
            {"$set": {"invoice_id": None, "status": "Completed"}},
        )
    await db.invoices.delete_one({"id": invoice_id, "user_id": user["id"]})
    return {"ok": True}


@router.post("/{invoice_id}/mark-paid", response_model=InvoiceOut)
async def mark_invoice_paid(invoice_id: str, request: Request, payment: Optional[PaymentIn] = None):
    user = await get_current_user(request)
    inv = await db.invoices.find_one({"id": invoice_id, "user_id": user["id"]}, {"_id": 0})
    if not inv:
        raise HTTPException(404, "Invoice not found")
    paid_at = now_iso()
    if payment:
        pdoc = {
            "id": str(uuid.uuid4()),
            "user_id": user["id"],
            "invoice_id": invoice_id,
            "amount": float(payment.amount),
            "payment_date": payment.payment_date,
            "payment_method": payment.payment_method,
            "reference": payment.reference,
            "notes": payment.notes,
            "created_at": paid_at,
        }
        await db.payments.insert_one(pdoc)
    await db.invoices.update_one(
        {"id": invoice_id, "user_id": user["id"]},
        {"$set": {"status": "Paid", "paid_at": paid_at}},
    )
    inv = await db.invoices.find_one({"id": invoice_id, "user_id": user["id"]}, {"_id": 0})
    if inv and inv.get("job_id"):
        await db.jobs.update_one(
            {"id": inv["job_id"], "user_id": user["id"]},
            {"$set": {"status": "Paid"}},
        )
    return InvoiceOut(**serialize_invoice(inv))


@router.post("/{invoice_id}/void", response_model=InvoiceOut)
async def void_invoice(invoice_id: str, request: Request):
    user = await get_current_user(request)
    res = await db.invoices.update_one(
        {"id": invoice_id, "user_id": user["id"]},
        {"$set": {"status": "Void", "voided_at": now_iso()}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Invoice not found")
    inv = await db.invoices.find_one({"id": invoice_id, "user_id": user["id"]}, {"_id": 0})
    if inv and inv.get("job_id"):
        await db.jobs.update_one(
            {"id": inv["job_id"], "user_id": user["id"]},
            {"$set": {"status": "Completed", "invoice_id": None}},
        )
    return InvoiceOut(**serialize_invoice(inv))


@router.get("/{invoice_id}/payments", response_model=List[PaymentOut])
async def list_payments(invoice_id: str, request: Request):
    user = await get_current_user(request)
    rows = await db.payments.find(
        {"invoice_id": invoice_id, "user_id": user["id"]}, {"_id": 0}
    ).sort("payment_date", -1).to_list(100)
    return [PaymentOut(**{k: v for k, v in p.items() if k != "user_id"}) for p in rows]


async def _build_pdf_for(invoice_id: str, user: dict) -> tuple[bytes, dict, dict]:
    """Compose the full PDF for one invoice (helpers + letterhead fetch)."""
    inv = await db.invoices.find_one({"id": invoice_id, "user_id": user["id"]}, {"_id": 0})
    if not inv:
        raise HTTPException(404, "Invoice not found")
    cl = await db.clients.find_one({"id": inv["client_id"], "user_id": user["id"]}, {"_id": 0}) or {}
    job = None
    if inv.get("job_id"):
        job = await db.jobs.find_one({"id": inv["job_id"], "user_id": user["id"]}, {"_id": 0})
    reporter = {
        "name": user.get("name") or "Court Reporter",
        "business_name": user.get("business_name"),
        "address": user.get("address"),
        "address_line1": user.get("address_line1"),
        "address_line2": user.get("address_line2"),
        "city": user.get("city"),
        "state": user.get("state"),
        "zip": user.get("zip"),
        "phone": user.get("phone"),
        "email": user.get("email"),
        "cert_number": user.get("cert_number"),
        "cert_type": user.get("cert_type"),
    }
    if user.get("letterhead_path"):
        try:
            data, ctype = get_object(user["letterhead_path"])
            if ctype in ("image/png", "image/jpeg", "image/jpg"):
                reporter["letterhead_bytes"] = data
        except Exception as e:
            logger.warning(f"letterhead fetch failed for PDF: {e}")
    pdf_bytes = generate_invoice_pdf(inv, reporter, cl, job)
    return pdf_bytes, inv, cl


@router.get("/{invoice_id}/pdf")
async def invoice_pdf(invoice_id: str, request: Request):
    user = await get_current_user(request)
    pdf_bytes, inv, _ = await _build_pdf_for(invoice_id, user)
    fname = f"Invoice-{inv['invoice_number']}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{fname}"'},
    )


@router.post("/{invoice_id}/send")
async def send_invoice(invoice_id: str, payload: SendInvoiceIn, request: Request):
    user = await require_active_subscription(request)
    pdf_bytes, inv, _ = await _build_pdf_for(invoice_id, user)
    fname = f"Invoice-{inv['invoice_number']}.pdf"
    ok, msg_id = send_invoice_email(
        to_email=payload.to_email,
        subject=payload.subject,
        body_text=payload.body,
        pdf_bytes=pdf_bytes,
        pdf_filename=fname,
        cc=payload.cc,
        reporter_name=user.get("name"),
    )
    if not ok:
        raise HTTPException(502, "We couldn't send this email right now. Please try again, or download the PDF and send it from your inbox.")
    sent_at = now_iso()
    await db.invoices.update_one(
        {"id": invoice_id, "user_id": user["id"]},
        {"$set": {"status": "Sent", "sent_at": sent_at, "message_id": msg_id,
                  # Clear any prior delivery / reminder state on a fresh send.
                  "delivered_at": None, "opened_at": None, "last_opened_at": None,
                  "opens_count": 0, "bounce_status": None, "bounce_at": None,
                  "bounce_message": None,
                  "reminders_sent_count": 0, "last_reminder_sent_at": None}},
    )
    return {"ok": True, "message_id": msg_id, "sent_at": sent_at}


@router.post("/{invoice_id}/follow-up")
async def follow_up_invoice(invoice_id: str, payload: SendInvoiceIn, request: Request):
    """Manual follow-up email for an already-sent invoice. Re-attaches the
    PDF, sends through Postmark, and bumps the reminder counter so the
    automated 7/14/30-day scheduler doesn't double-fire on top of this.

    Reuses SendInvoiceIn since the payload shape is identical (to/cc/subject/body).
    """
    user = await require_active_subscription(request)
    inv = await db.invoices.find_one(
        {"id": invoice_id, "user_id": user["id"]}, {"_id": 0}
    )
    if not inv:
        raise HTTPException(404, "Invoice not found.")
    if inv.get("status") != "Sent":
        raise HTTPException(
            400, "Follow-ups are only for sent invoices. Send the invoice first."
        )
    pdf_bytes, inv2, _ = await _build_pdf_for(invoice_id, user)
    fname = f"Invoice-{inv2['invoice_number']}.pdf"
    ok, msg_id = send_invoice_email(
        to_email=payload.to_email,
        subject=payload.subject,
        body_text=payload.body,
        pdf_bytes=pdf_bytes,
        pdf_filename=fname,
        cc=payload.cc,
        reporter_name=user.get("name"),
    )
    if not ok:
        raise HTTPException(
            502,
            "We couldn't send this follow-up right now. Please try again, "
            "or download the PDF and send it from your inbox.",
        )
    now = now_iso()
    await db.invoices.update_one(
        {"id": invoice_id, "user_id": user["id"]},
        {
            "$set": {
                "last_reminder_sent_at": now,
                "last_reminder_message_id": msg_id,
                # The new send creates a fresh message_id we should track
                # for open/delivery webhooks. Don't reset opens_count — the
                # client may still open the original AND this one; we want
                # to know.
                "message_id": msg_id,
            },
            "$inc": {"reminders_sent_count": 1},
        },
    )
    return {
        "ok": True,
        "message_id": msg_id,
        "sent_at": now,
        "reminders_sent_count": int(inv.get("reminders_sent_count") or 0) + 1,
    }


# ---------------------------------------------------------------- bulk -----
def _seed_lines_from_rates(rates: dict) -> list:
    """Build a sensible default line-item skeleton from a client's saved rates.
    Pages default to 0 so the reporter fills them in before sending."""
    lines: list = []
    if rates.get("appearance_fee"):
        lines.append({
            "type": "appearance_fee", "label": "Appearance fee",
            "detail": None, "quantity": None, "rate": None,
            "amount": float(rates["appearance_fee"]),
        })
    if rates.get("original_per_page"):
        lines.append({
            "type": "original_transcript", "label": "Original transcript",
            "detail": "0 pages", "quantity": 0, "rate": float(rates["original_per_page"]),
            "amount": 0.0,
        })
    if rates.get("copy_per_page"):
        lines.append({
            "type": "copy", "label": "Copy",
            "detail": "0 pages", "quantity": 0, "rate": float(rates["copy_per_page"]),
            "amount": 0.0,
        })
    if not lines:
        # No defaults — leave a placeholder so the invoice isn't empty.
        lines.append({
            "type": "appearance_fee", "label": "Appearance fee",
            "detail": None, "quantity": None, "rate": None, "amount": 0.0,
        })
    return lines


@router.post("/bulk-generate")
async def bulk_generate(payload: BulkGenerateIn, request: Request):
    """Create one Draft invoice per supplied job_id using each job's client
    default rates. Skips jobs that already have an invoice. Returns a per-job
    result so the UI can show what was/wasn't generated."""
    user = await require_active_subscription(request)
    if not payload.job_ids:
        raise HTTPException(400, "Pick at least one job.")

    from datetime import date, timedelta
    today = date.today()
    net_days = int(user.get("default_net_days") or 30)
    due = today + timedelta(days=net_days)

    results: list[dict] = []
    for job_id in payload.job_ids:
        job = await db.jobs.find_one({"id": job_id, "user_id": user["id"]}, {"_id": 0})
        if not job:
            results.append({"job_id": job_id, "ok": False, "reason": "Job not found."})
            continue
        if job.get("invoice_id"):
            results.append({"job_id": job_id, "ok": False, "reason": "Already invoiced."})
            continue
        cl = await db.clients.find_one(
            {"id": job.get("client_id"), "user_id": user["id"]}, {"_id": 0}
        ) or {}
        billed_to_name = (cl.get("name") or "").strip()
        contact = (cl.get("contact_name") or "").strip()
        if contact:
            billed_to_name = (f"{billed_to_name} (Attn: {contact})").strip()

        line_items = _seed_lines_from_rates(cl.get("rates") or {})
        iid = str(uuid.uuid4())
        invoice_number = await next_invoice_number(user["id"])
        doc = {
            "id": iid,
            "user_id": user["id"],
            "invoice_number": invoice_number,
            "job_id": job_id,
            "client_id": job.get("client_id"),
            "invoice_date": today.isoformat(),
            "due_date": due.isoformat(),
            "line_items": line_items,
            "notes": None,
            "payment_instructions": user.get("payment_instructions_default"),
            "billed_to_name": billed_to_name,
            "billed_to_email": cl.get("contact_email"),
            "billed_to_address": cl.get("billing_address"),
            "status": "Draft",
            "total": calc_invoice_total(line_items),
            "sent_at": None,
            "paid_at": None,
            "voided_at": None,
            "created_at": now_iso(),
        }
        await db.invoices.insert_one(doc)
        await db.jobs.update_one(
            {"id": job_id, "user_id": user["id"]},
            {"$set": {"invoice_id": iid, "status": "Invoiced"}},
        )
        results.append({
            "job_id": job_id, "ok": True,
            "invoice_id": iid, "invoice_number": invoice_number,
        })

    return {
        "created": sum(1 for r in results if r["ok"]),
        "skipped": sum(1 for r in results if not r["ok"]),
        "results": results,
    }


@router.post("/bulk-send")
async def bulk_send(payload: BulkSendIn, request: Request):
    """Email each Draft invoice in the list to its resolved recipient
    (ordering attorney's email if known, else billed_to_email). Returns
    per-invoice success / failure so the UI can render a clean report."""
    user = await require_active_subscription(request)
    if not payload.invoice_ids:
        raise HTTPException(400, "Pick at least one invoice.")
    prefix = (payload.subject_prefix or "").strip()

    results: list[dict] = []
    for inv_id in payload.invoice_ids:
        inv = await db.invoices.find_one({"id": inv_id, "user_id": user["id"]}, {"_id": 0})
        if not inv:
            results.append({"invoice_id": inv_id, "ok": False, "reason": "Not found."})
            continue
        if inv.get("status") not in ("Draft", "Sent"):
            results.append({"invoice_id": inv_id, "ok": False, "reason": f"Status is {inv.get('status')}."})
            continue

        # Resolve recipient: prefer ordering attorney's email.
        to_email = inv.get("billed_to_email")
        recipient_name = ""
        if inv.get("job_id"):
            job = await db.jobs.find_one({"id": inv["job_id"], "user_id": user["id"]}, {"_id": 0})
            if job and job.get("ordering_attorney_id"):
                a = await db.attorneys.find_one(
                    {"id": job["ordering_attorney_id"], "user_id": user["id"]}, {"_id": 0}
                )
                if a and a.get("email"):
                    to_email = a["email"]
                    recipient_name = f"{a.get('first_name','')} {a.get('last_name','')}".strip()
        if not to_email:
            results.append({"invoice_id": inv_id, "ok": False,
                            "reason": "No recipient email — set client billing email."})
            continue

        try:
            pdf_bytes, _, _ = await _build_pdf_for(inv_id, user)
        except HTTPException as e:
            results.append({"invoice_id": inv_id, "ok": False, "reason": e.detail})
            continue

        subject = f"Invoice {inv['invoice_number']}"
        if prefix:
            subject = f"{prefix} {subject}"
        body_total = float(inv.get("total") or 0)
        greeting = f"Hello{(' ' + recipient_name) if recipient_name else ''},"
        body = (
            f"{greeting}\n\n"
            f"Please find attached invoice {inv['invoice_number']}.\n\n"
            f"Total due: ${body_total:,.2f}\n\n"
            "Thank you,"
        )
        fname = f"Invoice-{inv['invoice_number']}.pdf"
        ok, msg_id = send_invoice_email(
            to_email=to_email, subject=subject, body_text=body,
            pdf_bytes=pdf_bytes, pdf_filename=fname,
            cc=None, reporter_name=user.get("name"),
        )
        if not ok:
            results.append({"invoice_id": inv_id, "ok": False,
                            "reason": "Postmark refused this send."})
            continue
        sent_at = now_iso()
        await db.invoices.update_one(
            {"id": inv_id, "user_id": user["id"]},
            {"$set": {
                "status": "Sent",
                "sent_at": sent_at,
                "message_id": msg_id,
                "delivered_at": None,
                "opened_at": None,
                "last_opened_at": None,
                "opens_count": 0,
                "bounce_status": None,
            }},
        )
        results.append({
            "invoice_id": inv_id, "ok": True,
            "invoice_number": inv["invoice_number"],
            "to_email": to_email, "message_id": msg_id, "sent_at": sent_at,
        })

    return {
        "sent": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
        "results": results,
    }
