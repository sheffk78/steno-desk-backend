"""Smoke tests for /api/reports tax-summary endpoints."""
import uuid


def _seed_year_data(client, year=2025):
    """Create one client + one paid invoice + a couple expenses in `year`."""
    cl = client.post("/clients", json={
        "name": f"TaxClient {uuid.uuid4().hex[:6]}",
        "contact_email": f"tax-{uuid.uuid4().hex[:6]}@example.com",
    }).json()
    inv = client.post("/invoices", json={
        "client_id": cl["id"],
        "invoice_date": f"{year}-03-15",
        "due_date": f"{year}-04-14",
        "line_items": [{"type": "appearance_fee", "label": "Appearance fee", "amount": 500.0}],
    }).json()
    # Mark paid in same year
    client.post(f"/invoices/{inv['id']}/mark-paid", json={
        "amount": 500.0, "payment_date": f"{year}-04-10",
        "payment_method": "check", "reference": "1234",
    })
    # Two expenses
    client.post("/expenses", json={
        "date": f"{year}-02-20", "amount": 35.0, "description": "Phoenix → Tucson",
        "category": "Mileage", "miles": 50.0, "irs_rate": 0.70,
    })
    client.post("/expenses", json={
        "date": f"{year}-05-05", "amount": 120.0, "description": "Scopist (Jamie)",
        "category": "Scopist",
    })
    return cl, inv


def test_reports_tax_summary(client):
    _, _ = _seed_year_data(client, year=2025)
    r = client.get("/reports/tax-summary", params={"year": 2025})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["year"] == 2025
    assert s["invoiced_total"] >= 500.0
    assert s["collected_total"] >= 500.0
    assert s["expenses_total"] >= 155.0  # 35 + 120
    # Mileage row populated with miles + Schedule C line "9"
    mile = next(c for c in s["expense_categories"] if c["category"] == "Mileage")
    assert mile["schedule_c_line"] == "9"
    assert mile["amount"] >= 35.0
    assert s["mileage_total_miles"] >= 50.0
    # Net profit calc
    assert s["net_profit_estimate"] == round(s["collected_total"] - s["expenses_total"], 2)
    # Top clients populated
    assert len(s["top_clients"]) >= 1


def test_reports_tax_summary_empty_year(client):
    r = client.get("/reports/tax-summary", params={"year": 2099})
    assert r.status_code == 200
    s = r.json()
    assert s["invoiced_total"] == 0
    assert s["collected_total"] == 0
    assert s["expenses_total"] == 0
    assert s["net_profit_estimate"] == 0


def test_reports_csv_export(client):
    _seed_year_data(client, year=2026)
    r = client.get("/reports/tax-summary.csv", params={"year": 2026})
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    body = r.text
    assert "Tax Summary" in body
    assert "Schedule C" in body
    assert "Mileage" in body
    assert "Scopist" in body
    assert "NET PROFIT ESTIMATE" in body


def test_reports_years_includes_current_year(client):
    r = client.get("/reports/years")
    assert r.status_code == 200
    years = r.json()
    from datetime import date
    assert date.today().year in years
    # Descending
    assert years == sorted(years, reverse=True)
