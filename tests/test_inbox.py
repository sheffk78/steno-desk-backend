"""Smoke tests for V2.2 Inbox / bulk-generate / bulk-send flows."""
import uuid

import httpx


def _make_completed_job(client, client_id, witness="Bulk Test"):
    job = client.post("/jobs", json={
        "client_id": client_id,
        "witness": witness,
        "job_date": "2026-02-09",
        "job_type": "Deposition",
        "status": "Completed",
    }).json()
    return job


# -----------------------------------------------------------------------------
# /dashboard/inbox
# -----------------------------------------------------------------------------
def test_inbox_lists_completed_jobs_without_invoice(client):
    cl = client.post("/clients", json={
        "name": f"InboxCl {uuid.uuid4().hex[:6]}",
        "contact_email": f"inbox-{uuid.uuid4().hex[:8]}@example.com",
        "rates": {"appearance_fee": 250.0, "original_per_page": 4.50},
    }).json()
    job = _make_completed_job(client, cl["id"])

    r = client.get("/dashboard/inbox")
    assert r.status_code == 200, r.text
    body = r.json()
    assert any(j["id"] == job["id"] for j in body["ready_jobs"])
    found = next(j for j in body["ready_jobs"] if j["id"] == job["id"])
    assert found["client_name"]
    assert found["client_rates"]["appearance_fee"] == 250.0


def test_inbox_excludes_already_invoiced_jobs(client):
    cl = client.post("/clients", json={"name": f"IxCl {uuid.uuid4().hex[:6]}"}).json()
    job = _make_completed_job(client, cl["id"])
    # Create an invoice attached to that job
    client.post("/invoices", json={
        "job_id": job["id"], "client_id": cl["id"],
        "invoice_date": "2026-02-10", "due_date": "2026-03-12",
        "line_items": [{"type": "appearance_fee", "label": "Appearance fee", "amount": 250.0}],
    })
    body = client.get("/dashboard/inbox").json()
    assert all(j["id"] != job["id"] for j in body["ready_jobs"])


# -----------------------------------------------------------------------------
# bulk-generate
# -----------------------------------------------------------------------------
def test_bulk_generate_creates_drafts(client):
    cl = client.post("/clients", json={
        "name": f"BulkGen {uuid.uuid4().hex[:6]}",
        "contact_email": f"bg-{uuid.uuid4().hex[:6]}@example.com",
        "rates": {"appearance_fee": 200.0, "original_per_page": 4.0, "copy_per_page": 1.0},
    }).json()
    jobs = [_make_completed_job(client, cl["id"], witness=f"W{i}") for i in range(3)]
    r = client.post("/invoices/bulk-generate", json={"job_ids": [j["id"] for j in jobs]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 3
    assert body["skipped"] == 0
    # Verify each result has a real invoice + appearance line item seeded
    for res in body["results"]:
        assert res["ok"] is True
        inv = client.get(f"/invoices/{res['invoice_id']}").json()
        assert inv["status"] == "Draft"
        labels = [li["label"] for li in inv["line_items"]]
        assert "Appearance fee" in labels
        assert "Original transcript" in labels


def test_bulk_generate_skips_already_invoiced(client):
    cl = client.post("/clients", json={"name": f"Skip {uuid.uuid4().hex[:6]}"}).json()
    job = _make_completed_job(client, cl["id"])
    # First generate creates one
    r1 = client.post("/invoices/bulk-generate", json={"job_ids": [job["id"]]})
    assert r1.json()["created"] == 1
    # Second generate should skip
    r2 = client.post("/invoices/bulk-generate", json={"job_ids": [job["id"]]})
    assert r2.json()["created"] == 0
    assert r2.json()["skipped"] == 1


# -----------------------------------------------------------------------------
# bulk-send
# -----------------------------------------------------------------------------
def test_bulk_send_with_no_recipient_fails_per_item(client):
    """An invoice without billed_to_email should be reported in `failed`,
    not throw. Other invoices in the same call still send."""
    cl = client.post("/clients", json={"name": f"NoEmail {uuid.uuid4().hex[:6]}"}).json()
    inv = client.post("/invoices", json={
        "client_id": cl["id"], "invoice_date": "2026-02-10", "due_date": "2026-03-12",
        "line_items": [{"type": "appearance_fee", "label": "Appearance fee", "amount": 100.0}],
    }).json()
    # Wipe the billed_to_email via direct PUT to force the no-recipient branch
    client.put(f"/invoices/{inv['id']}", json={
        "client_id": cl["id"], "invoice_date": "2026-02-10", "due_date": "2026-03-12",
        "line_items": [{"type": "appearance_fee", "label": "Appearance fee", "amount": 100.0}],
    })
    r = client.post("/invoices/bulk-send", json={"invoice_ids": [inv["id"]]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["failed"] == 1
    assert body["sent"] == 0
    assert "recipient email" in body["results"][0]["reason"].lower()


def test_bulk_send_empty_list_rejects(client):
    r = client.post("/invoices/bulk-send", json={"invoice_ids": []})
    assert r.status_code == 400


def test_bulk_generate_empty_list_rejects(client):
    r = client.post("/invoices/bulk-generate", json={"job_ids": []})
    assert r.status_code == 400
