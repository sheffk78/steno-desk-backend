"""Overdue-invoice reminder emails — mounted at /api/reminders.

A reminder fires when ALL of the following are true:
  - invoice.status == "Sent" (NOT Paid, NOT Void, NOT Draft)
  - invoice.due_date is at least 7 days in the past
  - we have a client email to send to
  - the user hasn't opted out (user.auto_reminders_enabled != False)
  - the cadence rules below allow it

Cadence: 1st reminder at 7+ days past due, 2nd at 14+, 3rd at 30+. After
3 reminders we stop nagging. Each invoice tracks:
  - reminders_sent_count: int
  - last_reminder_sent_at: ISO datetime

This module exposes:
  - send_overdue_reminders()  — the batch job (also reused by the cron loop)
  - GET  /reminders/preview   — what would fire right now (debugging UI)
  - POST /reminders/run-now   — manual trigger (debug + ops)
  - GET  /reminders/recent    — recent reminder activity (last 50)
"""
import logging
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request

from auth_core import get_current_user
from db import get_db, now_iso
from email_service import send_overdue_reminder_email
from pdf_generator import generate_invoice_pdf

logger = logging.getLogger(__name__)
router = APIRouter()

# Cadence: nth reminder requires at least this many days past due.
REMINDER_THRESHOLDS = [7, 14, 30]
MAX_REMINDERS = len(REMINDER_THRESHOLDS)


def _days_overdue(due_date_iso: str) -> int:
    """Returns negative if not yet due."""
    try:
        due = date.fromisoformat(due_date_iso)
    except ValueError:
        return -9999
    return (date.today() - due).days


def _next_threshold(reminders_sent: int) -> Optional[int]:
    """Days-past-due required before the next reminder may fire."""
    if reminders_sent >= MAX_REMINDERS:
        return None
    return REMINDER_THRESHOLDS[reminders_sent]


async def _candidates_for_user(user: dict) -> list[dict]:
    """Return invoices that are eligible for a reminder right now, for a
    single user. Read-only — does not mutate state."""
    if user.get("auto_reminders_enabled") is False:
        return []
    cursor = db.invoices.find(
        {"user_id": user["id"], "status": "Sent"},
        {"_id": 0},
    )
    out = []
    async for inv in cursor:
        days = _days_overdue(inv.get("due_date", ""))
        sent_count = int(inv.get("reminders_sent_count") or 0)
        threshold = _next_threshold(sent_count)
        if threshold is None:
            continue  # already at max reminders
        if days < threshold:
            continue
        if not (inv.get("billed_to_email") or "").strip():
            continue  # no client email — can't send
        if inv.get("bounce_status"):
            continue  # don't keep hitting a bounced address
        out.append({"invoice": inv, "days_overdue": days, "reminder_number": sent_count + 1})
    return out


async def send_overdue_reminders(target_user_id: Optional[str] = None) -> dict:
    """Iterate users (or just one if `target_user_id` set), find their
    overdue invoices, send reminders, and record what we sent. Returns a
    summary dict for logging / UI display.
    """
    sent, skipped, failed = 0, 0, 0
    user_filter = {"id": target_user_id} if target_user_id else {}
    cursor = db.users.find(user_filter, {"_id": 0})
    log: list[dict] = []
    async for user in cursor:
        candidates = await _candidates_for_user(user)
        for c in candidates:
            inv = c["invoice"]
            try:
                pdf_bytes = generate_invoice_pdf(inv, user)
                ok, msg_id = send_overdue_reminder_email(
                    user=user,
                    invoice=inv,
                    days_overdue=c["days_overdue"],
                    reminder_number=c["reminder_number"],
                    pdf_bytes=pdf_bytes,
                )
            except Exception as e:
                logger.error(f"overdue reminder generate/send failed for {inv['id']}: {e}")
                failed += 1
                continue
            if not ok:
                failed += 1
                continue
            await db.invoices.update_one(
                {"id": inv["id"], "user_id": user["id"]},
                {"$set": {"last_reminder_sent_at": now_iso(),
                          "last_reminder_message_id": msg_id},
                 "$inc": {"reminders_sent_count": 1}},
            )
            sent += 1
            log.append({
                "invoice_id": inv["id"],
                "invoice_number": inv.get("invoice_number"),
                "to_email": inv.get("billed_to_email"),
                "days_overdue": c["days_overdue"],
                "reminder_number": c["reminder_number"],
            })
        # Track skipped invoices (eligible Sent but not yet at threshold)
        skipped += 0  # placeholder — could compute if useful
    summary = {"sent": sent, "skipped": skipped, "failed": failed, "log": log,
               "ran_at": now_iso()}
    if sent or failed:
        logger.info(f"overdue reminders: {summary}")
    return summary


# --------------------------------------------------------------- endpoints --
@router.get("/preview")
async def preview_reminders(request: Request):
    """What invoices would receive a reminder right now? For UI display."""
    user = await get_current_user(request)
    candidates = await _candidates_for_user(user)
    return {
        "auto_reminders_enabled": user.get("auto_reminders_enabled") is not False,
        "candidates": [
            {
                "invoice_id": c["invoice"]["id"],
                "invoice_number": c["invoice"].get("invoice_number"),
                "billed_to_name": c["invoice"].get("billed_to_name"),
                "billed_to_email": c["invoice"].get("billed_to_email"),
                "total": c["invoice"].get("total"),
                "due_date": c["invoice"].get("due_date"),
                "days_overdue": c["days_overdue"],
                "reminder_number": c["reminder_number"],
            }
            for c in candidates
        ],
    }


@router.post("/run-now")
async def run_now(request: Request):
    """Manually trigger the reminder sweep for the current user only."""
    user = await get_current_user(request)
    return await send_overdue_reminders(target_user_id=user["id"])


@router.get("/recent")
async def recent_reminders(request: Request):
    """Last 50 invoices that received a reminder for this user."""
    user = await get_current_user(request)
    cursor = db.invoices.find(
        {"user_id": user["id"], "last_reminder_sent_at": {"$ne": None}},
        {"_id": 0, "id": 1, "invoice_number": 1, "billed_to_name": 1,
         "billed_to_email": 1, "total": 1, "due_date": 1, "status": 1,
         "reminders_sent_count": 1, "last_reminder_sent_at": 1},
    ).sort("last_reminder_sent_at", -1)
    return {"reminders": await cursor.to_list(50)}
