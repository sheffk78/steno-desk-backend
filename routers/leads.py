"""Soft email-capture (beta wait list) — mounted at /api/leads"""
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks

from db import get_db, now_iso
from email_service import send_new_lead_notification
from models import LeadIn

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("")
async def create_lead(payload: LeadIn, background: BackgroundTasks):
    """Soft email-capture from the marketing landing page (beta wait list)."""
    email = payload.email.lower().strip()
    source = payload.source or "landing_email_capture"
    doc = {
        "id": str(uuid.uuid4()),
        "email": email,
        "source": source,
        "created_at": now_iso(),
    }
    is_new = False
    try:
        await db.leads.insert_one(doc)
        is_new = True
    except Exception:
        # Duplicate email on the wait list is fine — we want it to be idempotent.
        # Any other error is silently absorbed: the marketing form must never
        # show a backend error to a curious visitor on the landing page.
        pass
    # Only notify support on a NEW lead — repeat submissions of the same
    # email shouldn't spam the inbox.
    if is_new:
        background.add_task(send_new_lead_notification, email, source)
    return {"ok": True}
