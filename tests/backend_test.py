"""Steno Desk backend regression tests.

Covers: auth (signup/login/me/forgot/reset), tenant isolation, clients, attorneys,
jobs, invoices (sequential numbering, totals, mark-paid cascade, PDF, send),
expenses + CSV export, dashboard summary.

Run:
    pytest /app/backend/tests/backend_test.py -v --tb=short \
        --junitxml=/app/test_reports/pytest/pytest_results.xml
"""
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", os.environ.get("TEST_BASE_URL", "http://localhost:8001")).rstrip("/")
API = f"{BASE_URL}/api"


# ---------- helpers / fixtures ----------
def _unique_email(prefix: str = "test_user") -> str:
    # Backend lowercases emails on signup; keep test fixture lowercase to match.
    return f"{prefix}_{uuid.uuid4().hex[:8]}@stenodesk.example.com".lower()


@pytest.fixture(scope="session")
def user_a():
    """Primary test user — created once, reused across tests."""
    s = requests.Session()
    email = _unique_email("TEST_userA")
    r = s.post(f"{API}/auth/signup", json={"email": email, "password": "depo1234", "name": "Marie A. Chen"})
    assert r.status_code == 200, f"signup failed: {r.status_code} {r.text}"
    data = r.json()
    assert "user" in data and "access_token" in data
    s.headers.update({"Authorization": f"Bearer {data['access_token']}"})
    return {"session": s, "email": email, "user": data["user"], "token": data["access_token"]}


@pytest.fixture(scope="session")
def user_b():
    """Second user for tenant-isolation tests."""
    s = requests.Session()
    email = _unique_email("TEST_userB")
    r = s.post(f"{API}/auth/signup", json={"email": email, "password": "depo1234", "name": "Bob B."})
    assert r.status_code == 200
    data = r.json()
    s.headers.update({"Authorization": f"Bearer {data['access_token']}"})
    return {"session": s, "email": email, "user": data["user"], "token": data["access_token"]}


# ============================== AUTH ==============================
class TestAuth:
    def test_signup_sets_cookies_and_returns_user(self):
        s = requests.Session()
        email = _unique_email("TEST_signup")
        r = s.post(f"{API}/auth/signup", json={"email": email, "password": "depo1234", "name": "Signup Test"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user"]["email"] == email
        assert body["user"]["trial_ends_at"], "trial_ends_at must be set"
        # 7-day trial
        ends = datetime.fromisoformat(body["user"]["trial_ends_at"])
        starts = datetime.fromisoformat(body["user"]["trial_started_at"])
        delta = (ends - starts).days
        assert 6 <= delta <= 7, f"trial should be ~7 days, got {delta}"
        assert "access_token" in body
        # Cookies set?
        assert s.cookies.get("access_token"), "HttpOnly access_token cookie not set"
        assert s.cookies.get("refresh_token"), "HttpOnly refresh_token cookie not set"

    def test_signup_duplicate_returns_409(self, user_a):
        r = requests.post(f"{API}/auth/signup", json={"email": user_a["email"], "password": "depo1234"})
        assert r.status_code == 409
        assert "already exists" in r.json().get("detail", "").lower()

    def test_login_works_with_correct_credentials(self, user_a):
        s = requests.Session()
        r = s.post(f"{API}/auth/login", json={"email": user_a["email"], "password": "depo1234"})
        assert r.status_code == 200
        assert s.cookies.get("access_token")

    def test_login_invalid_password_returns_401(self, user_a):
        r = requests.post(f"{API}/auth/login", json={"email": user_a["email"], "password": "WRONGPASS"})
        assert r.status_code == 401
        assert "incorrect" in r.json().get("detail", "").lower()

    def test_me_with_bearer_token(self, user_a):
        r = user_a["session"].get(f"{API}/auth/me")
        assert r.status_code == 200
        assert r.json()["email"] == user_a["email"]
        assert "password_hash" not in r.json()

    def test_me_with_cookie(self, user_a):
        s = requests.Session()
        r = s.post(f"{API}/auth/login", json={"email": user_a["email"], "password": "depo1234"})
        assert r.status_code == 200
        # Use cookie only (no Authorization header)
        r2 = s.get(f"{API}/auth/me")
        assert r2.status_code == 200
        assert r2.json()["email"] == user_a["email"]

    def test_me_unauthenticated_returns_401(self):
        r = requests.get(f"{API}/auth/me")
        assert r.status_code == 401

    def test_forgot_password_returns_ok_for_unknown_email(self):
        r = requests.post(f"{API}/auth/forgot-password", json={"email": "nonexistent_xyz@stenodesk.example.com"})
        assert r.status_code == 200
        assert r.json().get("ok") is True

    def test_forgot_password_returns_ok_for_known_email(self, user_a):
        r = requests.post(f"{API}/auth/forgot-password", json={"email": user_a["email"]})
        assert r.status_code == 200
        assert r.json().get("ok") is True

    def test_reset_password_invalid_token_returns_400(self):
        r = requests.post(f"{API}/auth/reset-password", json={"token": "bogus-token-xyz", "new_password": "newpass1234"})
        assert r.status_code == 400

    def test_settings_update_persists(self, user_a):
        r = user_a["session"].put(f"{API}/auth/settings", json={
            "name": "Marie A. Chen, RPR",
            "cert_number": "CSR-12345",
            "address": "123 Main St, SF CA 94110",
            "phone": "415-555-0100",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["cert_number"] == "CSR-12345"
        # Verify by GET
        r2 = user_a["session"].get(f"{API}/auth/me")
        assert r2.json()["cert_number"] == "CSR-12345"
        assert r2.json()["name"] == "Marie A. Chen, RPR"


# ============================== CLIENTS / ATTORNEYS ==============================
class TestClientsAndAttorneys:
    def test_create_client_with_rates(self, user_a):
        s = user_a["session"]
        payload = {
            "name": "TEST_Veritext SF",
            "type": "Agency",
            "contact_name": "Jen Smith",
            "contact_email": "jen@veritext.example.com",
            "billing_address": "1 Market St, SF CA",
            "phone": "415-555-0123",
            "rates": {"original_per_page": 4.5, "copy_per_page": 1.25, "appearance_fee": 75},
        }
        r = s.post(f"{API}/clients", json=payload)
        assert r.status_code == 200, r.text
        c = r.json()
        assert c["name"] == "TEST_Veritext SF"
        assert c["rates"]["original_per_page"] == 4.5
        assert c["job_count"] == 0
        # Persistence
        r2 = s.get(f"{API}/clients/{c['id']}")
        assert r2.status_code == 200
        assert r2.json()["rates"]["copy_per_page"] == 1.25
        user_a["client_id"] = c["id"]  # stash for downstream tests

    def test_list_clients_user_scoped(self, user_a):
        r = user_a["session"].get(f"{API}/clients")
        assert r.status_code == 200
        names = [c["name"] for c in r.json()]
        assert any("TEST_Veritext SF" == n for n in names)

    def test_update_client(self, user_a):
        s = user_a["session"]
        cid = user_a["client_id"]
        r = s.put(f"{API}/clients/{cid}", json={
            "name": "TEST_Veritext SF Updated",
            "type": "Agency",
            "rates": {"original_per_page": 5.0, "copy_per_page": 1.5},
        })
        assert r.status_code == 200
        assert r.json()["name"] == "TEST_Veritext SF Updated"
        # Verify
        r2 = s.get(f"{API}/clients/{cid}")
        assert r2.json()["rates"]["original_per_page"] == 5.0

    def test_create_attorney(self, user_a):
        s = user_a["session"]
        r = s.post(f"{API}/attorneys", json={
            "first_name": "Jane",
            "last_name": "Doe",
            "email": "jane.doe@lawfirm.example.com",
            "client_id": user_a["client_id"],
        })
        assert r.status_code == 200, r.text
        a = r.json()
        assert a["last_name"] == "Doe"
        user_a["attorney_id"] = a["id"]

    def test_list_attorneys_filter_by_client(self, user_a):
        r = user_a["session"].get(f"{API}/attorneys", params={"client_id": user_a["client_id"]})
        assert r.status_code == 200
        ids = [a["id"] for a in r.json()]
        assert user_a["attorney_id"] in ids

    def test_tenant_isolation_clients(self, user_a, user_b):
        # User B should NOT see user A's client
        r = user_b["session"].get(f"{API}/clients")
        assert r.status_code == 200
        ids = [c["id"] for c in r.json()]
        assert user_a["client_id"] not in ids
        # Direct access by id
        r2 = user_b["session"].get(f"{API}/clients/{user_a['client_id']}")
        assert r2.status_code == 404


# ============================== JOBS ==============================
class TestJobs:
    def test_create_job(self, user_a):
        s = user_a["session"]
        future = (datetime.now(timezone.utc) + timedelta(days=5)).date().isoformat()
        r = s.post(f"{API}/jobs", json={
            "case_caption": "TEST_Smith v. Jones",
            "case_number": "CV-2026-0001",
            "witness": "John Witness",
            "job_date": future,
            "job_type": "Deposition",
            "client_id": user_a["client_id"],
            "ordering_attorney_id": user_a["attorney_id"],
            "status": "Scheduled",
        })
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["witness"] == "John Witness"
        assert j["status"] == "Scheduled"
        user_a["job_id"] = j["id"]

    def test_list_jobs_with_status_filter(self, user_a):
        r = user_a["session"].get(f"{API}/jobs", params={"status": "Scheduled"})
        assert r.status_code == 200
        for j in r.json():
            assert j["status"] == "Scheduled"
        # All filter
        r2 = user_a["session"].get(f"{API}/jobs", params={"status": "All"})
        assert r2.status_code == 200
        assert len(r2.json()) >= 1

    def test_search_jobs_by_witness(self, user_a):
        r = user_a["session"].get(f"{API}/jobs", params={"q": "John Witness"})
        assert r.status_code == 200
        assert any(j["id"] == user_a["job_id"] for j in r.json())

    def test_search_jobs_by_case_caption(self, user_a):
        r = user_a["session"].get(f"{API}/jobs", params={"q": "Smith v"})
        assert r.status_code == 200
        assert any(j["id"] == user_a["job_id"] for j in r.json())

    def test_update_job_status(self, user_a):
        s = user_a["session"]
        # Fetch then update
        r = s.get(f"{API}/jobs/{user_a['job_id']}")
        body = r.json()
        body["status"] = "Completed"
        r2 = s.put(f"{API}/jobs/{user_a['job_id']}", json={
            "case_caption": body["case_caption"],
            "case_number": body["case_number"],
            "witness": body["witness"],
            "job_date": body["job_date"],
            "job_type": body["job_type"],
            "client_id": body["client_id"],
            "ordering_attorney_id": body.get("ordering_attorney_id"),
            "status": "Completed",
        })
        assert r2.status_code == 200
        assert r2.json()["status"] == "Completed"

    def test_tenant_isolation_jobs(self, user_a, user_b):
        r = user_b["session"].get(f"{API}/jobs/{user_a['job_id']}")
        assert r.status_code == 404


# ============================== INVOICES ==============================
class TestInvoices:
    def test_create_invoice_from_job_sequential_number(self, user_a):
        s = user_a["session"]
        today = datetime.now(timezone.utc).date().isoformat()
        due = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
        r = s.post(f"{API}/invoices", json={
            "job_id": user_a["job_id"],
            "client_id": user_a["client_id"],
            "invoice_date": today,
            "due_date": due,
            "line_items": [
                {"type": "appearance_fee", "label": "Appearance Fee", "amount": 75.0},
                {"type": "original_transcript", "label": "Original Transcript",
                 "quantity": 100, "rate": 5.0, "amount": 500.0},
                {"type": "scopist_deduction", "label": "Scopist Deduction", "amount": -50.0},
            ],
        })
        assert r.status_code == 200, r.text
        inv = r.json()
        assert inv["invoice_number"].startswith("SD-")
        assert inv["total"] == 525.0  # 75 + 500 - 50
        assert inv["status"] == "Draft"
        user_a["invoice_id"] = inv["id"]
        user_a["invoice_number"] = inv["invoice_number"]
        # Job should be auto-set to Invoiced and linked
        rj = s.get(f"{API}/jobs/{user_a['job_id']}")
        assert rj.json()["status"] == "Invoiced"
        assert rj.json()["invoice_id"] == inv["id"]

    def test_invoice_numbers_sequential_never_reused(self, user_a):
        s = user_a["session"]
        today = datetime.now(timezone.utc).date().isoformat()
        due = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
        # Create 2 more invoices to verify increment
        r1 = s.post(f"{API}/invoices", json={
            "client_id": user_a["client_id"], "invoice_date": today, "due_date": due,
            "line_items": [{"type": "custom", "label": "X", "amount": 10}],
        })
        r2 = s.post(f"{API}/invoices", json={
            "client_id": user_a["client_id"], "invoice_date": today, "due_date": due,
            "line_items": [{"type": "custom", "label": "Y", "amount": 20}],
        })
        assert r1.status_code == 200 and r2.status_code == 200
        n0 = int(user_a["invoice_number"].split("-")[1])
        n1 = int(r1.json()["invoice_number"].split("-")[1])
        n2 = int(r2.json()["invoice_number"].split("-")[1])
        assert n1 == n0 + 1
        assert n2 == n0 + 2
        # Delete one, create another — must NOT reuse number
        del_id = r1.json()["id"]
        rd = s.delete(f"{API}/invoices/{del_id}")
        assert rd.status_code == 200
        r3 = s.post(f"{API}/invoices", json={
            "client_id": user_a["client_id"], "invoice_date": today, "due_date": due,
            "line_items": [{"type": "custom", "label": "Z", "amount": 30}],
        })
        assert r3.status_code == 200
        n3 = int(r3.json()["invoice_number"].split("-")[1])
        assert n3 == n2 + 1, f"Sequential numbering broken: deleted {n1}, expected next {n2+1}, got {n3}"

    def test_get_invoice_pdf(self, user_a):
        r = user_a["session"].get(f"{API}/invoices/{user_a['invoice_id']}/pdf")
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("application/pdf")
        assert r.content[:4] == b"%PDF", "PDF magic bytes missing"
        assert len(r.content) > 500

    def test_mark_invoice_paid_cascades_job(self, user_a):
        s = user_a["session"]
        r = s.post(f"{API}/invoices/{user_a['invoice_id']}/mark-paid")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "Paid"
        assert body["paid_at"] is not None
        # Job cascaded
        rj = s.get(f"{API}/jobs/{user_a['job_id']}")
        assert rj.json()["status"] == "Paid"

    def test_send_invoice_postmark(self, user_a):
        s = user_a["session"]
        # Create a new invoice (current one is paid; sending should still work but be cleaner on a draft)
        today = datetime.now(timezone.utc).date().isoformat()
        due = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
        rc = s.post(f"{API}/invoices", json={
            "client_id": user_a["client_id"], "invoice_date": today, "due_date": due,
            "line_items": [{"type": "custom", "label": "Send Test", "amount": 100}],
        })
        assert rc.status_code == 200
        iid = rc.json()["id"]
        r = s.post(f"{API}/invoices/{iid}/send", json={
            "to_email": "test@blackhole.postmarkapp.com",
            "subject": "TEST Invoice",
            "body": "Please see attached.",
        })
        # Either 200 (sent) or 502 (Postmark refused) — both are acceptable per spec
        assert r.status_code in (200, 502), f"Unexpected: {r.status_code} {r.text}"
        if r.status_code == 200:
            assert r.json().get("ok") is True
            # Status should be Sent
            ri = s.get(f"{API}/invoices/{iid}")
            assert ri.json()["status"] == "Sent"

    def test_tenant_isolation_invoices(self, user_a, user_b):
        r = user_b["session"].get(f"{API}/invoices/{user_a['invoice_id']}")
        assert r.status_code == 404
        # PDF endpoint
        r2 = user_b["session"].get(f"{API}/invoices/{user_a['invoice_id']}/pdf")
        assert r2.status_code == 404


# ============================== EXPENSES ==============================
class TestExpenses:
    def test_create_expense(self, user_a):
        today = datetime.now(timezone.utc).date().isoformat()
        r = user_a["session"].post(f"{API}/expenses", json={
            "date": today,
            "amount": 125.50,
            "description": "TEST_CAT software subscription",
            "category": "Software",
        })
        assert r.status_code == 200, r.text
        assert r.json()["category"] == "Software"
        user_a["expense_id"] = r.json()["id"]

    def test_create_mileage_expense(self, user_a):
        today = datetime.now(timezone.utc).date().isoformat()
        r = user_a["session"].post(f"{API}/expenses", json={
            "date": today, "amount": 32.50, "description": "TEST_drive to depo",
            "category": "Mileage", "miles": 50,
        })
        assert r.status_code == 200
        assert r.json()["miles"] == 50

    def test_list_expenses(self, user_a):
        r = user_a["session"].get(f"{API}/expenses")
        assert r.status_code == 200
        assert len(r.json()) >= 2

    def test_csv_export(self, user_a):
        r = user_a["session"].get(f"{API}/expenses/export.csv")
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("text/csv")
        body = r.text
        assert "Date,Description,Category,Amount" in body
        assert "TEST_CAT software subscription" in body


# ============================== DASHBOARD ==============================
class TestDashboard:
    def test_dashboard_summary_shape(self, user_a):
        r = user_a["session"].get(f"{API}/dashboard/summary")
        assert r.status_code == 200
        body = r.json()
        for k in ("billed_this_month", "collected_this_month", "outstanding_count",
                  "outstanding_total", "upcoming_jobs"):
            assert k in body, f"missing {k}"
        # types
        assert isinstance(body["billed_this_month"], (int, float))
        assert isinstance(body["upcoming_jobs"], list)

    def test_dashboard_collected_reflects_paid_invoice(self, user_a):
        r = user_a["session"].get(f"{API}/dashboard/summary")
        body = r.json()
        # We marked one invoice paid this month with total 525.00 — collected must be >= 525
        assert body["collected_this_month"] >= 525.0, f"expected >=525, got {body['collected_this_month']}"

    def test_dashboard_upcoming_includes_scheduled_job(self, user_a):
        # The job we created had future date 5 days out, but was set to Completed/Paid later.
        # Create a fresh future Scheduled job to verify upcoming_jobs surfaces it.
        s = user_a["session"]
        future = (datetime.now(timezone.utc) + timedelta(days=7)).date().isoformat()
        rj = s.post(f"{API}/jobs", json={
            "witness": "TEST_upcoming witness",
            "job_date": future,
            "client_id": user_a["client_id"],
            "status": "Scheduled",
        })
        assert rj.status_code == 200
        r = s.get(f"{API}/dashboard/summary")
        body = r.json()
        wits = [j.get("witness") for j in body["upcoming_jobs"]]
        assert "TEST_upcoming witness" in wits
        # Also client_name should be populated
        for j in body["upcoming_jobs"]:
            if j.get("witness") == "TEST_upcoming witness":
                assert j.get("client_name"), "client_name missing on upcoming job"


# ============================== V1.5 SPEC ALIGNMENT ==============================
class TestLeadsCapture:
    """POST /api/leads — unauthenticated wait-list capture."""

    def test_lead_capture_no_auth(self):
        email = _unique_email("TEST_lead")
        r = requests.post(f"{API}/leads", json={"email": email, "source": "landing_email_capture"})
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True

    def test_lead_capture_no_source_defaults(self):
        email = _unique_email("TEST_lead2")
        r = requests.post(f"{API}/leads", json={"email": email})
        assert r.status_code == 200

    def test_lead_capture_invalid_email_400(self):
        r = requests.post(f"{API}/leads", json={"email": "not-an-email", "source": "x"})
        assert r.status_code == 422

    def test_lead_capture_idempotent_duplicate(self):
        email = _unique_email("TEST_lead_dup")
        r1 = requests.post(f"{API}/leads", json={"email": email, "source": "landing"})
        r2 = requests.post(f"{API}/leads", json={"email": email, "source": "landing"})
        assert r1.status_code == 200 and r2.status_code == 200


class TestSettingsExpanded:
    """PUT /api/auth/settings — V1.5 expanded fields."""

    def test_settings_expanded_persists_all_fields(self, user_a):
        s = user_a["session"]
        payload = {
            "name": "Marie Chen, RPR",
            "business_name": "Chen Court Reporting LLC",
            "cert_type": "RPR",
            "cert_number": "CSR-77777",
            "address_line1": "123 Main St",
            "address_line2": "Suite 200",
            "city": "Phoenix",
            "state": "AZ",
            "zip": "85001",
            "default_net_days": 45,
            "invoice_prefix": "MCH",
            "payment_instructions_default": "Net 45. Make checks payable to Marie Chen.",
        }
        r = s.put(f"{API}/auth/settings", json=payload)
        assert r.status_code == 200, r.text
        body = r.json()
        for k, v in payload.items():
            assert body.get(k) == v, f"settings field {k} expected {v} got {body.get(k)}"
        # Verify GET /me persisted
        r2 = s.get(f"{API}/auth/me")
        for k, v in payload.items():
            assert r2.json().get(k) == v


class TestInvoiceV15:
    """V1.5 invoice changes: billed_to snapshot, void, mark-paid+payments, dashboard exclusion."""

    @pytest.fixture(scope="class")
    def setup_inv(self, user_a):
        s = user_a["session"]
        # Fresh dedicated client + job + invoice for this class
        rc = s.post(f"{API}/clients", json={
            "name": "TEST_V15 Snapshot Client",
            "type": "Law Firm",
            "contact_name": "Pat Lawyer",
            "contact_email": "pat@lawfirm.example.com",
            "billing_address": "1 Lawyer Way, Phoenix AZ 85001",
            "rates": {},
        })
        assert rc.status_code == 200
        cid = rc.json()["id"]
        today = datetime.now(timezone.utc).date().isoformat()
        future = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
        rj = s.post(f"{API}/jobs", json={
            "witness": "TEST_V15 witness",
            "job_date": future,
            "client_id": cid,
            "status": "Completed",
        })
        jid = rj.json()["id"]
        ri = s.post(f"{API}/invoices", json={
            "job_id": jid,
            "client_id": cid,
            "invoice_date": today,
            "due_date": (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat(),
            "line_items": [
                {"type": "appearance_fee", "label": "Appearance", "amount": 100.0},
                {"type": "original_transcript", "label": "Original",
                 "quantity": 50, "rate": 5.0, "amount": 250.0},
            ],
        })
        assert ri.status_code == 200, ri.text
        return {"client_id": cid, "job_id": jid, "invoice": ri.json()}

    def test_invoice_billed_to_snapshot(self, setup_inv):
        inv = setup_inv["invoice"]
        # Snapshot stored on invoice doc
        assert inv["billed_to_name"], "billed_to_name should be snapshotted"
        assert "TEST_V15 Snapshot Client" in inv["billed_to_name"]
        # Contact name appended as Attn:
        assert "Attn: Pat Lawyer" in inv["billed_to_name"]
        assert inv["billed_to_email"] == "pat@lawfirm.example.com"
        assert "Phoenix" in (inv["billed_to_address"] or "")

    def test_billed_to_snapshot_immutable_after_client_edit(self, user_a, setup_inv):
        s = user_a["session"]
        cid = setup_inv["client_id"]
        inv_id = setup_inv["invoice"]["id"]
        original_billed_name = setup_inv["invoice"]["billed_to_name"]
        # Edit the client -> invoice billed_to should NOT change
        r = s.put(f"{API}/clients/{cid}", json={
            "name": "TEST_V15 RENAMED",
            "type": "Law Firm",
            "contact_name": "Different Person",
            "contact_email": "new@lawfirm.example.com",
            "billing_address": "Somewhere else",
            "rates": {},
        })
        assert r.status_code == 200
        ri = s.get(f"{API}/invoices/{inv_id}")
        assert ri.status_code == 200
        assert ri.json()["billed_to_name"] == original_billed_name, "snapshot should not change after client edit"

    def test_mark_paid_with_body_creates_payment_and_cascades(self, user_a, setup_inv):
        s = user_a["session"]
        inv_id = setup_inv["invoice"]["id"]
        job_id = setup_inv["job_id"]
        today = datetime.now(timezone.utc).date().isoformat()
        r = s.post(f"{API}/invoices/{inv_id}/mark-paid", json={
            "amount": 350.0,
            "payment_date": today,
            "payment_method": "check",
            "reference": "Check #1234",
            "notes": "TEST_V15 mark-paid",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "Paid"
        assert body["paid_at"]
        # Job cascaded to Paid
        rj = s.get(f"{API}/jobs/{job_id}")
        assert rj.json()["status"] == "Paid"
        # Payment created
        rp = s.get(f"{API}/invoices/{inv_id}/payments")
        assert rp.status_code == 200
        payments = rp.json()
        assert len(payments) >= 1
        p = payments[0]
        assert p["amount"] == 350.0
        assert p["payment_method"] == "check"
        assert p["reference"] == "Check #1234"
        assert p["invoice_id"] == inv_id

    def test_mark_paid_without_body_backward_compat(self, user_a):
        """Mark-paid still works with no body (V1 backward-compat)."""
        s = user_a["session"]
        today = datetime.now(timezone.utc).date().isoformat()
        due = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
        rc = s.post(f"{API}/invoices", json={
            "client_id": user_a["client_id"], "invoice_date": today, "due_date": due,
            "line_items": [{"type": "custom", "label": "BC test", "amount": 50}],
        })
        iid = rc.json()["id"]
        r = s.post(f"{API}/invoices/{iid}/mark-paid")  # no body
        assert r.status_code == 200
        assert r.json()["status"] == "Paid"
        # No payment record was created when body omitted
        rp = s.get(f"{API}/invoices/{iid}/payments")
        assert rp.status_code == 200
        assert rp.json() == []

    def test_payments_list_tenant_isolation(self, user_a, user_b, setup_inv):
        inv_id = setup_inv["invoice"]["id"]
        r = user_b["session"].get(f"{API}/invoices/{inv_id}/payments")
        # Other tenant gets empty list (filtered by user_id) — not 500
        assert r.status_code == 200
        assert r.json() == []

    def test_void_invoice_rolls_job_back_to_completed(self, user_a):
        s = user_a["session"]
        # Make a brand new job + invoice to void
        future = (datetime.now(timezone.utc) + timedelta(days=10)).date().isoformat()
        rj = s.post(f"{API}/jobs", json={
            "witness": "TEST_void witness", "job_date": future,
            "client_id": user_a["client_id"], "status": "Completed",
        })
        jid = rj.json()["id"]
        today = datetime.now(timezone.utc).date().isoformat()
        due = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
        ri = s.post(f"{API}/invoices", json={
            "job_id": jid, "client_id": user_a["client_id"],
            "invoice_date": today, "due_date": due,
            "line_items": [{"type": "custom", "label": "tovoid", "amount": 99}],
        })
        iid = ri.json()["id"]
        # Job auto-set to Invoiced
        assert s.get(f"{API}/jobs/{jid}").json()["status"] == "Invoiced"
        # Void it
        rv = s.post(f"{API}/invoices/{iid}/void")
        assert rv.status_code == 200, rv.text
        body = rv.json()
        assert body["status"] == "Void"
        assert body["voided_at"]
        # Job rolled back to Completed, invoice_id cleared
        rj2 = s.get(f"{API}/jobs/{jid}")
        assert rj2.json()["status"] == "Completed"
        assert rj2.json().get("invoice_id") in (None, "")

    def test_void_invoice_excluded_from_recent(self, user_a):
        """Voided invoices must not appear in dashboard.recent_invoices."""
        s = user_a["session"]
        today = datetime.now(timezone.utc).date().isoformat()
        due = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
        ri = s.post(f"{API}/invoices", json={
            "client_id": user_a["client_id"], "invoice_date": today, "due_date": due,
            "line_items": [{"type": "custom", "label": "recent-void", "amount": 11}],
        })
        iid = ri.json()["id"]
        s.post(f"{API}/invoices/{iid}/void")
        r = s.get(f"{API}/dashboard/summary")
        body = r.json()
        recent_ids = [ri2["id"] for ri2 in body.get("recent_invoices", [])]
        assert iid not in recent_ids, "voided invoice leaked into recent_invoices"
        for ri2 in body.get("recent_invoices", []):
            assert ri2.get("status") != "Void"

    def test_void_404_for_other_tenant(self, user_a, user_b, setup_inv):
        inv_id = setup_inv["invoice"]["id"]
        r = user_b["session"].post(f"{API}/invoices/{inv_id}/void")
        assert r.status_code == 404


class TestDashboardV15:
    """Dashboard recent_invoices shape + content."""

    def test_dashboard_recent_invoices_shape(self, user_a):
        r = user_a["session"].get(f"{API}/dashboard/summary")
        assert r.status_code == 200
        body = r.json()
        assert "recent_invoices" in body
        assert isinstance(body["recent_invoices"], list)
        assert len(body["recent_invoices"]) <= 5
        if body["recent_invoices"]:
            ri = body["recent_invoices"][0]
            for key in ("id", "invoice_number", "client_name", "invoice_date",
                        "due_date", "total", "status"):
                assert key in ri, f"missing {key} in recent_invoices entry"
            assert ri["status"] != "Void"


# ============================== LETTERHEAD UPLOAD ==============================
# 1x1 PNG (valid)
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf"
    b"\xc0\xc0\xc0\x00\x00\x00\x05\x00\x01^\xf3*:\x00\x00\x00\x00IEND\xaeB`\x82"
)
# Minimal JPG (valid header + EOI)
JPG_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00" + b"\x08" * 64 +
    b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    b"\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\xff\xc4\x00\x14\x10\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\xff\xd9"
)
SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10" fill="red"/></svg>'


class TestLetterheadUpload:
    """V2 letterhead upload via object storage."""

    def test_upload_requires_auth(self):
        r = requests.post(
            f"{API}/uploads/letterhead",
            files={"file": ("lh.png", PNG_BYTES, "image/png")},
        )
        assert r.status_code == 401

    def test_upload_png_persists_fields(self, user_a):
        s = user_a["session"]
        r = s.post(
            f"{API}/uploads/letterhead",
            files={"file": ("lh.png", PNG_BYTES, "image/png")},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["url"].startswith("/api/files/stenodesk/letterhead/")
        assert body["content_type"] == "image/png"
        assert body["size"] == len(PNG_BYTES)
        # Persisted on user
        me = s.get(f"{API}/auth/me").json()
        assert me["letterhead_url"] == body["url"]
        assert me["letterhead_path"].startswith(f"stenodesk/letterhead/{user_a['user']['id']}/")
        assert me["letterhead_content_type"] == "image/png"
        assert me["letterhead_size"] == len(PNG_BYTES)
        assert me["letterhead_uploaded_at"]
        user_a["letterhead_url"] = body["url"]
        user_a["letterhead_path"] = me["letterhead_path"]

    def test_upload_jpg(self, user_a):
        r = user_a["session"].post(
            f"{API}/uploads/letterhead",
            files={"file": ("lh.jpg", JPG_BYTES, "image/jpeg")},
        )
        assert r.status_code == 200, r.text
        assert r.json()["content_type"] == "image/jpeg"
        assert r.json()["url"].endswith(".jpg")

    def test_upload_svg(self, user_a):
        r = user_a["session"].post(
            f"{API}/uploads/letterhead",
            files={"file": ("lh.svg", SVG_BYTES, "image/svg+xml")},
        )
        assert r.status_code == 200, r.text
        assert r.json()["content_type"] == "image/svg+xml"

    def test_upload_rejects_text_plain(self, user_a):
        r = user_a["session"].post(
            f"{API}/uploads/letterhead",
            files={"file": ("note.txt", b"hello", "text/plain")},
        )
        assert r.status_code == 400
        assert "PNG" in r.json().get("detail", "")

    def test_upload_rejects_empty(self, user_a):
        r = user_a["session"].post(
            f"{API}/uploads/letterhead",
            files={"file": ("empty.png", b"", "image/png")},
        )
        assert r.status_code == 400

    def test_upload_rejects_too_large(self, user_a):
        big = b"\x00" * (2 * 1024 * 1024 + 100)  # > 2 MB
        r = user_a["session"].post(
            f"{API}/uploads/letterhead",
            files={"file": ("big.png", big, "image/png")},
        )
        assert r.status_code == 413

    def test_replace_generates_new_path(self, user_a):
        """Re-upload must generate a new UUID path and update DB."""
        s = user_a["session"]
        # Re-upload PNG to ensure latest is PNG (after svg/jpg above)
        r1 = s.post(
            f"{API}/uploads/letterhead",
            files={"file": ("first.png", PNG_BYTES, "image/png")},
        )
        assert r1.status_code == 200
        first_url = r1.json()["url"]
        time.sleep(0.05)
        r2 = s.post(
            f"{API}/uploads/letterhead",
            files={"file": ("second.png", PNG_BYTES, "image/png")},
        )
        assert r2.status_code == 200
        second_url = r2.json()["url"]
        assert first_url != second_url, "replace should generate a new UUID path"
        # User record points to the new file
        me = s.get(f"{API}/auth/me").json()
        assert me["letterhead_url"] == second_url
        user_a["letterhead_url"] = second_url
        user_a["letterhead_path"] = me["letterhead_path"]

    def test_serve_file_with_cookie(self, user_a):
        # Login fresh to get cookie
        cs = requests.Session()
        cs.post(f"{API}/auth/login", json={"email": user_a["email"], "password": "depo1234"})
        url = f"{BASE_URL}{user_a['letterhead_url']}"
        r = cs.get(url)
        assert r.status_code == 200, r.text
        assert r.headers.get("content-type", "").startswith("image/png")
        assert len(r.content) > 0

    def test_serve_file_with_query_auth(self, user_a):
        # No cookie, no Authorization header — only ?auth=<jwt>
        url = f"{BASE_URL}{user_a['letterhead_url']}?auth={user_a['token']}"
        r = requests.get(url)
        assert r.status_code == 200, r.text
        assert r.headers.get("content-type", "").startswith("image/png")

    def test_serve_file_no_auth_returns_401(self, user_a):
        url = f"{BASE_URL}{user_a['letterhead_url']}"
        r = requests.get(url)
        assert r.status_code == 401

    def test_serve_file_cross_tenant_403(self, user_a, user_b):
        """User B authenticated, fetching User A's letterhead -> 403."""
        url = f"{BASE_URL}{user_a['letterhead_url']}"
        r = user_b["session"].get(url)
        assert r.status_code == 403
        assert "Not yours" in r.json().get("detail", "")

    def test_serve_file_nonexistent_returns_404(self, user_a):
        bogus = f"stenodesk/letterhead/{user_a['user']['id']}/{uuid.uuid4()}.png"
        r = user_a["session"].get(f"{BASE_URL}/api/files/{bogus}")
        assert r.status_code == 404

    def _ensure_client(self, user_a):
        if user_a.get("client_id"):
            return user_a["client_id"]
        s = user_a["session"]
        rc = s.post(f"{API}/clients", json={"name": "TEST_LH Client", "type": "Agency", "rates": {}})
        cid = rc.json()["id"]
        user_a["client_id"] = cid
        return cid

    def test_pdf_embeds_letterhead(self, user_a):
        """PDF size with letterhead must exceed no-letterhead baseline AND contain image stream."""
        s = user_a["session"]
        cid = self._ensure_client(user_a)
        # Baseline: clear letterhead, get PDF size
        s.delete(f"{API}/uploads/letterhead")
        today = datetime.now(timezone.utc).date().isoformat()
        due = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
        ri = s.post(f"{API}/invoices", json={
            "client_id": cid, "invoice_date": today, "due_date": due,
            "line_items": [{"type": "custom", "label": "LH test", "amount": 100}],
        })
        assert ri.status_code == 200
        iid = ri.json()["id"]
        r1 = s.get(f"{API}/invoices/{iid}/pdf")
        assert r1.status_code == 200
        assert r1.content[:4] == b"%PDF"
        baseline_size = len(r1.content)

        # Now upload a real-ish PNG (use a slightly larger one to ensure image stream is detectable)
        # Use the bundled marie@stenodesk letterhead approach: just our 1x1 PNG is enough for embedding test
        ru = s.post(
            f"{API}/uploads/letterhead",
            files={"file": ("lh.png", PNG_BYTES, "image/png")},
        )
        assert ru.status_code == 200
        r2 = s.get(f"{API}/invoices/{iid}/pdf")
        assert r2.status_code == 200
        assert r2.content[:4] == b"%PDF"
        with_lh_size = len(r2.content)
        # Image stream typically adds at least a few hundred bytes
        assert with_lh_size > baseline_size, (
            f"PDF with letterhead ({with_lh_size}) should be larger than baseline ({baseline_size})"
        )
        # PDF should contain an image XObject reference
        assert b"/Image" in r2.content or b"/XObject" in r2.content, "PDF lacks image XObject after letterhead upload"

    def test_pdf_works_without_letterhead(self, user_a):
        s = user_a["session"]
        s.delete(f"{API}/uploads/letterhead")
        cid = self._ensure_client(user_a)
        today = datetime.now(timezone.utc).date().isoformat()
        due = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
        ri = s.post(f"{API}/invoices", json={
            "client_id": cid, "invoice_date": today, "due_date": due,
            "line_items": [{"type": "custom", "label": "no-LH PDF", "amount": 50}],
        })
        iid = ri.json()["id"]
        r = s.get(f"{API}/invoices/{iid}/pdf")
        assert r.status_code == 200
        assert r.content[:4] == b"%PDF"

    def test_delete_clears_user_fields(self, user_a):
        s = user_a["session"]
        # Ensure something is uploaded first
        s.post(
            f"{API}/uploads/letterhead",
            files={"file": ("lh.png", PNG_BYTES, "image/png")},
        )
        rd = s.delete(f"{API}/uploads/letterhead")
        assert rd.status_code == 200
        assert rd.json().get("ok") is True
        me = s.get(f"{API}/auth/me").json()
        for k in ("letterhead_path", "letterhead_url", "letterhead_content_type",
                  "letterhead_size", "letterhead_uploaded_at"):
            assert k not in me or me.get(k) in (None, ""), f"{k} should be cleared after delete"


# ============================== ROOT ==============================
def test_api_root():
    r = requests.get(f"{API}/")
    assert r.status_code == 200
    assert r.json().get("ok") is True
