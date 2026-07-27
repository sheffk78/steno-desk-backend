"""Stripe webhook receiver — mounted at /api/webhooks/stripe.

We verify the signature using STRIPE_WEBHOOK_SECRET and the raw request
body (NOT the parsed JSON — Stripe signs the bytes verbatim). Events we
care about:

  - checkout.session.completed       → mark user subscribed
  - customer.subscription.updated    → keep current_period_end fresh,
                                       handle plan changes + cancel_at_period_end
  - customer.subscription.deleted    → revert user to trial-expired / free
  - invoice.payment_failed           → flag user, send reminder (don't downgrade —
                                       Stripe retries 3x over ~2 weeks)
  - invoice.payment_succeeded        → clear any prior payment-failure flag

We update `users` via `stripe_customer_id` (set when Checkout starts) so
we don't have to thread the user_id through every event.
"""
import logging
import os
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, HTTPException, Request

from db import get_db, now_iso

logger = logging.getLogger(__name__)
router = APIRouter()


def _plan_from_subscription(sub: dict) -> str:
    """Map a Stripe subscription's price → our subscription_type."""
    price_monthly = os.environ.get("STRIPE_PRICE_MONTHLY", "")
    price_annual = os.environ.get("STRIPE_PRICE_ANNUAL", "")
    try:
        items = sub.get("items", {}).get("data") or []
        if not items:
            return "active_monthly"
        price_id = items[0].get("price", {}).get("id")
    except (AttributeError, KeyError):
        return "active_monthly"
    if price_id == price_annual:
        return "active_annual"
    if price_id == price_monthly:
        return "active_monthly"
    # Unknown price — still mark active so we don't lock them out
    return "active_monthly"


def _period_end_iso(sub: dict) -> str | None:
    ts = sub.get("current_period_end")
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


async def _apply_subscription(sub: dict) -> dict:
    """Persist a Stripe subscription's state onto the matching user. Returns
    a small dict describing what changed for the response body."""
    customer_id = sub.get("customer")
    if not customer_id:
        return {"matched": False, "reason": "no_customer_id"}
    status = sub.get("status")  # active | trialing | past_due | canceled | unpaid | incomplete
    cancel_at_period_end = bool(sub.get("cancel_at_period_end"))
    set_fields: dict = {
        "stripe_subscription_id": sub.get("id"),
        "subscription_current_period_end": _period_end_iso(sub),
        "cancel_at_period_end": cancel_at_period_end,
        "subscription_status_raw": status,
    }
    if status in ("active", "trialing"):
        set_fields["subscription_type"] = _plan_from_subscription(sub)
        if not cancel_at_period_end:
            set_fields["subscribed_at"] = set_fields.get("subscribed_at") or now_iso()
    elif status in ("canceled", "incomplete_expired"):
        set_fields["subscription_type"] = None
        set_fields["canceled_at"] = now_iso()
    # past_due / unpaid → keep them active during retries; webhooks
    # invoice.payment_failed already sets last_payment_failed_at.

    result = await db.users.update_one(
        {"stripe_customer_id": customer_id},
        {"$set": set_fields},
    )
    return {"matched": result.matched_count > 0, "fields": set_fields}


@router.post("/stripe")
async def stripe_webhook(request: Request):
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise HTTPException(503, "Stripe webhook is not configured.")
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.warning(f"stripe webhook signature failed: {e}")
        raise HTTPException(400, "Invalid signature")

    etype = event["type"]
    data = event["data"]["object"]
    logger.info(f"stripe webhook: {etype} id={event.get('id')}")

    if etype == "checkout.session.completed":
        # `data` is a Checkout Session. The subscription ID is on
        # data['subscription']. We fetch it fresh to read price + period_end.
        sub_id = data.get("subscription")
        if sub_id:
            try:
                sub = stripe.Subscription.retrieve(sub_id)
                result = await _apply_subscription(dict(sub))
                return {"ok": True, "type": etype, **result}
            except stripe.error.StripeError as e:
                logger.error(f"checkout.session.completed retrieve failed: {e}")
                return {"ok": True, "type": etype, "note": "could_not_retrieve"}
        return {"ok": True, "type": etype, "note": "no_subscription"}

    if etype in ("customer.subscription.updated", "customer.subscription.created", "customer.subscription.deleted"):
        result = await _apply_subscription(data)
        return {"ok": True, "type": etype, **result}

    if etype == "invoice.payment_failed":
        cid = data.get("customer")
        if cid:
            await db.users.update_one(
                {"stripe_customer_id": cid},
                {"$set": {
                    "last_payment_failed_at": now_iso(),
                    "last_payment_failure_reason": data.get("last_payment_error", {}).get("message")
                        if isinstance(data.get("last_payment_error"), dict) else None,
                }},
            )
        return {"ok": True, "type": etype}

    if etype == "invoice.payment_succeeded":
        cid = data.get("customer")
        if cid:
            await db.users.update_one(
                {"stripe_customer_id": cid},
                {"$set": {"last_payment_failed_at": None, "last_payment_failure_reason": None}},
            )
        return {"ok": True, "type": etype}

    # Ack other event types so Stripe doesn't retry forever.
    return {"ok": True, "type": etype, "note": "unhandled"}
