"""
Data import script — runs on startup to seed MongoDB with data from the initial data dump.
Checks if data already exists before importing to avoid duplicates.
"""
import json
import os
import asyncio
from pathlib import Path

DUMP_DIR = Path(__file__).parent / "data_dump"

# Never auto-seed users: the dump carries a historical password hash nobody
# holds anymore, and a seeded support@ row SHADOWS the real signup (dup-409),
# permanently locking the owner account on any fresh environment. Users are
# created by real signups; ADMIN_EMAILS grants the flag at read time.
SKIP_COLLECTIONS = {"users", "payments", "leads", "invoice_templates", "counters"}

async def import_data(db):
    """Import data from JSON dump files into MongoDB."""
    if db is None:
        print("⚠ Cannot import data — MongoDB not connected")
        return

    for dump_file in DUMP_DIR.glob("*.json"):
        collection_name = dump_file.stem
        if collection_name in SKIP_COLLECTIONS:
            print(f"  ⏭ {collection_name}: skip (security/seed guard)")
            continue
        existing = await db[collection_name].count_documents({})
        if existing > 0:
            print(f"  ⏭ {collection_name}: already has {existing} records, skipping")
            continue
        
        with open(dump_file) as f:
            records = json.load(f)
        
        if records:
            # Convert _id strings to ObjectId where needed
            for r in records:
                if '_id' in r and isinstance(r['_id'], dict) and '$oid' in r['_id']:
                    from bson import ObjectId
                    r['_id'] = ObjectId(r['_id']['$oid'])
            
            result = await db[collection_name].insert_many(records)
            print(f"  ✅ {collection_name}: imported {len(result.inserted_ids)} records")
        else:
            print(f"  ⏭ {collection_name}: empty dump file")
