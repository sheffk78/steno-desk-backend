"""Tests for V2.12 admin bulk-delete + admin lifetime billing."""
import os
import uuid

import httpx
import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from pymongo import MongoClient

BASE = "http://localhost:8001"
ADMIN_EMAIL = "support@stenodesk.co"
ADMIN_PASS = "adminpass123"
_sync = MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]


def _ensure_admin() -> str:
    """Return the admin's access token; create if missing."""
    r = httpx.post(f"{BASE}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS})
    if r.status_code != 200:
        # Try to create
        httpx.post(f"{BASE}/api/auth/signup", json={
            "email": ADMIN_EMAIL, "password": ADMIN_PASS, "name": "Admin",
        })
        r = httpx.post(f"{BASE}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS})
    return r.json()["access_token"]


def _signup_random() -> dict:
    email = f"bulk_{uuid.uuid4().hex[:8]}@example.com"
    r = httpx.post(f"{BASE}/api/auth/signup", json={
        "email": email, "password": "depo1234", "name": "Bulk Target",
    })
    assert r.status_code == 200, r.text
    return r.json()["user"]


def _hdr(tok): return {"Authorization": f"Bearer {tok}"}


# ----------------------------------------------------------- admin lifetime --
def test_admin_billing_status_is_lifetime():
    """Admin's /billing/status should return is_admin_lifetime=true with
    subscription_type='admin_lifetime'."""
    tok = _ensure_admin()
    r = httpx.get(f"{BASE}/api/billing/status", headers=_hdr(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_admin_lifetime"] is True
    assert body["is_active"] is True
    assert body["subscription_type"] == "admin_lifetime"
    assert body["trial_ends_at"] is None  # admins have no trial concept


def test_non_admin_billing_status_is_not_lifetime():
    """Regular users should NOT see is_admin_lifetime=true."""
    u = _signup_random()
    tok_r = httpx.post(f"{BASE}/api/auth/login", json={"email": u["email"], "password": "depo1234"})
    tok = tok_r.json()["access_token"]
    r = httpx.get(f"{BASE}/api/billing/status", headers=_hdr(tok))
    body = r.json()
    assert body["is_admin_lifetime"] is False
    # Fresh user is active during the 7-day trial — state_reason="trial"
    assert body["state_reason"] == "trial"


# ------------------------------------------------------- admin bulk-delete --
def test_bulk_delete_requires_admin():
    """Non-admin gets 403."""
    u = _signup_random()
    tok_r = httpx.post(f"{BASE}/api/auth/login", json={"email": u["email"], "password": "depo1234"})
    r = httpx.post(
        f"{BASE}/api/admin/users/bulk-delete",
        headers=_hdr(tok_r.json()["access_token"]),
        json={"user_ids": ["any"]},
    )
    assert r.status_code == 403


def test_bulk_delete_requires_at_least_one_id():
    tok = _ensure_admin()
    r = httpx.post(f"{BASE}/api/admin/users/bulk-delete", headers=_hdr(tok),
                   json={"user_ids": []})
    assert r.status_code == 400


def test_bulk_delete_happy_path():
    tok = _ensure_admin()
    targets = [_signup_random() for _ in range(3)]
    ids = [t["id"] for t in targets]
    # Seed a job for each so we can verify owned-data cleanup
    for t in targets:
        _sync.jobs.insert_one({
            "id": str(uuid.uuid4()), "user_id": t["id"],
            "witness": "X", "job_date": "2026-01-01", "status": "Scheduled",
        })
    r = httpx.post(f"{BASE}/api/admin/users/bulk-delete", headers=_hdr(tok),
                   json={"user_ids": ids})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted_users"] == 3
    assert set(body["deleted_user_ids"]) == set(ids)
    assert body["owned_data_removed"].get("jobs") == 3
    # Confirm gone from DB
    assert _sync.users.count_documents({"id": {"$in": ids}}) == 0
    assert _sync.jobs.count_documents({"user_id": {"$in": ids}}) == 0


def test_bulk_delete_skips_admin_emails():
    """Trying to delete an admin user gets skipped, not 500'd."""
    tok = _ensure_admin()
    admin = _sync.users.find_one({"email": ADMIN_EMAIL}, {"id": 1})
    # Also include a non-admin so we have at least 1 deletable
    other = _signup_random()
    r = httpx.post(
        f"{BASE}/api/admin/users/bulk-delete",
        headers=_hdr(tok),
        json={"user_ids": [admin["id"], other["id"]]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["deleted_users"] == 1
    assert any(s["reason"] in ("self", "admin") for s in body["skipped"])
    assert any(s["email"] == ADMIN_EMAIL for s in body["skipped"])


def test_bulk_delete_reports_missing_ids():
    tok = _ensure_admin()
    fake_id = str(uuid.uuid4())
    r = httpx.post(f"{BASE}/api/admin/users/bulk-delete", headers=_hdr(tok),
                   json={"user_ids": [fake_id]})
    body = r.json()
    assert body["deleted_users"] == 0
    assert fake_id in body["missing"]
