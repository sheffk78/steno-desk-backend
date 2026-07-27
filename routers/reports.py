"""Tax reports & annual revenue summary — mounted at /api/reports.

The whole point of this module is to compress a year of invoicing + expenses
into something Marie can email to her tax accountant in one click.

Schedule C category mapping is conservative — it surfaces the line number on
the IRS form so the accountant can cross-reference, but it does NOT claim to
substitute for actual tax advice.
"""
import csv
import io
from collections import defaultdict
from datetime import date
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from auth_core import get_current_user
from db import get_db

router = APIRouter()


# Schedule C line mapping per the IRS 2025 form (Part II — Expenses). Values
# below are the most defensible default mapping; tax accountants may reclassify.
SCHEDULE_C_MAP: dict[str, dict] = {
    "Scopist":              {"line": "11", "title": "Contract labor"},
    "Mileage":              {"line": "9",  "title": "Car and truck expenses"},
    "Software":             {"line": "18", "title": "Office expense"},
    "Continuing Education": {"line": "17", "title": "Legal and professional services"},
    "Supplies":             {"line": "22", "title": "Supplies"},
    "Equipment":            {"line": "13", "title": "Depreciation / section 179"},
    "Professional Dues":    {"line": "17", "title": "Legal and professional services"},
    "Other":                {"line": "27a", "title": "Other expenses"},
}


async def _build_summary(user_id: str, year: int) -> dict:
    """Aggregate the year's invoicing + expenses into one report object."""
    yr_start = f"{year}-01-01"
    yr_end = f"{year}-12-31"

    # ---- invoices: invoiced amount by month, paid amount by paid_at month ----
    invs = await db.invoices.find(
        {"user_id": user_id, "status": {"$ne": "Void"}},
        {"_id": 0},
    ).to_list(20000)

    invoiced_total = 0.0
    collected_total = 0.0
    outstanding_total = 0.0
    by_month = [{"month": m, "invoiced": 0.0, "collected": 0.0} for m in range(1, 13)]
    by_client: dict[str, dict] = defaultdict(lambda: {"invoiced": 0.0, "collected": 0.0})

    invoice_to_client: dict[str, str] = {}
    for inv in invs:
        if inv.get("id"):
            invoice_to_client[inv["id"]] = inv.get("client_id") or "_unknown"

        idate = inv.get("invoice_date") or ""
        if yr_start <= idate <= yr_end:
            amount = float(inv.get("total") or 0)
            invoiced_total += amount
            try:
                m = int(idate[5:7])
                by_month[m - 1]["invoiced"] += amount
            except (ValueError, IndexError):
                pass
            cid = inv.get("client_id") or "_unknown"
            by_client[cid]["invoiced"] += amount

        if inv.get("status") in ("Draft", "Sent"):
            if yr_start <= idate <= yr_end:
                outstanding_total += float(inv.get("total") or 0)

    # Collected (cash basis) = actual payments dated within the year. This is
    # more accurate than invoice.paid_at, which is set to now() when the
    # reporter clicks "Mark paid" (independent of payment_date).
    async for p in db.payments.find(
        {"user_id": user_id, "payment_date": {"$gte": yr_start, "$lte": yr_end}},
        {"_id": 0},
    ):
        amount = float(p.get("amount") or 0)
        collected_total += amount
        try:
            m = int((p.get("payment_date") or "")[5:7])
            by_month[m - 1]["collected"] += amount
        except (ValueError, IndexError):
            pass
        cid = invoice_to_client.get(p.get("invoice_id") or "", "_unknown")
        by_client[cid]["collected"] += amount

    # Resolve client names (include soft-deleted with [Deleted] suffix)
    if by_client:
        async for c in db.clients.find(
            {"user_id": user_id, "id": {"$in": list(by_client.keys())}},
            {"_id": 0, "id": 1, "name": 1, "is_deleted": 1},
        ):
            entry = by_client[c["id"]]
            entry["name"] = (f"{c['name']} [Deleted]" if c.get("is_deleted") else c["name"])
    for cid, entry in by_client.items():
        entry.setdefault("name", "Unknown client" if cid == "_unknown" else cid)

    top_clients = sorted(
        [
            {"name": e["name"], "invoiced": round(e["invoiced"], 2), "collected": round(e["collected"], 2)}
            for e in by_client.values() if e["invoiced"] > 0
        ],
        key=lambda r: r["invoiced"],
        reverse=True,
    )[:10]

    # ---- expenses: by category ----
    exps = await db.expenses.find(
        {"user_id": user_id, "date": {"$gte": yr_start, "$lte": yr_end}},
        {"_id": 0},
    ).to_list(20000)

    expenses_total = 0.0
    by_category: dict[str, float] = defaultdict(float)
    mileage_total_miles = 0.0
    for e in exps:
        amt = float(e.get("amount") or 0)
        cat = e.get("category") or "Other"
        expenses_total += amt
        by_category[cat] += amt
        if cat == "Mileage":
            mileage_total_miles += float(e.get("miles") or 0)

    categories = [
        {
            "category": cat,
            "amount": round(by_category.get(cat, 0.0), 2),
            "schedule_c_line": SCHEDULE_C_MAP[cat]["line"],
            "schedule_c_title": SCHEDULE_C_MAP[cat]["title"],
        }
        for cat in SCHEDULE_C_MAP.keys()
    ]

    return {
        "year": year,
        "invoiced_total": round(invoiced_total, 2),
        "collected_total": round(collected_total, 2),
        "outstanding_total": round(outstanding_total, 2),
        "expenses_total": round(expenses_total, 2),
        "net_profit_estimate": round(collected_total - expenses_total, 2),
        "by_month": [
            {"month": m["month"],
             "invoiced": round(m["invoiced"], 2),
             "collected": round(m["collected"], 2)}
            for m in by_month
        ],
        "top_clients": top_clients,
        "expense_categories": categories,
        "mileage_total_miles": round(mileage_total_miles, 1),
    }


@router.get("/tax-summary")
async def tax_summary(request: Request, year: Optional[int] = Query(None)):
    user = await get_current_user(request)
    yr = int(year or date.today().year)
    return await _build_summary(user["id"], yr)


@router.get("/tax-summary.csv")
async def tax_summary_csv(request: Request, year: Optional[int] = Query(None)):
    """CSV export laid out for handoff to a tax accountant."""
    user = await get_current_user(request)
    yr = int(year or date.today().year)
    s = await _build_summary(user["id"], yr)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"Steno Desk — {yr} Tax Summary"])
    w.writerow([])
    w.writerow(["REVENUE"])
    w.writerow(["Total invoiced", f"{s['invoiced_total']:.2f}"])
    w.writerow(["Total collected (cash basis)", f"{s['collected_total']:.2f}"])
    w.writerow(["Outstanding at year end", f"{s['outstanding_total']:.2f}"])
    w.writerow([])
    w.writerow(["EXPENSES (Schedule C, Part II)"])
    w.writerow(["Schedule C line", "Category", "Title", "Amount"])
    for c in s["expense_categories"]:
        w.writerow([c["schedule_c_line"], c["category"], c["schedule_c_title"], f"{c['amount']:.2f}"])
    w.writerow([])
    w.writerow(["Total expenses", f"{s['expenses_total']:.2f}"])
    w.writerow(["Mileage — total miles driven", f"{s['mileage_total_miles']:.1f}"])
    w.writerow([])
    w.writerow(["NET PROFIT ESTIMATE (collected − expenses)", f"{s['net_profit_estimate']:.2f}"])
    w.writerow([])
    w.writerow(["MONTHLY BREAKDOWN"])
    w.writerow(["Month", "Invoiced", "Collected"])
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for m in s["by_month"]:
        w.writerow([months[m["month"] - 1], f"{m['invoiced']:.2f}", f"{m['collected']:.2f}"])
    w.writerow([])
    w.writerow(["TOP CLIENTS BY INVOICED"])
    w.writerow(["Client", "Invoiced", "Collected"])
    for c in s["top_clients"]:
        w.writerow([c["name"], f"{c['invoiced']:.2f}", f"{c['collected']:.2f}"])

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="StenoDesk-{yr}-TaxSummary.csv"'},
    )


@router.get("/years")
async def reports_years(request: Request):
    """Distinct years with revenue or expense activity, descending. Useful for the
    year selector dropdown."""
    user = await get_current_user(request)
    years: set[int] = set()
    async for i in db.invoices.find(
        {"user_id": user["id"], "invoice_date": {"$ne": None}},
        {"_id": 0, "invoice_date": 1},
    ):
        try:
            years.add(int((i.get("invoice_date") or "")[:4]))
        except (ValueError, TypeError):
            pass
    async for e in db.expenses.find(
        {"user_id": user["id"]}, {"_id": 0, "date": 1}
    ):
        try:
            years.add(int((e.get("date") or "")[:4]))
        except (ValueError, TypeError):
            pass
    this_year = date.today().year
    years.add(this_year)
    return sorted(years, reverse=True)
