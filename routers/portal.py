"""Public portal endpoints (no JWT) — mounted at /api/portal.

Two flavors:
- /portal/invoice/{token}  — magic-link single-invoice view for clients.
- /portal/scopist/{token}  — magic-link work-list for scopists.

Tokens are stored on the invoice (`share_token`) or scopist (`share_token`)
record. Reporter can revoke by calling the regenerate-token endpoints.
"""
import io
import logging
import secrets
from typing import List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from auth_core import get_current_user
from db import db, now_iso
from email_service import send_invoice_email
from models import PortalShareIn
from pdf_generator import generate_invoice_pdf
from storage_service import get_object

logger = logging.getLogger(__name__)
router = APIRouter()


# -------------------------------------------------------------- invoice ----
async def _ensure_invoice_share_token(invoice_id: str, user_id: str) -> str:
    """Return the invoice's share_token, generating one on first call."""
    inv = await db.invoices.find_one({"id": invoice_id, "user_id": user_id}, {"_id": 0})
    if not inv:
        raise HTTPException(404, "Invoice not found")
    token = inv.get("share_token")
    if not token:
        token = secrets.token_urlsafe(24)
        await db.invoices.update_one(
            {"id": invoice_id, "user_id": user_id},
            {"$set": {"share_token": token}},
        )
    return token


@router.post("/invoice/{invoice_id}/share-link")
async def get_invoice_share_link(invoice_id: str, request: Request):
    """Reporter-only: ensure invoice has a share token and return the public URL."""
    user = await get_current_user(request)
    token = await _ensure_invoice_share_token(invoice_id, user["id"])
    import os
    base = os.environ.get("FRONTEND_URL", "").rstrip("/")
    url = f"{base}/portal/invoice/{token}"
    return {"share_token": token, "url": url}


@router.post("/invoice/{invoice_id}/email-share-link")
async def email_invoice_share_link(invoice_id: str, payload: PortalShareIn, request: Request):
    """Reporter-only: email the magic link to the client (Postmark, no PDF)."""
    user = await get_current_user(request)
    token = await _ensure_invoice_share_token(invoice_id, user["id"])
    inv = await db.invoices.find_one({"id": invoice_id, "user_id": user["id"]}, {"_id": 0})
    to = (payload.to_email or inv.get("billed_to_email") or "").strip()
    if not to:
        raise HTTPException(400, "No recipient email — set the client's billing email or pass to_email.")
    import os
    base = os.environ.get("FRONTEND_URL", "").rstrip("/")
    url = f"{base}/portal/invoice/{token}"
    subject = (payload.subject or f"Invoice {inv['invoice_number']} from {user.get('name') or 'your court reporter'}").strip()
    body_text = (payload.body or
                 f"Your invoice is ready to view and download here:\n\n{url}\n\nThis link is private — please don't share.")
    body_text = f"{body_text}\n\nView invoice: {url}" if url not in body_text else body_text
    ok, msg_id = send_invoice_email(
        to_email=to,
        subject=subject,
        body_text=body_text,
        pdf_bytes=b"",  # no attachment — link only
        pdf_filename="link.txt",
        cc=None,
        reporter_name=user.get("name"),
    )
    if not ok:
        raise HTTPException(502, "We couldn't send this email right now. Please copy the link and email it manually.")
    return {"ok": True, "message_id": msg_id, "url": url}


@router.post("/invoice/{invoice_id}/regenerate-token")
async def regenerate_invoice_token(invoice_id: str, request: Request):
    user = await get_current_user(request)
    new_token = secrets.token_urlsafe(24)
    res = await db.invoices.update_one(
        {"id": invoice_id, "user_id": user["id"]},
        {"$set": {"share_token": new_token}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Invoice not found")
    import os
    base = os.environ.get("FRONTEND_URL", "").rstrip("/")
    return {"share_token": new_token, "url": f"{base}/portal/invoice/{new_token}"}


@router.get("/invoice/{token}")
async def public_invoice_view(token: str):
    """Public — anyone with the token can read the invoice (no PDF, no payment data)."""
    inv = await db.invoices.find_one({"share_token": token}, {"_id": 0})
    if not inv:
        raise HTTPException(404, "This link is invalid or has been revoked.")
    if inv.get("status") == "Void":
        raise HTTPException(410, "This invoice has been voided.")
    cl = await db.clients.find_one({"id": inv.get("client_id"), "user_id": inv["user_id"]}, {"_id": 0}) or {}
    user = await db.users.find_one({"id": inv["user_id"]}, {"_id": 0, "password_hash": 0}) or {}
    reporter = {
        "name": user.get("name"),
        "business_name": user.get("business_name"),
        "email": user.get("email"),
        "phone": user.get("phone"),
        "address_line1": user.get("address_line1"),
        "address_line2": user.get("address_line2"),
        "city": user.get("city"),
        "state": user.get("state"),
        "zip": user.get("zip"),
    }
    return {
        "invoice": {k: v for k, v in inv.items() if k not in ("user_id",)},
        "reporter": reporter,
        "client_name": cl.get("name"),
    }


@router.get("/invoice/{token}/pdf")
async def public_invoice_pdf(token: str):
    """Public PDF download via magic link."""
    inv = await db.invoices.find_one({"share_token": token}, {"_id": 0})
    if not inv:
        raise HTTPException(404, "This link is invalid or has been revoked.")
    if inv.get("status") == "Void":
        raise HTTPException(410, "This invoice has been voided.")
    user = await db.users.find_one({"id": inv["user_id"]}, {"_id": 0, "password_hash": 0}) or {}
    cl = await db.clients.find_one({"id": inv.get("client_id"), "user_id": inv["user_id"]}, {"_id": 0}) or {}
    job = None
    if inv.get("job_id"):
        job = await db.jobs.find_one({"id": inv["job_id"], "user_id": inv["user_id"]}, {"_id": 0})
    reporter = {
        "name": user.get("name") or "Court Reporter",
        "business_name": user.get("business_name"),
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
            logger.warning(f"portal letterhead fetch failed: {e}")
    pdf_bytes = generate_invoice_pdf(inv, reporter, cl, job)
    fname = f"Invoice-{inv['invoice_number']}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{fname}"'},
    )


# -------------------------------------------------------------- scopist ---
@router.get("/scopist/{token}")
async def public_scopist_view(token: str):
    """Public — scopist sees their open + recently completed jobs from this reporter."""
    s = await db.scopists.find_one({"share_token": token, "is_deleted": {"$ne": True}}, {"_id": 0})
    if not s:
        raise HTTPException(404, "This link is invalid or has been revoked.")
    user = await db.users.find_one({"id": s["user_id"]}, {"_id": 0, "password_hash": 0}) or {}
    jobs = await db.jobs.find(
        {"user_id": s["user_id"], "scopist_id": s["id"]},
        {"_id": 0, "user_id": 0},
    ).sort("job_date", -1).to_list(500)
    # Attach client names
    needed = list({j.get("client_id") for j in jobs if j.get("client_id")})
    name_map: dict = {}
    if needed:
        async for c in db.clients.find(
            {"user_id": s["user_id"], "id": {"$in": needed}},
            {"_id": 0, "id": 1, "name": 1},
        ):
            name_map[c["id"]] = c["name"]
    for j in jobs:
        j["client_name"] = name_map.get(j.get("client_id"), "")
    return {
        "scopist": {
            "id": s["id"],
            "first_name": s["first_name"],
            "last_name": s["last_name"],
            "email": s.get("email"),
        },
        "reporter": {
            "name": user.get("name"),
            "business_name": user.get("business_name"),
            "email": user.get("email"),
        },
        "jobs": jobs,
    }


@router.post("/scopist/{token}/jobs/{job_id}/start")
async def public_scopist_start(token: str, job_id: str):
    s = await db.scopists.find_one({"share_token": token, "is_deleted": {"$ne": True}}, {"_id": 0})
    if not s:
        raise HTTPException(404, "This link is invalid or has been revoked.")
    res = await db.jobs.update_one(
        {"id": job_id, "user_id": s["user_id"], "scopist_id": s["id"]},
        {"$set": {"scopist_status": "In Progress"}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Job not found or not assigned to you.")
    return {"ok": True}


@router.post("/scopist/{token}/jobs/{job_id}/complete")
async def public_scopist_complete(token: str, job_id: str):
    s = await db.scopists.find_one({"share_token": token, "is_deleted": {"$ne": True}}, {"_id": 0})
    if not s:
        raise HTTPException(404, "This link is invalid or has been revoked.")
    res = await db.jobs.update_one(
        {"id": job_id, "user_id": s["user_id"], "scopist_id": s["id"]},
        {"$set": {"scopist_status": "Completed", "scoping_completed_at": now_iso()}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Job not found or not assigned to you.")
    return {"ok": True}
