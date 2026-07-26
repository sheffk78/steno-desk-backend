"""Expenses CRUD + Schedule C CSV export — mounted at /api/expenses"""
import csv
import io
import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from auth_core import get_current_user, require_active_subscription
from db import db, now_iso
from models import ExpenseIn, ExpenseOut

router = APIRouter()


@router.get("", response_model=List[ExpenseOut])
async def list_expenses(request: Request, year: Optional[int] = None):
    user = await get_current_user(request)
    query = {"user_id": user["id"]}
    if year:
        query["date"] = {"$gte": f"{year}-01-01", "$lte": f"{year}-12-31"}
    items = await db.expenses.find(query, {"_id": 0}).sort("date", -1).to_list(5000)
    return [ExpenseOut(**{k: v for k, v in e.items() if k != "user_id"}) for e in items]


@router.post("", response_model=ExpenseOut)
async def create_expense(payload: ExpenseIn, request: Request):
    user = await require_active_subscription(request)
    eid = str(uuid.uuid4())
    doc = {"id": eid, "user_id": user["id"], **payload.model_dump(), "created_at": now_iso()}
    await db.expenses.insert_one(doc)
    return ExpenseOut(**{k: v for k, v in doc.items() if k != "user_id"})


@router.put("/{expense_id}", response_model=ExpenseOut)
async def update_expense(expense_id: str, payload: ExpenseIn, request: Request):
    user = await get_current_user(request)
    res = await db.expenses.update_one(
        {"id": expense_id, "user_id": user["id"]},
        {"$set": payload.model_dump()},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Expense not found")
    e = await db.expenses.find_one({"id": expense_id, "user_id": user["id"]}, {"_id": 0})
    return ExpenseOut(**{k: v for k, v in e.items() if k != "user_id"})


@router.delete("/{expense_id}")
async def delete_expense(expense_id: str, request: Request):
    user = await get_current_user(request)
    await db.expenses.delete_one({"id": expense_id, "user_id": user["id"]})
    return {"ok": True}


@router.get("/export.csv")
async def export_expenses_csv(request: Request, year: Optional[int] = None):
    user = await get_current_user(request)
    query = {"user_id": user["id"]}
    if year:
        query["date"] = {"$gte": f"{year}-01-01", "$lte": f"{year}-12-31"}
    rows = await db.expenses.find(query, {"_id": 0}).sort("date", 1).to_list(10000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Description", "Category", "Amount", "Miles", "IRS rate", "Notes"])
    for e in rows:
        w.writerow([
            e.get("date", ""), e.get("description", ""), e.get("category", ""),
            f"{float(e.get('amount') or 0):.2f}", e.get("miles") or "",
            e.get("irs_rate") or "", e.get("notes") or "",
        ])
    buf.seek(0)
    fname = f"stenodesk-expenses-{year or 'all'}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
