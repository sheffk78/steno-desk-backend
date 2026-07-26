"""Tests for V2.13 subscription gating + trial-state computation."""
import os
import uuid
from datetime import date, timedelta

import httpx
import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from pymongo import MongoClient

BASE = "http://localhost:8001"
_sync = MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]


def _signup() -> dict:
    email = f"gate_{uuid.uuid4().hex[:8]}@example.com"
    r = httpx.post(f"{BASE}/api/auth/signup", json={
        "email": email, "password": "depo1234", "name": "Gate Test",
    })
    assert r.status_code == 200, r.text
    return {"email": email, "token": r.json()["access_token"],
            "id": r.json()["user"]["id"]}


def _hdr(tok): return {"Authorization": f"Bearer {tok}"}


def _expire_trial(user_id: str):
    """Backdate the trial so it's already expired."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    _sync.users.update_one({"id": user_id}, {"$set": {"trial_ends_at": yesterday}})


def test_subscription_state_active_for_fresh_trial():
    """A fresh signup has a trial that's not expired → /billing/status active."""
    u = _signup()
    r = httpx.get(f"{BASE}/api/billing/status", headers=_hdr(u["token"]))
    assert r.json()["is_active"] is True


def test_create_endpoints_blocked_when_trial_expired():
    """Multiple critical write endpoints should return 402 with a structured
    error code when the user's trial is expired."""
    u = _signup()
    _expire_trial(u["id"])

    # POST /jobs
    r = httpx.post(f"{BASE}/api/jobs", headers=_hdr(u["token"]),
                   json={"witness": "X", "job_date": date.today().isoformat(),
                         "client_id": str(uuid.uuid4()), "status": "Scheduled"})
    assert r.status_code == 402, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "subscription_required"
    assert detail["reason"] == "trial_expired"

    # POST /clients
    r = httpx.post(f"{BASE}/api/clients", headers=_hdr(u["token"]),
                   json={"name": "Acme", "type": "Law Firm"})
    assert r.status_code == 402, r.text

    # POST /expenses
    r = httpx.post(f"{BASE}/api/expenses", headers=_hdr(u["token"]),
                   json={"category": "Other", "amount": 50,
                         "date": date.today().isoformat(),
                         "description": "test"})
    assert r.status_code == 402, r.text

    # POST /invoices (won't reach validation — gating fires first)
    r = httpx.post(f"{BASE}/api/invoices", headers=_hdr(u["token"]),
                   json={"client_id": "x", "job_id": "x",
                         "invoice_date": date.today().isoformat(),
                         "due_date": date.today().isoformat(),
                         "line_items": []})
    assert r.status_code == 402, r.text


def test_read_endpoints_still_work_when_trial_expired():
    """Existing data must remain readable when the trial expires —
    users keep their data even if they don't upgrade."""
    u = _signup()
    # Seed a client + job + invoice BEFORE expiring the trial
    r = httpx.post(f"{BASE}/api/clients", headers=_hdr(u["token"]),
                   json={"name": "Pre-Expiry Client", "type": "Law Firm"})
    client = r.json()
    r = httpx.post(f"{BASE}/api/jobs", headers=_hdr(u["token"]),
                   json={"witness": "Y", "job_date": date.today().isoformat(),
                         "client_id": client["id"], "status": "Completed"})
    job = r.json()

    _expire_trial(u["id"])

    # GETs should still succeed
    for path in ["/clients", "/jobs", "/invoices", "/expenses",
                 f"/clients/{client['id']}", f"/jobs/{job['id']}",
                 "/dashboard/kpis"]:
        r = httpx.get(f"{BASE}/api{path}", headers=_hdr(u["token"]))
        assert r.status_code in (200, 404), f"GET {path} → {r.status_code}: {r.text[:200]}"
        # 404 is acceptable for endpoints without data; 402 is NOT.
        assert r.status_code != 402, f"GET {path} returned 402 but should be readable"


def test_admin_bypasses_gating():
    """Admin users (founder email) can create even with 'expired' trial flags."""
    # Use the seeded admin
    r = httpx.post(f"{BASE}/api/auth/login",
                   json={"email": "support@stenodesk.co", "password": "adminpass123"})
    if r.status_code != 200:
        pytest.skip("Admin account not present")
    tok = r.json()["access_token"]
    # Even if we backdate the admin's trial (shouldn't matter), they bypass
    admin = _sync.users.find_one({"email": "support@stenodesk.co"}, {"id": 1})
    _sync.users.update_one(
        {"id": admin["id"]},
        {"$set": {"trial_ends_at": (date.today() - timedelta(days=99)).isoformat(),
                  "subscription_type": None}},
    )
    # Create a client — should succeed
    r = httpx.post(f"{BASE}/api/clients", headers=_hdr(tok),
                   json={"name": f"Admin gate test {uuid.uuid4().hex[:6]}", "type": "Direct"})
    assert r.status_code == 200, r.text


def test_beta_user_with_no_expiry_bypasses_gating():
    """Comped beta users with no beta_expires_at are forever-active."""
    u = _signup()
    _sync.users.update_one(
        {"id": u["id"]},
        {"$set": {"subscription_type": "beta", "beta_expires_at": None,
                  "trial_ends_at": (date.today() - timedelta(days=99)).isoformat()}},
    )
    r = httpx.post(f"{BASE}/api/clients", headers=_hdr(u["token"]),
                   json={"name": "Beta-comped test", "type": "Direct"})
    assert r.status_code == 200, r.text


def test_beta_user_with_expired_comp_is_gated():
    """Beta users whose beta_expires_at is in the past should be gated."""
    u = _signup()
    _sync.users.update_one(
        {"id": u["id"]},
        {"$set": {"subscription_type": "beta",
                  "beta_expires_at": (date.today() - timedelta(days=1)).isoformat(),
                  "trial_ends_at": (date.today() - timedelta(days=99)).isoformat()}},
    )
    r = httpx.post(f"{BASE}/api/clients", headers=_hdr(u["token"]),
                   json={"name": "Beta-expired test", "type": "Direct"})
    assert r.status_code == 402, r.text
    assert r.json()["detail"]["reason"] == "beta_expired"
