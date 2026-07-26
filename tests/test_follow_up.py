"""Tests for the manual /follow-up endpoint on invoices."""
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
    email = f"fu_{uuid.uuid4().hex[:8]}@test.com"
    r = httpx.post(f"{BASE}/api/auth/signup", json={
        "email": email, "password": "depo1234", "name": "FU Test",
    })
    assert r.status_code == 200, r.text
    return {"email": email, "token": r.json()["access_token"]}


def _hdr(tok): return {"Authorization": f"Bearer {tok}"}


def _make_invoice(token: str, status: str = "Sent", days_overdue: int = 5,
                  client_email: str = "client@test.com"):
    r = httpx.post(f"{BASE}/api/clients", headers=_hdr(token),
                   json={"name": "C", "type": "Law Firm", "contact_email": client_email})
    client = r.json()
    r = httpx.post(f"{BASE}/api/jobs", headers=_hdr(token),
                   json={"witness": "W", "job_date": date.today().isoformat(),
                         "client_id": client["id"], "status": "Completed"})
    job = r.json()
    due = (date.today() - timedelta(days=days_overdue)).isoformat()
    r = httpx.post(f"{BASE}/api/invoices", headers=_hdr(token),
                   json={"job_id": job["id"], "client_id": client["id"],
                         "invoice_date": (date.today() - timedelta(days=days_overdue+30)).isoformat(),
                         "due_date": due,
                         "line_items": [{"type": "appearance_fee", "label": "X",
                                         "amount": 250.00}]})
    inv = r.json()
    _sync.invoices.update_one({"id": inv["id"]}, {"$set": {"status": status}})
    return inv


def test_follow_up_rejects_draft_invoice():
    u = _signup()
    inv = _make_invoice(u["token"], status="Draft")
    r = httpx.post(f"{BASE}/api/invoices/{inv['id']}/follow-up", headers=_hdr(u["token"]),
                   json={"to_email": "client@test.com", "subject": "x", "body": "y"})
    assert r.status_code == 400
    assert "send the invoice first" in r.json()["detail"].lower()


def test_follow_up_404_for_unknown_invoice():
    u = _signup()
    r = httpx.post(f"{BASE}/api/invoices/does-not-exist/follow-up",
                   headers=_hdr(u["token"]),
                   json={"to_email": "client@test.com", "subject": "x", "body": "y"})
    assert r.status_code == 404


def test_follow_up_isolates_users():
    """User A can't send a follow-up on User B's invoice."""
    a = _signup()
    b = _signup()
    inv = _make_invoice(a["token"], status="Sent")
    r = httpx.post(f"{BASE}/api/invoices/{inv['id']}/follow-up", headers=_hdr(b["token"]),
                   json={"to_email": "x@y.com", "subject": "x", "body": "y"})
    assert r.status_code == 404


def test_follow_up_requires_auth():
    r = httpx.post(f"{BASE}/api/invoices/whatever/follow-up",
                   json={"to_email": "x@y.com", "subject": "x", "body": "y"})
    assert r.status_code == 401


def test_follow_up_bumps_reminder_counter_on_real_send(monkeypatch):
    """When the email actually sends successfully, reminders_sent_count
    increments and the auto-scheduler won't double-fire."""
    u = _signup()
    inv = _make_invoice(u["token"], status="Sent", days_overdue=3)
    # Stub Postmark by patching at the running server level — we can't
    # directly monkeypatch the subprocess. Instead, set a known message_id
    # on the invoice and check that the endpoint either succeeds (real
    # Postmark) or returns 502 (Postmark unavailable). Either way, if the
    # endpoint succeeded we must see the counter incremented.
    r = httpx.post(f"{BASE}/api/invoices/{inv['id']}/follow-up", headers=_hdr(u["token"]),
                   json={"to_email": "client@test.com", "subject": "Following up",
                         "body": "Hi, just checking in."})
    if r.status_code == 502:
        pytest.skip("Postmark unavailable in this environment")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reminders_sent_count"] == 1
    assert body["message_id"]
    # And the invoice doc was updated
    doc = _sync.invoices.find_one({"id": inv["id"]})
    assert doc.get("reminders_sent_count") == 1
    assert doc.get("last_reminder_sent_at")
    # Subsequent auto-reminder logic: this invoice should NOT be a candidate
    # right now (only 3 days overdue, but counter is 1 → next threshold is 14).
    r2 = httpx.get(f"{BASE}/api/reminders/preview", headers=_hdr(u["token"]))
    assert r2.json()["candidates"] == []
