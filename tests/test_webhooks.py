"""Smoke tests for the Postmark webhook receiver at /api/webhooks/postmark.
The endpoint is public but guarded by POSTMARK_WEBHOOK_TOKEN."""
import os
import uuid

import httpx
import pytest


TOKEN = os.environ.get("POSTMARK_WEBHOOK_TOKEN", "jfbF1wuUKwh5rBW8vSpDFPT27SWWZWjG6gX04GMukDo")


def _make_sent_invoice(client) -> tuple[str, str]:
    """Create a Draft invoice + manually set message_id+status=Sent via Mongo.
    We use motor directly so the test doesn't depend on a non-existent
    'set message_id' route."""
    cl = client.post("/clients", json={"name": f"WHC {uuid.uuid4().hex[:6]}"}).json()
    inv = client.post("/invoices", json={
        "client_id": cl["id"], "invoice_date": "2026-02-10", "due_date": "2026-03-12",
        "line_items": [{"type": "appearance_fee", "label": "Appearance fee", "amount": 100.0}],
    }).json()
    fake_msg_id = f"msg-{uuid.uuid4().hex}"

    # Use the same Mongo handle the running server uses (loaded from .env)
    import asyncio
    from db import db as _db  # type: ignore
    asyncio.get_event_loop().run_until_complete(
        _db.invoices.update_one(
            {"id": inv["id"]},
            {"$set": {"message_id": fake_msg_id, "status": "Sent"}},
        )
    )
    return inv["id"], fake_msg_id


def test_webhook_rejects_missing_token(api_url):
    with httpx.Client(base_url=api_url, timeout=20.0) as c:
        r = c.post("/webhooks/postmark", json={"RecordType": "Delivery", "MessageID": "x"})
        assert r.status_code == 401


def test_webhook_rejects_wrong_token(api_url):
    with httpx.Client(base_url=api_url, timeout=20.0) as c:
        r = c.post("/webhooks/postmark?token=nope", json={"RecordType": "Delivery", "MessageID": "x"})
        assert r.status_code == 401


def test_webhook_delivery_updates_invoice(api_url, client):
    inv_id, msg_id = _make_sent_invoice(client)
    with httpx.Client(base_url=api_url, timeout=20.0) as c:
        r = c.post(
            f"/webhooks/postmark?token={TOKEN}",
            json={"RecordType": "Delivery", "MessageID": msg_id, "DeliveredAt": "2026-02-11T10:00:00Z"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["matched"] is True
    inv = client.get(f"/invoices/{inv_id}").json()
    assert inv["delivered_at"] == "2026-02-11T10:00:00Z"


def test_webhook_open_increments_count(api_url, client):
    inv_id, msg_id = _make_sent_invoice(client)
    with httpx.Client(base_url=api_url, timeout=20.0) as c:
        r1 = c.post(
            f"/webhooks/postmark?token={TOKEN}",
            json={"RecordType": "Open", "MessageID": msg_id, "FirstOpen": True, "ReceivedAt": "2026-02-12T08:00:00Z"},
        )
        assert r1.status_code == 200
        r2 = c.post(
            f"/webhooks/postmark?token={TOKEN}",
            json={"RecordType": "Open", "MessageID": msg_id, "FirstOpen": False, "ReceivedAt": "2026-02-13T09:00:00Z"},
        )
        assert r2.status_code == 200
    inv = client.get(f"/invoices/{inv_id}").json()
    assert inv["opened_at"] == "2026-02-12T08:00:00Z"
    assert inv["last_opened_at"] == "2026-02-13T09:00:00Z"
    assert inv["opens_count"] == 2


def test_webhook_bounce_sets_status(api_url, client):
    inv_id, msg_id = _make_sent_invoice(client)
    with httpx.Client(base_url=api_url, timeout=20.0) as c:
        r = c.post(
            f"/webhooks/postmark?token={TOKEN}",
            json={
                "RecordType": "Bounce",
                "MessageID": msg_id,
                "Type": "HardBounce",
                "BouncedAt": "2026-02-11T11:00:00Z",
                "Description": "Recipient does not exist.",
            },
        )
        assert r.status_code == 200
    inv = client.get(f"/invoices/{inv_id}").json()
    assert inv["bounce_status"] == "HardBounce"
    assert "does not exist" in inv["bounce_message"]


def test_webhook_unknown_message_id_is_noop(api_url):
    with httpx.Client(base_url=api_url, timeout=20.0) as c:
        r = c.post(
            f"/webhooks/postmark?token={TOKEN}",
            json={"RecordType": "Delivery", "MessageID": "ghost-id-xyz"},
        )
        assert r.status_code == 200
        assert r.json()["matched"] is False
