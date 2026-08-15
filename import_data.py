"""
Data import script — runs on startup to seed MongoDB with data from the initial data dump.
Checks if data already exists before importing to avoid duplicates.
"""
import json
import os
import asyncio
from pathlib import Path

DUMP_DIR = Path(__file__).parent / "data_dump"

async def import_data(db):
    """Import data from JSON dump files into MongoDB."""
    if db is None:
        print("⚠ Cannot import data — MongoDB not connected")
        return
    
    for dump_file in DUMP_DIR.glob("*.json"):
        collection_name = dump_file.stem
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
