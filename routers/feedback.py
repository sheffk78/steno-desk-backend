"""Exit-intent / visitor feedback (public, no auth) — mounted at /api/feedback.

Receives the ExitIntentFeedback popup on the marketing site: the one feature
the visitor says is missing, optional email, page URL, and source. Stores in
Mongo (feedback collection) and emails support. Public endpoint — must never
leak errors to a leaving visitor, so every failure path returns {"ok": True}.
"""
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Request

from db import db, now_iso
from email_service import send_feedback_notification
from models import FeedbackIn

logger = logging.getLogger(__name__)
router = APIRouter()


def _clean_email(raw) -> str | None:
    """Best-effort email cleanup. A malformed email must never cost us the
    feedback itself, so we keep it only if it looks plausible."""
    if not raw:
        return None
    cleaned = str(raw).strip().lower()
    if 3 <= len(cleaned) <= 320 and "@" in cleaned and "." in cleaned.split("@")[-1]:
        return cleaned
    return None


@router.post("")
async def create_feedback(payload: FeedbackIn, request: Request, background: BackgroundTasks):
    """Exit-intent feedback from the marketing site (public, no auth)."""
    doc = {
        "id": str(uuid.uuid4()),
        "feature": payload.feature.strip()[:2000],
        "email": _clean_email(payload.email),
        "page_url": (payload.page_url or "")[:500] or None,
        "source": (payload.source or "exit_intent")[:120],
        "user_agent": request.headers.get("user-agent", "")[:400],
        "referrer": request.headers.get("referer", "")[:500] or None,
        "created_at": now_iso(),
    }
    try:
        await db.feedback.insert_one(doc)
    except Exception:
        # Absorb entirely: the popup silently dismisses on failure anyway,
        # and a leaving visitor must never see a backend error.
        logger.exception("feedback insert failed")
        return {"ok": True}
    if doc["email"]:
        # Only email support when the visitor left a callback address —
        # anonymous notes stay in the DB for the weekly review.
        background.add_task(send_feedback_notification, doc)
    return {"ok": True}