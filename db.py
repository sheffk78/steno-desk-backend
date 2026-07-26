"""Shared MongoDB handle + small helpers used across routers."""
import os
from datetime import datetime, timezone
from typing import List

from motor.motor_asyncio import AsyncIOMotorClient

mongo_url = os.environ["MONGO_URL"]
mongo_client = AsyncIOMotorClient(mongo_url)
db = mongo_client[os.environ["DB_NAME"]]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def strip(d: dict) -> dict:
    """Remove Mongo `_id` and any sensitive fields before serialising to JSON."""
    if not d:
        return d
    d = {k: v for k, v in d.items() if k != "_id"}
    d.pop("password_hash", None)
    return d


def calc_invoice_total(items: List[dict]) -> float:
    return round(sum(float(li.get("amount") or 0) for li in items), 2)


def serialize_invoice(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in ("_id", "user_id")}


async def next_invoice_number(user_id: str) -> str:
    """Atomic per-user invoice counter. Returns a 'SD-NNNN' string."""
    await db.counters.find_one_and_update(
        {"user_id": user_id, "kind": "invoice"},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=True,
    )
    rec = await db.counters.find_one({"user_id": user_id, "kind": "invoice"}, {"_id": 0})
    n = (rec or {}).get("value", 1)
    return f"SD-{n:04d}"
