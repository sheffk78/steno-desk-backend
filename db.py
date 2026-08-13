"""Shared MongoDB handle + small helpers used across routers.

Eager connection: routers import `db` at module load and expect a live
handle (e.g. `from db import db`). The lazy `db = None` refactor broke every
router that captured `db` by value at import time (it froze to None) and
every router that referenced bare `db` after importing only `get_db`
(NameError). We restore the eager pattern the routers were written for,
while keeping `init_db`/`get_db`/`get_collection` as backward-compatible
no-op helpers so nothing that calls them breaks.
"""
import os
from datetime import datetime, timezone
from typing import List, Optional, Any

from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URL = os.environ.get("MONGO_URL", "")
DB_NAME = os.environ.get("DB_NAME", "steno_desk")

# Eager MongoDB connection — routers expect a live handle at import time.
mongo_client = AsyncIOMotorClient(MONGO_URL) if MONGO_URL else None
db = mongo_client[DB_NAME] if mongo_client is not None else None  # type: Optional[Any]


async def init_db():
    """Backward-compatible no-op. Connection is already eager.

    Kept so `server.py` startup and any `await init_db()` callers still work.
    If the eager connection was unavailable (no MONGO_URL), retry once here.
    """
    global mongo_client, db
    if db is not None:
        return
    if not MONGO_URL:
        print("ℹ MONGO_URL not set — running without database")
        return
    from motor.motor_asyncio import AsyncIOMotorClient as _Client
    import asyncio as _asyncio
    for attempt in range(3):
        try:
            mongo_client = _Client(MONGO_URL, serverSelectionTimeoutMS=10000)
            await mongo_client.server_info()
            db = mongo_client[DB_NAME]
            print(f"✓ MongoDB connected to {DB_NAME} (attempt {attempt + 1})")
            return
        except Exception as e:
            print(f"⚠ MongoDB connection attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                await _asyncio.sleep(5)
    print("⚠ MongoDB not available after 3 attempts — running without database")


async def get_db():
    """Backward-compatible: return the eager handle (connect lazily if None)."""
    if db is None:
        await init_db()
    return db


async def get_collection(name: str):
    """Backward-compatible: get a collection by name."""
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
