"""Smoke tests for V1-parity closeout: soft-delete clients/attorneys with
`[Deleted]` lookup, attorney phone field, expanded client rate fields,
expense receipt upload + irs_rate."""
import io
import uuid

import httpx


# -----------------------------------------------------------------------------
# soft-delete clients + lookup
# -----------------------------------------------------------------------------
def test_client_soft_delete_and_lookup(client):
    name = f"SoftDel {uuid.uuid4().hex[:6]}"
    cl = client.post("/clients", json={"name": name, "type": "Other"}).json()
    # Lives in /clients before deletion
    assert any(c["id"] == cl["id"] for c in client.get("/clients").json())

    # Delete → should now be soft-deleted
    assert client.delete(f"/clients/{cl['id']}").status_code == 200

    # /clients (live list) no longer includes it
    live = client.get("/clients").json()
    assert all(c["id"] != cl["id"] for c in live)

    # /clients/_lookup still includes it with is_deleted=True
    look = client.get("/clients/_lookup").json()
    found = [c for c in look if c["id"] == cl["id"]]
    assert len(found) == 1
    assert found[0]["is_deleted"] is True
    assert found[0]["name"] == name


def test_attorney_soft_delete(client):
    cl = client.post("/clients", json={"name": f"AttClient {uuid.uuid4().hex[:6]}"}).json()
    a = client.post("/attorneys", json={
        "first_name": "Phone", "last_name": "Test",
        "phone": "555-1212", "client_id": cl["id"],
    }).json()
    assert a["phone"] == "555-1212"

    assert any(x["id"] == a["id"] for x in client.get("/attorneys").json())
    assert client.delete(f"/attorneys/{a['id']}").status_code == 200
    assert all(x["id"] != a["id"] for x in client.get("/attorneys").json())


def test_client_delete_cascades_attorney_soft_delete(client):
    cl = client.post("/clients", json={"name": f"Cascade {uuid.uuid4().hex[:6]}"}).json()
    a = client.post("/attorneys", json={
        "first_name": "Aa", "last_name": "Bb", "client_id": cl["id"],
    }).json()
    client.delete(f"/clients/{cl['id']}")
    listed = client.get("/attorneys", params={"client_id": cl["id"]}).json()
    assert all(x["id"] != a["id"] for x in listed)


# -----------------------------------------------------------------------------
# expanded client rates + Other type
# -----------------------------------------------------------------------------
def test_client_extra_rates_and_other_type(client):
    payload = {
        "name": f"RateClient {uuid.uuid4().hex[:6]}",
        "type": "Other",
        "rates": {
            "original_per_page": 4.50,
            "rough_draft_flat": 75.0,
            "read_sign_fee": 50.0,
        },
    }
    r = client.post("/clients", json=payload)
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["type"] == "Other"
    assert saved["rates"]["rough_draft_flat"] == 75.0
    assert saved["rates"]["read_sign_fee"] == 50.0


# -----------------------------------------------------------------------------
# expense receipt upload + irs_rate
# -----------------------------------------------------------------------------
def test_expense_with_irs_rate_and_receipt(client):
    # Upload a tiny PNG (1x1) as receipt
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6300010000000500010d0a2db40000000049454e44ae42"
        "6082"
    )
    r = client.post(
        "/uploads/receipt",
        files={"file": ("receipt.png", png_bytes, "image/png")},
    )
    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["path"].startswith("stenodesk/receipt/")
    assert receipt["url"].startswith("/api/files/")

    # Now create an expense referencing that receipt
    payload = {
        "date": "2026-02-12",
        "amount": 35.00,
        "description": "Phoenix → Tucson",
        "category": "Mileage",
        "miles": 50.0,
        "irs_rate": 0.70,
        "receipt_url": receipt["url"],
        "receipt_path": receipt["path"],
        "receipt_content_type": "image/png",
        "notes": "Hartwell depo",
    }
    r = client.post("/expenses", json=payload)
    assert r.status_code == 200, r.text
    e = r.json()
    assert e["irs_rate"] == 0.70
    assert e["receipt_path"] == receipt["path"]


def test_expense_csv_includes_irs_rate(client):
    # Create one mileage expense in 2026
    client.post("/expenses", json={
        "date": "2026-03-04", "amount": 7.00, "description": "Local",
        "category": "Mileage", "miles": 10.0, "irs_rate": 0.70,
    })
    r = client.get("/expenses/export.csv", params={"year": 2026})
    assert r.status_code == 200
    body = r.text
    assert "IRS rate" in body.splitlines()[0]


# -----------------------------------------------------------------------------
# receipt upload rejection
# -----------------------------------------------------------------------------
def test_receipt_upload_rejects_bad_mime(client):
    r = client.post(
        "/uploads/receipt",
        files={"file": ("evil.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 400


def test_receipt_upload_requires_auth(api_url):
    with httpx.Client(base_url=api_url, timeout=20.0) as c:
        r = c.post(
            "/uploads/receipt",
            files={"file": ("a.png", b"x", "image/png")},
        )
        assert r.status_code == 401
