"""Shared MongoDB handle + small helpers used across routers."""
import os
from datetime import datetime, timezone
from typing import List, Optional, Any

MONGO_URL = os.environ.get("MONGO_URL", "")
DB_NAME = os.environ.get("DB_NAME", "steno_desk")

# Lazy MongoDB connection — initialized on first use
mongo_client = None
db = None  # type: Optional[Any]


async def init_db():
    """Initialize the MongoDB connection. Safe to call multiple times."""
    global mongo_client, db
    if db is not None:
        return
    if not MONGO_URL:
        print("ℹ MONGO_URL not set — running without database")
    return
    from motor.motor_asyncio import AsyncIOMotorClient
    try:
        mongo_client = AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=5000)
        await mongo_client.server_info()
    except Exception as e:
        print(f"⚠ MongoDB not available: {e}")
        return
    db = mongo_client[DB_NAME]
    print(f"✓ MongoDB connected to {DB_NAME}")


async def get_db():
    """Get the database handle, connecting lazily on first access."""
    if db is None:
        await init_db()
    return db


async def get_collection(name: str):
    """Get a MongoDB collection by name, connecting lazily."""
    d = await get_db()
    return d[name]


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