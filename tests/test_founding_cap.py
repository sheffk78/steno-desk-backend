"""10-spot founding cap regression test (2026-10-08 hardening).

Seeds 10 founding_lifetime users directly in the test DB, then verifies:
founding checkout -> 409, monthly checkout still works, cleanup runs.
Requires a live server at TEST_BASE_URL (default http://localhost:8001)
with STRIPE_SECRET_KEY set (skips otherwise — monthly check needs Stripe).
"""
import os
import uuid

import httpx
import pytest
import pymongo

BASE = os.environ.get("TEST_BASE_URL", "http://localhost:8001")
API = f"{BASE}/api"


def _server_stripe_configured() -> bool:
    return (
        os.environ.get("SERVER_STRIPE_KEY_PRESENT") == "1"
        or bool(os.environ.get("STRIPE_SECRET_KEY"))
    )


def _checkout(c, token, plan):
    """POST /billing/checkout. Retries transient Stripe 5xx (bursty suites
    can trip rate limits); skips if the server isn't reachable (boot race,
    not a code failure)."""
    last = None
    for _ in range(3):
        try:
            r = c.post("/billing/checkout", json={"plan": plan, "origin": "https://stenodesk.co"},
                       headers={"Authorization": f"Bearer {token}"})
        except httpx.ConnectError:
            pytest.skip(f"server not reachable at {BASE} — boot race, not a code failure")
        if r.status_code == 200 or r.status_code < 500:
            return r
        last = r
    return last


def test_founding_cap_enforced():
    if not _server_stripe_configured():
        pytest.skip("monthly-check leg needs a Stripe key on the server")
    if "MONGO_URL" not in os.environ or "DB_NAME" not in os.environ:
        pytest.skip("MONGO_URL/DB_NAME required for direct seeding")

    client = pymongo.MongoClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    db.users.delete_many({"email": {"$regex": "captest_"}})
    docs = []
    for i in range(10):
        docs.append({
            "id": str(uuid.uuid4()),
            "email": f"captest_{i}_{uuid.uuid4().hex[:6]}@test.com",
            "name": "Cap Test",
            "subscription_type": "founding_lifetime",
            "trial_ends_at": "2020-01-01T00:00:00+00:00",
            "is_active": True,
            "created_at": "2026-10-08T00:00:00+00:00",
        })
    db.users.insert_many(docs)

    # A fresh buyer signs up
    email = f"captest_buyer_{uuid.uuid4().hex[:6]}@test.com"
    with httpx.Client(base_url=API, timeout=20.0) as c:
        try:
            r = c.post("/auth/signup", json={"email": email, "password": "depo1234", "name": "Cap Buyer"})
        except httpx.ConnectError:
            pytest.skip(f"server not reachable at {BASE} — boot race, not a code failure")
        assert r.status_code == 200, r.text
        tok = r.json()["access_token"]

        # Founding checkout must be REFUSED with 409 at the cap
        r2 = _checkout(c, tok, "founding")
        assert r2.status_code == 409, (r2.status_code, r2.text)
        assert "full" in r2.json()["detail"].lower()
        print(f"PASS: at cap -> founding checkout 409: {r2.json()['detail'][:60]}")

        # Monthly checkout still works
        r3 = _checkout(c, tok, "monthly")
        assert r3 is not None and r3.status_code == 200, (r3.status_code if r3 else None,
                                                          r3.text[:160] if r3 else "")
        assert r3.json().get("url", "").startswith("https://checkout.stripe.com")
        print("PASS: monthly checkout unaffected by cap (200 -> stripe)")

    n = db.users.delete_many({"email": {"$regex": "captest_"}}).deleted_count
    client.close()
    assert n >= 11  # 10 founders + buyer