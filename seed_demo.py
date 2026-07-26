"""One-shot demo seeder — creates/refreshes a demo account with V2 sample data
so a tester can hit every feature in 60 seconds:
  - login: marie@stenodesk.example.com / depo1234
  - 2 clients, 1 scopist with 3 assigned jobs, 1 template, 1 recurring schedule,
    2 sent invoices (one with a public share link)

Run: `python /app/backend/seed_demo.py`
Idempotent: re-running cleans the demo account's existing rows first.
"""
import asyncio
import os
import secrets
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from db import db, now_iso, next_invoice_number, calc_invoice_total  # noqa: E402
from auth_core import hash_password  # noqa: E402

EMAIL = "marie@stenodesk.example.com"
PASSWORD = "depo1234"
NAME = "Marie Chen, RPR"


async def upsert_user() -> dict:
    existing = await db.users.find_one({"email": EMAIL})
    if existing:
        # Reset password to known value to keep credentials consistent for testers.
        await db.users.update_one(
            {"id": existing["id"]},
            {"$set": {"password_hash": hash_password(PASSWORD), "name": NAME}},
        )
        return existing
    now = datetime.now(timezone.utc)
    user_id = str(uuid.uuid4())
    doc = {
        "id": user_id,
        "email": EMAIL,
        "password_hash": hash_password(PASSWORD),
        "name": NAME,
        "business_name": "Marie Chen Court Reporting",
        "cert_number": "RPR-AZ-019421",
        "cert_type": "RPR",
        "address_line1": "1240 W Camelback Rd",
        "address_line2": "Suite 220",
        "city": "Phoenix",
        "state": "AZ",
        "zip": "85013",
        "phone": "(602) 555-0142",
        "default_net_days": 30,
        "invoice_prefix": "SD",
        "payment_instructions_default": "Please remit payment within 30 days. Make checks payable to Marie Chen Court Reporting.",
        "subscribed_at": None,
        "subscription_type": None,
        "trial_started_at": now.isoformat(),
        "trial_ends_at": (now + timedelta(days=7)).isoformat(),
        "created_at": now.isoformat(),
    }
    await db.users.insert_one(doc)
    print(f"+ user created: {EMAIL}")
    return doc


async def reset_owned(user_id: str) -> None:
    """Wipe demo-owned rows for a clean re-seed (keeps user record)."""
    for col in (
        "clients", "attorneys", "jobs", "invoices", "payments",
        "expenses", "scopists", "invoice_templates", "recurring_invoices",
    ):
        await db[col].delete_many({"user_id": user_id})


async def main() -> None:
    user = await upsert_user()
    uid = user["id"]
    await reset_owned(uid)

    # --- clients
    client_a = {
        "id": str(uuid.uuid4()), "user_id": uid,
        "name": "Snell & Wilmer LLP", "type": "Law Firm",
        "contact_name": "Theresa Lopez",
        "contact_email": "tlopez@swlaw.example.com",
        "billing_address": "400 E Van Buren St, Suite 1900\nPhoenix, AZ 85004",
        "phone": "(602) 555-0188",
        "rates": {"original_per_page": 4.50, "copy_per_page": 1.25,
                   "appearance_fee": 250.0, "rough_draft_per_page": 1.50},
        "created_at": now_iso(),
    }
    client_b = {
        "id": str(uuid.uuid4()), "user_id": uid,
        "name": "Esquire Deposition Solutions", "type": "Agency",
        "contact_name": "Mark Tran",
        "contact_email": "mtran@esquire.example.com",
        "billing_address": "2700 Centennial Tower\nAtlanta, GA 30303",
        "phone": "(404) 555-0173",
        "rates": {"original_per_page": 4.25, "copy_per_page": 1.10,
                   "appearance_fee": 225.0},
        "created_at": now_iso(),
    }
    await db.clients.insert_many([client_a, client_b])

    # --- scopist with magic-link token
    scopist = {
        "id": str(uuid.uuid4()), "user_id": uid,
        "first_name": "Jamie", "last_name": "Lee",
        "email": "jamie.lee@example.com",
        "rate_per_page": 0.85,
        "notes": "Fast turnaround, prefers 48-hr jobs. Avoids medical depos.",
        "share_token": secrets.token_urlsafe(24),
        "is_deleted": False,
        "created_at": now_iso(),
    }
    await db.scopists.insert_one(scopist)

    # --- jobs (3 assigned to scopist + a couple unassigned)
    today = date.today()
    jobs = []
    for i, (witness, days_offset, status, scopist_status) in enumerate([
        ("Robert Hartwell", -7, "Completed", "Assigned"),
        ("Sandra Kowalski", -3, "Completed", "In Progress"),
        ("Dr. Emil Vance",  4, "Scheduled", "Assigned"),
        ("Lin Marquez",     1, "Scheduled", None),
        ("Cassie Brooks",  -14, "Invoiced", None),
        # Two completed jobs WITHOUT an invoice so the Inbox has something
        # to show on first visit.
        ("Eleanor Reyes", -5, "Completed", None),
        ("Wesley Brand",  -2, "Completed", None),
    ]):
        d = today + timedelta(days=days_offset)
        jobs.append({
            "id": str(uuid.uuid4()), "user_id": uid,
            "case_caption": f"Hartwell v. Mesa Logistics" if i == 0 else f"Demo Case {i+1}",
            "case_number": f"CV-2026-0{6000+i}",
            "witness": witness,
            "job_date": d.isoformat(),
            "start_time": "09:30",
            "location": "Snell & Wilmer, Phoenix" if i % 2 == 0 else "Zoom",
            "job_type": "Deposition",
            "client_id": client_a["id"] if i % 2 == 0 else client_b["id"],
            "ordering_attorney_text": "T. Lopez" if i % 2 == 0 else "M. Tran",
            "status": status,
            "notes": "",
            "scopist_id": scopist["id"] if scopist_status else None,
            "scopist_status": scopist_status,
            "scoping_completed_at": None,
            "invoice_id": None,
            "created_at": now_iso(),
        })
    await db.jobs.insert_many(jobs)

    # --- two invoices (one Sent + a Sent with a public share link already issued)
    inv_lines_a = [
        {"type": "appearance_fee",     "label": "Appearance fee", "detail": "Half day", "quantity": None, "rate": None, "amount": 250.0},
        {"type": "original_transcript","label": "Original transcript", "detail": "112 pages", "quantity": 112, "rate": 4.50, "amount": 504.0},
        {"type": "copy",               "label": "Copy", "detail": "112 pages", "quantity": 112, "rate": 1.25, "amount": 140.0},
    ]
    inv_lines_b = [
        {"type": "appearance_fee",     "label": "Appearance fee", "detail": None, "quantity": None, "rate": None, "amount": 225.0},
        {"type": "original_transcript","label": "Original transcript", "detail": "84 pages", "quantity": 84, "rate": 4.25, "amount": 357.0},
        {"type": "rough_draft",        "label": "Rough draft", "detail": None, "quantity": None, "rate": None, "amount": 75.0},
    ]

    async def insert_invoice(client, lines, days_back, share_token=None, status="Sent"):
        idate = today - timedelta(days=days_back)
        ddate = idate + timedelta(days=30)
        num = await next_invoice_number(uid)
        billed_to_name = client["name"] + (f" (Attn: {client['contact_name']})" if client.get("contact_name") else "")
        doc = {
            "id": str(uuid.uuid4()), "user_id": uid,
            "invoice_number": num,
            "job_id": None,
            "client_id": client["id"],
            "invoice_date": idate.isoformat(),
            "due_date": ddate.isoformat(),
            "line_items": lines,
            "notes": None,
            "payment_instructions": user.get("payment_instructions_default")
                or "Please remit payment within 30 days.",
            "billed_to_name": billed_to_name,
            "billed_to_email": client.get("contact_email"),
            "billed_to_address": client.get("billing_address"),
            "status": status,
            "total": calc_invoice_total(lines),
            "sent_at": now_iso() if status in ("Sent", "Paid") else None,
            "paid_at": None,
            "voided_at": None,
            "created_at": now_iso(),
        }
        if share_token:
            doc["share_token"] = share_token
        await db.invoices.insert_one(doc)
        return doc

    portal_token = secrets.token_urlsafe(24)
    inv1 = await insert_invoice(client_a, inv_lines_a, days_back=10, share_token=portal_token)
    inv2 = await insert_invoice(client_b, inv_lines_b, days_back=22)
    # One Draft with a billing email so the Inbox "Drafts ready to send" lights up
    inv3 = await insert_invoice(client_a, inv_lines_a, days_back=0, status="Draft")

    # --- 1 template
    tmpl = {
        "id": str(uuid.uuid4()), "user_id": uid,
        "name": "Snell & Wilmer — half-day depo",
        "client_id": client_a["id"],
        "line_items": inv_lines_a,
        "notes": None,
        "payment_instructions": user.get("payment_instructions_default"),
        "created_at": now_iso(),
    }
    await db.invoice_templates.insert_one(tmpl)

    # --- 1 recurring schedule (next run = tomorrow so the tester can Run-Now)
    rec = {
        "id": str(uuid.uuid4()), "user_id": uid,
        "name": "Esquire — monthly retainer",
        "client_id": client_b["id"],
        "frequency": "monthly",
        "day_of_month": 1,
        "day_of_week": 1,
        "next_run_date": (today + timedelta(days=1)).isoformat(),
        "line_items": [
            {"type": "custom", "label": "Monthly retainer", "detail": None, "quantity": None, "rate": None, "amount": 1500.0},
        ],
        "notes": "Auto-billed at start of each month.",
        "payment_instructions": user.get("payment_instructions_default"),
        "active": True,
        "created_at": now_iso(),
        "last_run_at": None,
        "last_invoice_id": None,
        "runs_count": 0,
    }
    await db.recurring_invoices.insert_one(rec)

    # --- print quick test URLs
    frontend = os.environ.get("FRONTEND_URL", "").rstrip("/")
    print()
    print("=" * 70)
    print("DEMO ACCOUNT READY")
    print("=" * 70)
    print(f"Login:    {frontend}/login")
    print(f"  email:    {EMAIL}")
    print(f"  password: {PASSWORD}")
    print()
    print("Try these directly (no login needed — magic links):")
    print(f"  Client portal:  {frontend}/portal/invoice/{portal_token}")
    print(f"     → invoice {inv1['invoice_number']} for {client_a['name']}")
    print(f"  Scopist portal: {frontend}/portal/scopist/{scopist['share_token']}")
    print(f"     → 3 jobs assigned to {scopist['first_name']} {scopist['last_name']}")
    print()
    print("After login, try:")
    print(f"  /app/templates  → 1 template ('Snell & Wilmer — half-day depo')")
    print(f"  /app/recurring  → 1 schedule ('Esquire — monthly retainer'). Click Run now.")
    print(f"  /app/scopists   → Jamie Lee, with copy-link / open-link buttons")
    print(f"  /app/invoices   → 2 invoices ({inv1['invoice_number']}, {inv2['invoice_number']})")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
