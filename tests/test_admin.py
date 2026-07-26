"""Smoke tests for /api/admin/*. Requires ADMIN_EMAILS in .env to include
the admin test account."""
import os
import uuid

import httpx


# This test file assumes support@stenodesk.co is in ADMIN_EMAILS env var
# (which is set in /app/backend/.env as part of the V2.5 build).
ADMIN_EMAIL = "support@stenodesk.co"
ADMIN_PASS = "adminpass123"


def _admin_token(api_url):
    """Sign up (or no-op) admin then return a Bearer token."""
    with httpx.Client(base_url=api_url, timeout=20.0) as c:
        c.post("/auth/signup", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS, "name": "Admin"})
        r = c.post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS})
        assert r.status_code == 200, r.text
        return r.json()["access_token"]


def test_admin_endpoints_require_admin(client):
    """A normal user should get 403 on every /admin/* endpoint."""
    r = client.get("/admin/users")
    assert r.status_code == 403


def test_admin_stats(api_url):
    t = _admin_token(api_url)
    with httpx.Client(base_url=api_url, headers={"Authorization": f"Bearer {t}"}, timeout=20.0) as c:
        r = c.get("/admin/stats")
        assert r.status_code == 200, r.text
        s = r.json()
        for k in ("total_users", "trialing", "subscribed", "beta", "expiring_within_3_days", "signups_last_7_days"):
            assert k in s


def test_admin_extend_trial(api_url):
    t = _admin_token(api_url)
    with httpx.Client(base_url=api_url, headers={"Authorization": f"Bearer {t}"}, timeout=20.0) as c:
        # Create a target test user
        email = f"tgt-{uuid.uuid4().hex[:10]}@example.com"
        c.post("/auth/signup", json={"email": email, "password": "depo1234!", "name": "Target"})
        # Find their id via /admin/users
        users = c.get("/admin/users", params={"q": email}).json()
        assert len(users) >= 1
        uid = users[0]["id"]
        orig_end = users[0]["trial_ends_at"]
        # Extend by 30 days
        r = c.post(f"/admin/users/{uid}/extend-trial", json={"days": 30})
        assert r.status_code == 200, r.text
        updated = r.json()
        assert updated["trial_ends_at"] > orig_end
        assert updated["status"] == "Trialing"


def test_admin_comp_beta_and_revoke(api_url):
    t = _admin_token(api_url)
    with httpx.Client(base_url=api_url, headers={"Authorization": f"Bearer {t}"}, timeout=20.0) as c:
        email = f"tgt2-{uuid.uuid4().hex[:10]}@example.com"
        c.post("/auth/signup", json={"email": email, "password": "depo1234!", "name": "Target"})
        users = c.get("/admin/users", params={"q": email}).json()
        uid = users[0]["id"]

        # Comp
        r = c.post(f"/admin/users/{uid}/comp-beta", json={"expires_at": "2027-12-31"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "Beta (comped)"
        assert r.json()["subscription_type"] == "beta"

        # Revoke
        r = c.post(f"/admin/users/{uid}/revoke-comp")
        assert r.status_code == 200
        assert r.json()["subscription_type"] is None


def test_admin_search_filter(api_url):
    t = _admin_token(api_url)
    with httpx.Client(base_url=api_url, headers={"Authorization": f"Bearer {t}"}, timeout=20.0) as c:
        email = f"srch-{uuid.uuid4().hex[:10]}@example.com"
        c.post("/auth/signup", json={"email": email, "password": "depo1234!", "name": "Search"})
        # Substring search
        r = c.get("/admin/users", params={"q": email[:14]})
        assert r.status_code == 200
        results = r.json()
        assert any(u["email"] == email for u in results)


def test_beta_signup_grants_60_days(api_url):
    """A signup with beta=true in body should get a 60-day trial, not 7."""
    email = f"beta-{uuid.uuid4().hex[:10]}@example.com"
    with httpx.Client(base_url=api_url, timeout=20.0) as c:
        r = c.post("/auth/signup", json={
            "email": email, "password": "depo1234!", "name": "Beta", "beta": True,
        })
        assert r.status_code == 200, r.text
        user = r.json()["user"]
        from datetime import datetime
        starts = datetime.fromisoformat(user["trial_started_at"])
        ends = datetime.fromisoformat(user["trial_ends_at"])
        delta = (ends - starts).days
        assert 59 <= delta <= 60, f"beta trial should be ~60 days, got {delta}"


def test_normal_signup_still_7_days(api_url):
    email = f"normal-{uuid.uuid4().hex[:10]}@example.com"
    with httpx.Client(base_url=api_url, timeout=20.0) as c:
        r = c.post("/auth/signup", json={"email": email, "password": "depo1234!", "name": "Normal"})
        assert r.status_code == 200
        user = r.json()["user"]
        from datetime import datetime
        delta = (datetime.fromisoformat(user["trial_ends_at"]) -
                 datetime.fromisoformat(user["trial_started_at"])).days
        assert 6 <= delta <= 7
