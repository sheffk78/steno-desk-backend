"""Stripe subscription billing — mounted at /api/billing.

Flow:
  1. User clicks Upgrade → POST /checkout with {plan, origin}
  2. We create-or-reuse a Stripe Customer for the user, then a Checkout
     Session in mode=subscription. Optionally pass `trial_end` so we honor
     whatever Steno-Desk trial they still have left (no double-charge).
  3. Stripe redirects back to the origin URL on success/cancel.
  4. Stripe POSTs to /webhooks/stripe (a separate router) — that's what
     actually flips `user.subscription_type` from "trial" to
     "active_monthly" / "active_annual".

Manage / cancel: POST /portal returns a Stripe Customer Portal URL.

Why a separate webhook router (routers/stripe_webhooks.py) instead of
keeping it here? Webhook secret + raw-body verification belongs next to
the Postmark webhook so we have one place for all third-party callbacks.
"""
import logging
import os
from datetime import datetime, timezone
from typing import Literal, Optional

import stripe
from fastapi import APIRouter, HTTPException, Request

from auth_core import get_current_user
from db import db, now_iso
from models import StrictModel

logger = logging.getLogger(__name__)
router = APIRouter()

# Plans → Stripe Price IDs. The keys are what the frontend sends; the values
# come from env vars so we can swap test ↔ live without code changes.
PLANS = {
    "monthly": {
        "price_env": "STRIPE_PRICE_MONTHLY",
        "subscription_type": "active_monthly",
        "amount_display": "$39 / month",
    },
    "annual": {
        "price_env": "STRIPE_PRICE_ANNUAL",
        "subscription_type": "active_annual",
        "amount_display": "$249 / year",
    },
    # Founding User Program (2026-10-08): $149 one-time, lifetime access.
    # Documented offer in BRAND-STATUS.md + outreach/outreach-pack.md; Stripe
    # price price_1UOLaZJE7N1Bszdf0M0uJ2AA (Agentic Trust account, one_time).
    # Fulfillment flips subscription_type to "founding_lifetime" in the
    # checkout.session.completed webhook branch for one-time payments.
    "founding": {
        "price_env": "STRIPE_PRICE_FOUNDING",
        "subscription_type": "founding_lifetime",
        "amount_display": "$149 one-time",
        "one_time": True,
        "price_fallback": "price_1UOLaZJE7N1Bszdf0M0uJ2AA",
    },
}


def _stripe_key() -> str:
    """Return the Stripe secret key, raising 503 if it's missing."""
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        raise HTTPException(503, "Billing is not configured. Please contact support.")
    stripe.api_key = key
    return key


def _price_id(plan: str) -> str:
    cfg = PLANS.get(plan)
    if not cfg:
        raise HTTPException(400, "Invalid plan. Use 'monthly', 'annual' or 'founding'.")
    pid = os.environ.get(cfg["price_env"], "").strip()
    if not pid:
        # Founding price is new (Oct 2026): fall back to the live price ID
        # until STRIPE_PRICE_FOUNDING is set in the Railway env.
        pid = (cfg.get("price_fallback") or "").strip()
    if not pid:
        raise HTTPException(503, f"Plan '{plan}' is not configured.")
    return pid


def _trial_unix(user: dict) -> Optional[int]:
    """If the user still has Steno-Desk trial days left, convert their
    `trial_ends_at` (ISO date) to a UNIX timestamp so Stripe doesn't charge
    them until then. Returns None if trial is already gone."""
    raw = user.get("trial_ends_at")
    if not raw:
        return None
    try:
        # Stored as YYYY-MM-DD or full ISO — both parse.
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    # Stripe requires the trial_end to be at least 48 hours in the future
    # for it to honor the trial. Less than that → bill immediately.
    if (dt - now).total_seconds() < 48 * 3600:
        return None
    return int(dt.timestamp())


async def _ensure_customer(user: dict) -> str:
    """Get-or-create a Stripe Customer for this user, caching the ID on the
    user doc. Idempotent."""
    cid = user.get("stripe_customer_id")
    if cid:
        return cid
    customer = stripe.Customer.create(
        email=user["email"],
        name=user.get("name") or user.get("business_name") or None,
        metadata={"user_id": user["id"]},
    )
    await db.users.update_one(
        {"id": user["id"]},
        {"$set": {"stripe_customer_id": customer.id}},
    )
    return customer.id


# --------------------------------------------------------------- endpoints --
class CheckoutIn(StrictModel):
    plan: Literal["monthly", "annual", "founding"]
    origin: str  # frontend's window.location.origin — used to build URLs


@router.post("/checkout")
async def create_checkout(payload: CheckoutIn, request: Request):
    """Create a Stripe Checkout Session. Subscription mode for monthly/annual,
    one-time payment mode for the founding program (2026-10-08). Returns
    {url} for the frontend to redirect to."""
    user = await get_current_user(request)
    _stripe_key()
    cfg = PLANS[payload.plan]
    price_id = _price_id(payload.plan)

    origin = payload.origin.rstrip("/")
    if cfg.get("one_time"):
        # 10-spot cap — the public promise on the landing page is
        # "10 spots, then closes". Enforced here so spot #11 can never
        # reach Stripe checkout. Checked BEFORE customer creation so a
        # refused sale leaves no orphan Stripe customer behind. Fulfilled
        # grants are the counter (abandoned sessions don't consume a spot).
        sold = await db.users.count_documents(
            {"subscription_type": "founding_lifetime"}
        )
        if sold >= 10:
            raise HTTPException(
                409,
                "The founding program is full — all 10 spots are taken. "
                "Join the monthly or annual plan instead.",
            )
        customer_id = await _ensure_customer(user)
        # Founding User Program: single $149 payment, no trial interplay.
        # Webhook fulfillment (stripe_webhooks.py, checkout.session.completed
        # one-time branch) flips subscription_type → "founding_lifetime".
        try:
            session = stripe.checkout.Session.create(
                mode="payment",
                customer=customer_id,
                line_items=[{"price": price_id, "quantity": 1}],
                success_url=f"{origin}/app/settings?billing=success&session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=f"{origin}/app/settings?billing=canceled",
                billing_address_collection="auto",
                metadata={
                    "user_id": user["id"],
                    "plan": "founding",
                    "program": "founding_user_lifetime",
                },
            )
        except stripe.error.StripeError as e:
            logger.error(f"stripe founding checkout create failed: {e}")
            raise HTTPException(502, f"Stripe error: {e.user_message or str(e)}")
        return {"url": session.url, "session_id": session.id}

    subscription_data: dict = {
        "metadata": {"user_id": user["id"], "plan": payload.plan},
    }
    trial_end = _trial_unix(user)
    if trial_end is not None:
        subscription_data["trial_end"] = trial_end

    customer_id = await _ensure_customer(user)
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{origin}/app/settings?billing=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{origin}/app/settings?billing=canceled",
            allow_promotion_codes=True,
            billing_address_collection="auto",
            subscription_data=subscription_data,
            metadata={"user_id": user["id"], "plan": payload.plan},
        )
    except stripe.error.StripeError as e:
        logger.error(f"stripe checkout create failed: {e}")
        raise HTTPException(502, f"Stripe error: {e.user_message or str(e)}")

    return {"url": session.url, "session_id": session.id}


class PortalIn(StrictModel):
    origin: str


@router.post("/portal")
async def create_portal(payload: PortalIn, request: Request):
    """Returns a Stripe Customer Portal URL where the user can update
    payment method, view invoices, or cancel."""
    user = await get_current_user(request)
    _stripe_key()
    if not user.get("stripe_customer_id"):
        raise HTTPException(400, "No billing account. Subscribe first.")
    try:
        portal = stripe.billing_portal.Session.create(
            customer=user["stripe_customer_id"],
            return_url=f"{payload.origin.rstrip('/')}/app/settings",
        )
    except stripe.error.StripeError as e:
        logger.error(f"stripe portal create failed: {e}")
        raise HTTPException(502, f"Stripe error: {e.user_message or str(e)}")
    return {"url": portal.url}


@router.get("/status")
async def billing_status(request: Request):
    """Lightweight subscription read for the frontend. Surfaces what's on
    the user doc (no live Stripe call — webhooks keep these fields fresh).

    Admin users (per ADMIN_EMAILS env var + the hardcoded FOUNDER_EMAIL)
    are treated as forever-free — they always show as active so they're
    never asked to upgrade and never lose access.
    """
    from auth_core import is_admin_email

    user = await get_current_user(request)
    if is_admin_email(user.get("email", "")):
        return {
            "subscription_type": "admin_lifetime",
            "is_active": True,
            "is_beta": False,
            "is_admin_lifetime": True,
            "stripe_customer_id": user.get("stripe_customer_id"),
            "stripe_subscription_id": None,
            "subscription_current_period_end": None,
            "cancel_at_period_end": False,
            "trial_ends_at": None,
            "last_payment_failed_at": None,
        }

    sub_type = user.get("subscription_type")
    # Use the canonical subscription_state helper from auth_core so the
    # /billing/status endpoint and write-gating dependency stay in sync.
    from auth_core import subscription_state
    state = subscription_state(user)
    is_active = state["is_active"]
    return {
        "subscription_type": sub_type,
        "is_active": is_active,
        "is_beta": sub_type == "beta",
        "is_admin_lifetime": False,
        "state_reason": state["reason"],
        "days_left": state["days_left"],
        "stripe_customer_id": user.get("stripe_customer_id"),
        "stripe_subscription_id": user.get("stripe_subscription_id"),
        "subscription_current_period_end": user.get("subscription_current_period_end"),
        "cancel_at_period_end": bool(user.get("cancel_at_period_end")),
        "trial_ends_at": user.get("trial_ends_at"),
        "last_payment_failed_at": user.get("last_payment_failed_at"),
    }


@router.get("/plans")
async def list_plans():
    """Public — for the pricing page / upgrade dialog."""
    return {
        "plans": [
            {"id": "founding", "label": "Founding User", "price": "$149", "interval": "one-time",
             "blurb": "Lifetime access — 10 spots, then closes", "one_time": True},
            {"id": "monthly", "label": "Monthly", "price": "$39", "interval": "month", "blurb": "Pay as you go"},
            {"id": "annual", "label": "Annual", "price": "$249", "interval": "year",
             "blurb": "Save $219 vs. monthly", "savings_pct": 47},
        ],
    }
