"""Smoke regression — one happy-path per resource. Hits the live local
backend via httpx so the full FastAPI + Mongo + JWT stack is exercised."""
import uuid


# -----------------------------------------------------------------------------
# auth
# -----------------------------------------------------------------------------
def test_health_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_me(client):
    r = client.get("/auth/me")
    assert r.status_code == 200
    assert "email" in r.json()


def test_login_and_logout(auth, api_url):
    import httpx
    with httpx.Client(base_url=api_url, timeout=20.0) as c:
        r = c.post("/auth/login", json={"email": auth["email"], "password": auth["password"]})
        assert r.status_code == 200
        r2 = c.post("/auth/logout")
        assert r2.status_code == 200


# -----------------------------------------------------------------------------
# clients & attorneys
# -----------------------------------------------------------------------------
def test_client_crud(client):
    payload = {"name": f"Smoke Firm {uuid.uuid4().hex[:6]}", "type": "Law Firm"}
    r = client.post("/clients", json=payload)
    assert r.status_code == 200, r.text
    cid = r.json()["id"]

    r = client.get("/clients")
    assert any(c["id"] == cid for c in r.json())

    r = client.put(f"/clients/{cid}", json={**payload, "phone": "555-1212"})
    assert r.status_code == 200

    r = client.delete(f"/clients/{cid}")
    assert r.status_code == 200


def test_attorney_create(client):
    cl = client.post("/clients", json={"name": f"AttClient {uuid.uuid4().hex[:6]}"}).json()
    r = client.post("/attorneys", json={"first_name": "Jane", "last_name": "Doe", "client_id": cl["id"]})
    assert r.status_code == 200, r.text


# -----------------------------------------------------------------------------
# jobs
# -----------------------------------------------------------------------------
def test_job_crud(client):
    cl = client.post("/clients", json={"name": f"Jclient {uuid.uuid4().hex[:6]}"}).json()
    payload = {
        "client_id": cl["id"], "witness": "Smith", "job_date": "2026-02-12",
        "job_type": "Deposition", "status": "Scheduled",
    }
    r = client.post("/jobs", json=payload)
    assert r.status_code == 200, r.text
    jid = r.json()["id"]
    assert client.get(f"/jobs/{jid}").status_code == 200

    r = client.put(f"/jobs/{jid}", json={**payload, "status": "Completed"})
    assert r.status_code == 200
    assert r.json()["status"] == "Completed"

    assert client.delete(f"/jobs/{jid}").status_code == 200


# -----------------------------------------------------------------------------
# invoices
# -----------------------------------------------------------------------------
def test_invoice_lifecycle(client):
    cl = client.post("/clients", json={"name": f"InvClient {uuid.uuid4().hex[:6]}"}).json()
    inv_payload = {
        "client_id": cl["id"], "invoice_date": "2026-02-10", "due_date": "2026-03-12",
        "line_items": [
            {"type": "appearance_fee", "label": "Appearance fee", "amount": 250.0},
            {"type": "original_transcript", "label": "Original transcript", "quantity": 100, "rate": 4.5, "amount": 450.0},
        ],
    }
    r = client.post("/invoices", json=inv_payload)
    assert r.status_code == 200, r.text
    inv = r.json()
    assert inv["total"] == 700.0
    assert inv["status"] == "Draft"
    iid = inv["id"]

    # list with status filter
    r = client.get("/invoices", params={"status": "Draft"})
    assert any(i["id"] == iid for i in r.json())

    # PDF
    r = client.get(f"/invoices/{iid}/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert len(r.content) > 1000

    # mark paid (with payment record)
    r = client.post(f"/invoices/{iid}/mark-paid", json={
        "amount": 700.0, "payment_date": "2026-02-15", "payment_method": "check", "reference": "1001",
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Paid"

    # list payments
    r = client.get(f"/invoices/{iid}/payments")
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_invoice_void(client):
    cl = client.post("/clients", json={"name": f"VoidClient {uuid.uuid4().hex[:6]}"}).json()
    inv = client.post("/invoices", json={
        "client_id": cl["id"], "invoice_date": "2026-02-10", "due_date": "2026-03-12",
        "line_items": [{"type": "appearance_fee", "label": "Appearance fee", "amount": 100.0}],
    }).json()
    r = client.post(f"/invoices/{inv['id']}/void")
    assert r.status_code == 200
    assert r.json()["status"] == "Void"


# -----------------------------------------------------------------------------
# expenses
# -----------------------------------------------------------------------------
def test_expense_crud(client):
    r = client.post("/expenses", json={
        "date": "2026-02-10", "amount": 42.50, "description": "Coffee w/ atty",
        "category": "Other",
    })
    assert r.status_code == 200, r.text
    eid = r.json()["id"]
    assert client.get("/expenses").status_code == 200
    assert client.delete(f"/expenses/{eid}").status_code == 200


# -----------------------------------------------------------------------------
# dashboard
# -----------------------------------------------------------------------------
def test_dashboard_summary(client):
    r = client.get("/dashboard/summary")
    assert r.status_code == 200
    body = r.json()
    for key in ("billed_this_month", "collected_this_month", "outstanding_count", "outstanding_total"):
        assert key in body


# -----------------------------------------------------------------------------
# leads (public)
# -----------------------------------------------------------------------------
def test_leads_public(api_url):
    import httpx
    email = f"lead-{uuid.uuid4().hex[:8]}@example.com"
    with httpx.Client(base_url=api_url, timeout=20.0) as c:
        r = c.post("/leads", json={"email": email, "source": "pytest"})
        assert r.status_code == 200, r.text
