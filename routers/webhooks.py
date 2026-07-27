"""Postmark webhook receiver — mounted at /api/webhooks.

Postmark POSTs delivery / open / bounce events here. We look up the invoice
by the MessageID we stored when sending and update `delivered_at`,
`opened_at`, `opens_count`, and bounce fields.

Security: a shared token (`POSTMARK_WEBHOOK_TOKEN` env var) is checked from
either the `?token=` query string or the `X-Webhook-Token` header. The
endpoint is public (Postmark needs to reach it without auth), so the token
is what gates it.
"""
import logging
import os

from fastapi import APIRouter, HTTPException, Query, Request

from db import get_db, now_iso

logger = logging.getLogger(__name__)
router = APIRouter()


def _check_token(request: Request, token: str | None) -> None:
    expected = os.environ.get("POSTMARK_WEBHOOK_TOKEN", "")
    if not expected:
        # Misconfigured — fail closed so we don't accept unauthenticated traffic.
        raise HTTPException(503, "Webhook receiver is not configured.")
    supplied = token or request.headers.get("x-webhook-token") or ""
    if supplied.strip() != expected:
        raise HTTPException(401, "Invalid webhook token.")


@router.post("/postmark")
async def postmark_webhook(request: Request, token: str | None = Query(None)):
    """Single endpoint that handles every Postmark event type.

    Postmark posts a JSON body whose shape depends on `RecordType`:
      - "Delivery"        — MessageID, DeliveredAt, Recipient
      - "Open"            — MessageID, ReceivedAt, FirstOpen
      - "Bounce"          — MessageID, BouncedAt, Type, Description
      - "SpamComplaint"   — MessageID, BouncedAt
      - "SubscriptionChange" — handled as a no-op
    """
    _check_token(request, token)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body must be JSON.")
    if not isinstance(body, dict):
        raise HTTPException(400, "Unexpected body shape.")

    record_type = (body.get("RecordType") or "").strip()
    message_id = (body.get("MessageID") or "").strip()
    if not message_id:
        # Some Postmark events (subscription changes) won't have MessageID.
        return {"ok": True, "noop": True}

    inv = await db.invoices.find_one({"message_id": message_id}, {"_id": 0})
    if not inv:
        logger.info(f"postmark webhook: no invoice for message_id={message_id} ({record_type})")
        return {"ok": True, "matched": False}

    inv_id = inv["id"]
    user_id = inv["user_id"]

    if record_type == "Delivery":
        await db.invoices.update_one(
            {"id": inv_id, "user_id": user_id},
            {"$set": {
                "delivered_at": body.get("DeliveredAt") or now_iso(),
                "bounce_status": None,
            }},
        )
    elif record_type == "Open":
        # FirstOpen is true the first time; subsequent opens we just bump
        # opens_count + last_opened_at. We also send a one-time "client
        # opened your invoice" email to the reporter on the FIRST open
        # (unless they've opted out).
        set_fields = {"last_opened_at": body.get("ReceivedAt") or now_iso()}
        is_first_open = bool(body.get("FirstOpen")) and not inv.get("opened_at")
        if is_first_open:
            set_fields["opened_at"] = body.get("ReceivedAt") or now_iso()
        await db.invoices.update_one(
            {"id": inv_id, "user_id": user_id},
            {"$set": set_fields, "$inc": {"opens_count": 1}},
        )
        if is_first_open:
            try:
                user = await db.users.find_one({"id": user_id}, {"_id": 0})
                if user and user.get("notify_on_open") is not False and user.get("email"):
                    from email_service import send_invoice_opened_notification
                    app_url = os.environ.get("APP_BASE_URL", "https://stenodesk.co")
                    send_invoice_opened_notification(
                        to_email=user["email"],
                        reporter_name=user.get("name") or user.get("business_name") or "",
                        client_name=inv.get("billed_to_name") or "Your client",
                        invoice_number=inv.get("invoice_number") or inv_id[:8],
                        invoice_total=float(inv.get("total") or 0),
                        app_invoice_url=f"{app_url}/app/invoices/{inv_id}",
                    )
            except Exception as e:
                logger.error(f"open-notification email failed for invoice {inv_id}: {e}")
    elif record_type == "Bounce":
        await db.invoices.update_one(
            {"id": inv_id, "user_id": user_id},
            {"$set": {
                "bounce_status": (body.get("Type") or "Bounce"),
                "bounce_at": body.get("BouncedAt") or now_iso(),
                "bounce_message": body.get("Description") or body.get("Details") or None,
            }},
        )
    elif record_type == "SpamComplaint":
        await db.invoices.update_one(
            {"id": inv_id, "user_id": user_id},
            {"$set": {
                "bounce_status": "SpamComplaint",
                "bounce_at": body.get("BouncedAt") or now_iso(),
                "bounce_message": "Recipient flagged this as spam.",
            }},
        )
    else:
        # Unknown event type — log and acknowledge so Postmark doesn't retry.
        logger.info(f"postmark webhook: unhandled RecordType={record_type}")

    return {"ok": True, "matched": True, "invoice_id": inv_id, "record_type": record_type}
