"""Smoke tests for Stripe billing endpoints.

We don't hit Stripe's live API here — we verify auth gating, payload
validation, the public plans endpoint, status response shape, and that
checkout/portal return 503 when the secret key isn't configured (the
default in CI/dev). The real Stripe round-trip is verified manually.
"""
import os
import uuid

import httpx
import pytest

BASE = "http://localhost:8001"


@pytest.fixture
def auth_token():
    email = f"billing_{uuid.uuid4().hex[:8]}@test.com"
    r = httpx.post(f"{BASE}/api/auth/signup", json={
        "email": email, "password": "depo1234", "name": "Billing Test"
    })
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_plans_public():
    """Plans endpoint requires no auth and lists both plans."""
    r = httpx.get(f"{BASE}/api/billing/plans")
    assert r.status_code == 200
    plans = r.json()["plans"]
    ids = [p["id"] for p in plans]
    assert "monthly" in ids and "annual" in ids
    annual = next(p for p in plans if p["id"] == "annual")
    assert annual["price"] == "$249"


def test_status_requires_auth():
    """No token → 401."""
    r = httpx.get(f"{BASE}/api/billing/status")
    assert r.status_code == 401


def test_status_shape(auth_token):
    """Status returns the expected fields for a fresh trialing user."""
    r = httpx.get(
        f"{BASE}/api/billing/status",
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    for key in ("subscription_type", "is_active", "is_beta",
                "stripe_customer_id", "trial_ends_at"):
        assert key in body
    # Fresh trialing user IS active (within the 7-day window).
    assert body["is_active"] is True
    assert body["is_beta"] is False
    assert body["state_reason"] == "trial"


def test_checkout_invalid_plan(auth_token):
    """Bad plan name → 400."""
    r = httpx.post(
        f"{BASE}/api/billing/checkout",
        headers={"Authorization": f"Bearer {auth_token}"},
        json={"plan": "lifetime", "origin": "https://stenodesk.co"},
    )
    # 422 from pydantic Literal validation OR 400 from our handler
    assert r.status_code in (400, 422)


def test_checkout_503_when_key_missing(auth_token):
    """If STRIPE_SECRET_KEY isn't set, billing endpoint fails closed."""
    if os.environ.get("STRIPE_SECRET_KEY"):
        pytest.skip("Stripe key is configured — skipping fail-closed check")
    r = httpx.post(
        f"{BASE}/api/billing/checkout",
        headers={"Authorization": f"Bearer {auth_token}"},
        json={"plan": "monthly", "origin": "https://stenodesk.co"},
    )
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"].lower()


def test_portal_requires_existing_customer(auth_token):
    """Calling portal before subscribing yields a friendly 400 (no customer)."""
    if not os.environ.get("STRIPE_SECRET_KEY"):
        # Without a key, _stripe_key() short-circuits with 503 before
        # we get to the no-customer check — still a valid fail.
        r = httpx.post(
            f"{BASE}/api/billing/portal",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={"origin": "https://stenodesk.co"},
        )
        assert r.status_code == 503
        return
    r = httpx.post(
        f"{BASE}/api/billing/portal",
        headers={"Authorization": f"Bearer {auth_token}"},
        json={"origin": "https://stenodesk.co"},
    )
    assert r.status_code == 400
    assert "subscribe first" in r.json()["detail"].lower()


def test_stripe_webhook_bad_signature():
    """Unsigned POST → 400. Protects us from forged events."""
    r = httpx.post(
        f"{BASE}/api/webhooks/stripe",
        json={"id": "evt_test", "type": "checkout.session.completed", "data": {"object": {}}},
    )
    assert r.status_code == 400
    assert "signature" in r.json()["detail"].lower()
