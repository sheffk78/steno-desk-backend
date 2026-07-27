"""Shared MongoDB handle + small helpers used across routers."""
import os
from datetime import datetime, timezone
from typing import List

MONGO_URL = os.environ.get("MONGO_URL", "")
DB_NAME = os.environ.get("DB_NAME", "steno_desk")

# Lazy MongoDB connection — only connects when first accessed
_mongo_client = None
_db = None


async def get_db():
    """Get the database handle, connecting on first access."""
    global _mongo_client, _db
    if _db is not None:
        return _db
    if not MONGO_URL:
        raise RuntimeError("MONGO_URL environment variable is not set")
    from motor.motor_asyncio import AsyncIOMotorClient
    _mongo_client = AsyncIOMotorClient(MONGO_URL)
    _db = _mongo_client[DB_NAME]
    return _db


async def get_collection(name: str):
    """Get a MongoDB collection by name, connecting lazily."""
    db = await get_db()
    return db[name]


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
    coll = await get_collection("counters")
    await coll.find_one_and_update(
        {"user_id": user_id, "kind": "invoice"},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=True,
    )
    rec = await coll.find_one({"user_id": user_id, "kind": "invoice"}, {"_id": 0})
    n = (rec or {}).get("value", 1)
    return f"SD-{n:04d}"