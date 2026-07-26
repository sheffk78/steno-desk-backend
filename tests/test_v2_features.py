"""Smoke tests for V2 features: templates, scopists, client portal, scopist portal,
recurring invoices."""
import uuid

import httpx


# -----------------------------------------------------------------------------
# templates
# -----------------------------------------------------------------------------
def test_template_crud_and_create_invoice(client):
    cl = client.post("/clients", json={"name": f"TmplClient {uuid.uuid4().hex[:6]}"}).json()
    tmpl_payload = {
        "name": "Monthly retainer",
        "client_id": cl["id"],
        "line_items": [
            {"type": "appearance_fee", "label": "Appearance fee", "amount": 250.0},
            {"type": "custom", "label": "Retainer fee", "amount": 500.0},
        ],
        "notes": "Standard monthly retainer",
        "payment_instructions": "Net 30",
    }
    r = client.post("/templates", json=tmpl_payload)
    assert r.status_code == 200, r.text
    tid = r.json()["id"]

    assert any(t["id"] == tid for t in client.get("/templates").json())

    r = client.post(f"/templates/{tid}/create-invoice")
    assert r.status_code == 200, r.text
    inv = r.json()
    assert inv["status"] == "Draft"
    assert inv["total"] == 750.0
    assert inv["client_id"] == cl["id"]

    assert client.delete(f"/templates/{tid}").status_code == 200


# -----------------------------------------------------------------------------
# scopists
# -----------------------------------------------------------------------------
def test_scopist_crud_and_token(client):
    r = client.post("/scopists", json={"first_name": "Alex", "last_name": "Park", "email": "alex@example.com"})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["share_token"] and len(s["share_token"]) > 10
    sid = s["id"]
    assert any(x["id"] == sid for x in client.get("/scopists").json())

    r = client.post(f"/scopists/{sid}/regenerate-token")
    assert r.status_code == 200
    assert r.json()["share_token"] != s["share_token"]

    assert client.delete(f"/scopists/{sid}").status_code == 200


# -----------------------------------------------------------------------------
# scopist portal (public, no auth)
# -----------------------------------------------------------------------------
def test_scopist_portal_flow(client, api_url):
    cl = client.post("/clients", json={"name": f"SPClient {uuid.uuid4().hex[:6]}"}).json()
    sc = client.post("/scopists", json={"first_name": "Jamie", "last_name": "Lee"}).json()

    job = client.post("/jobs", json={
        "client_id": cl["id"], "witness": "Doe", "job_date": "2026-02-12",
        "status": "Completed", "scopist_id": sc["id"], "scopist_status": "Assigned",
    }).json()

    token = sc["share_token"]
    with httpx.Client(base_url=api_url, timeout=20.0) as c:  # no auth
        r = c.get(f"/portal/scopist/{token}")
        assert r.status_code == 200
        body = r.json()
        assert body["scopist"]["first_name"] == "Jamie"
        assert any(j["id"] == job["id"] for j in body["jobs"])

        r = c.post(f"/portal/scopist/{token}/jobs/{job['id']}/start")
        assert r.status_code == 200
        r = c.post(f"/portal/scopist/{token}/jobs/{job['id']}/complete")
        assert r.status_code == 200

        r = c.get(f"/portal/scopist/{token}")
        completed = [j for j in r.json()["jobs"] if j["id"] == job["id"]][0]
        assert completed["scopist_status"] == "Completed"
        assert completed.get("scoping_completed_at")

        # invalid token
        r = c.get("/portal/scopist/not-a-token")
        assert r.status_code == 404


# -----------------------------------------------------------------------------
# client portal (public invoice magic link)
# -----------------------------------------------------------------------------
def test_invoice_portal_share(client, api_url):
    cl = client.post("/clients", json={
        "name": f"PortalClient {uuid.uuid4().hex[:6]}", "contact_email": "billing@example.com",
    }).json()
    inv = client.post("/invoices", json={
        "client_id": cl["id"], "invoice_date": "2026-02-10", "due_date": "2026-03-12",
        "line_items": [{"type": "appearance_fee", "label": "Appearance fee", "amount": 300.0}],
    }).json()

    r = client.post(f"/portal/invoice/{inv['id']}/share-link")
    assert r.status_code == 200, r.text
    token = r.json()["share_token"]
    assert "/portal/invoice/" in r.json()["url"]

    with httpx.Client(base_url=api_url, timeout=20.0) as c:  # no auth
        r = c.get(f"/portal/invoice/{token}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["invoice"]["invoice_number"] == inv["invoice_number"]
        assert body["reporter"]["email"]
        # PDF
        r = c.get(f"/portal/invoice/{token}/pdf")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"

        # invalid token
        assert c.get("/portal/invoice/garbage").status_code == 404

    # regenerate revokes old token
    r = client.post(f"/portal/invoice/{inv['id']}/regenerate-token")
    new_token = r.json()["share_token"]
    assert new_token != token
    with httpx.Client(base_url=api_url, timeout=20.0) as c:
        assert c.get(f"/portal/invoice/{token}").status_code == 404
        assert c.get(f"/portal/invoice/{new_token}").status_code == 200


# -----------------------------------------------------------------------------
# recurring invoices
# -----------------------------------------------------------------------------
def test_recurring_run_now(client):
    cl = client.post("/clients", json={"name": f"RecurringClient {uuid.uuid4().hex[:6]}"}).json()
    payload = {
        "name": "Big Co retainer",
        "client_id": cl["id"],
        "frequency": "monthly",
        "day_of_month": 1,
        "next_run_date": "2026-03-01",
        "line_items": [
            {"type": "custom", "label": "Monthly retainer", "amount": 1500.0},
        ],
        "notes": "Auto-generated retainer",
        "active": True,
    }
    r = client.post("/recurring", json=payload)
    assert r.status_code == 200, r.text
    rec = r.json()
    rid = rec["id"]
    assert rec["runs_count"] == 0

    # Run-now → materializes Draft invoice and bumps next_run_date forward
    r = client.post(f"/recurring/{rid}/run-now")
    assert r.status_code == 200, r.text
    inv = r.json()
    assert inv["status"] == "Draft"
    assert inv["total"] == 1500.0

    # Verify schedule advanced
    r = client.get(f"/recurring/{rid}")
    rec2 = r.json()
    assert rec2["runs_count"] == 1
    assert rec2["last_invoice_id"] == inv["id"]
    assert rec2["next_run_date"] == "2026-04-01"  # bumped from 2026-03-01

    # Update + delete
    r = client.put(f"/recurring/{rid}", json={**payload, "active": False, "next_run_date": "2026-05-01"})
    assert r.status_code == 200
    assert r.json()["active"] is False

    assert client.delete(f"/recurring/{rid}").status_code == 200


def test_recurring_weekly_bump(client):
    cl = client.post("/clients", json={"name": f"WeeklyClient {uuid.uuid4().hex[:6]}"}).json()
    payload = {
        "name": "Weekly", "client_id": cl["id"], "frequency": "weekly",
        "day_of_week": 1, "next_run_date": "2026-02-09",
        "line_items": [{"type": "custom", "label": "Weekly", "amount": 100.0}],
    }
    rec = client.post("/recurring", json=payload).json()
    client.post(f"/recurring/{rec['id']}/run-now")
    r = client.get(f"/recurring/{rec['id']}")
    assert r.json()["next_run_date"] == "2026-02-16"  # +7 days
