"""Steno Desk — FastAPI app entrypoint.

Loads environment, wires routers, manages startup indexes + storage, and
configures CORS. Domain logic lives in `/app/backend/routers/*`.
"""
from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import logging
import os

from fastapi import APIRouter, FastAPI
from starlette.middleware.cors import CORSMiddleware

from db import get_db, init_db, db
from storage_service import init_storage

# Routers
from routers.admin import router as admin_router
from routers.attorneys import router as attorneys_router
from routers.auth import router as auth_router
from routers.billing import router as billing_router
from routers.clients import router as clients_router
from routers.dashboard import router as dashboard_router
from routers.expenses import router as expenses_router
from routers.files import router as files_router
from routers.invoices import router as invoices_router
from routers.jobs import router as jobs_router
from routers.leads import router as leads_router
from routers.portal import router as portal_router
from routers.recurring import router as recurring_router
from routers.recurring import run_due_recurrings
from routers.reminders import router as reminders_router
from routers.reminders import send_overdue_reminders
from routers.reports import router as reports_router
from routers.scopists import router as scopists_router
from routers.stripe_webhooks import router as stripe_webhooks_router
from routers.templates import router as templates_router
from routers.uploads import router as uploads_router
from routers.webhooks import router as webhooks_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
logger = logging.getLogger("stenodesk")

app = FastAPI(title="Steno Desk API")
api = APIRouter(prefix="/api")


@api.get("/")
async def root():
    return {"app": "Steno Desk", "ok": True}


@api.get("/health")
async def health():
    """Health check endpoint for Railway auto-deploy."""
    return {"status": "healthy", "app": "Steno Desk"}


# Mount each domain router with its own URL prefix — the prefix lives in one
# place per resource, paths inside each router stay short and readable.
api.include_router(auth_router,      prefix="/auth",      tags=["auth"])
api.include_router(admin_router,     prefix="/admin",     tags=["admin"])
api.include_router(clients_router,   prefix="/clients",   tags=["clients"])
api.include_router(attorneys_router, prefix="/attorneys", tags=["attorneys"])
api.include_router(scopists_router,  prefix="/scopists",  tags=["scopists"])
api.include_router(jobs_router,      prefix="/jobs",      tags=["jobs"])
api.include_router(invoices_router,  prefix="/invoices",  tags=["invoices"])
api.include_router(templates_router, prefix="/templates", tags=["templates"])
api.include_router(recurring_router, prefix="/recurring", tags=["recurring"])
api.include_router(expenses_router,  prefix="/expenses",  tags=["expenses"])
api.include_router(reports_router,   prefix="/reports",   tags=["reports"])
api.include_router(dashboard_router, prefix="/dashboard", tags=["dashboard"])
api.include_router(uploads_router,   prefix="/uploads",   tags=["uploads"])
api.include_router(files_router,     prefix="/files",     tags=["files"])
api.include_router(leads_router,     prefix="/leads",     tags=["leads"])
api.include_router(portal_router,    prefix="/portal",    tags=["portal"])
api.include_router(billing_router,   prefix="/billing",   tags=["billing"])
api.include_router(reminders_router, prefix="/reminders", tags=["reminders"])
api.include_router(webhooks_router,  prefix="/webhooks",  tags=["webhooks"])
api.include_router(stripe_webhooks_router, prefix="/webhooks", tags=["webhooks"])
app.include_router(api)


@app.on_event("startup")
async def _startup():
    await db.users.create_index("email", unique=True)
    await db.password_reset_tokens.create_index("expires_at", expireAfterSeconds=0)
    await db.clients.create_index([("user_id", 1), ("name", 1)])
    await db.jobs.create_index([("user_id", 1), ("job_date", -1)])
    await db.invoices.create_index([("user_id", 1), ("created_at", -1)])
    await db.expenses.create_index([("user_id", 1), ("date", -1)])
    await db.payments.create_index([("user_id", 1), ("invoice_id", 1)])
    await db.invoice_templates.create_index([("user_id", 1), ("created_at", -1)])
    await db.scopists.create_index([("user_id", 1), ("created_at", -1)])
    await db.scopists.create_index("share_token", unique=True, sparse=True)
    await db.invoices.create_index("share_token", unique=True, sparse=True)
    await db.invoices.create_index("message_id", sparse=True)
    await db.recurring_invoices.create_index([("user_id", 1), ("next_run_date", 1)])
    await db.users.create_index("stripe_customer_id", sparse=True)

    # Dedup leads (keep the earliest record per email) before applying the
    # unique index. Self-healing: idempotent on every boot.
    try:
        dupes_cursor = db.leads.aggregate([
            {"$group": {
                "_id": "$email",
                "ids": {"$push": "$id"},
                "earliest": {"$min": "$created_at"},
                "count": {"$sum": 1},
            }},
            {"$match": {"count": {"$gt": 1}}},
        ])
        removed = 0
        async for dup in dupes_cursor:
            keeper = await db.leads.find_one(
                {"email": dup["_id"], "created_at": dup["earliest"]},
                {"_id": 0, "id": 1},
            )
            keeper_id = (keeper or {}).get("id")
            res = await db.leads.delete_many(
                {"email": dup["_id"], "id": {"$ne": keeper_id}}
            )
            removed += res.deleted_count
        if removed:
            logger.info(f"leads dedup: removed {removed} duplicate row(s)")
    except Exception as e:
        logger.warning(f"leads dedup skipped: {e}")

    try:
        await db.leads.create_index("email", unique=True)
    except Exception as e:
        logger.warning(f"leads.email unique index not created: {e}")
    try:
        init_storage()
    except Exception as e:
        logger.error(f"Object storage init failed (uploads will retry on demand): {e}")
    logger.info("Steno Desk indexes ready")

    # Background scheduler — ticks every 60 minutes. Generates due
    # recurring invoices, then sends overdue-invoice reminders. Failures
    # are caught so a bad iteration never kills the loop.
    import asyncio

    async def _scheduler():
        # Stagger first run so it doesn't race with startup work.
        await asyncio.sleep(120)
        while True:
            try:
                await run_due_recurrings()
            except Exception as e:
                logger.warning(f"scheduler: run_due_recurrings failed: {e}")
            try:
                summary = await send_overdue_reminders()
                if summary.get("sent") or summary.get("failed"):
                    logger.info(f"scheduler: reminders {summary}")
            except Exception as e:
                logger.warning(f"scheduler: send_overdue_reminders failed: {e}")
            await asyncio.sleep(60 * 60)

    if os.environ.get("DISABLE_SCHEDULER") != "1":
        asyncio.create_task(_scheduler())
        logger.info("Steno Desk scheduler started (60-min tick)")


@app.on_event("shutdown")
async def _shutdown():
    mongo_client.close()


# CORS — allow credentials with origin echoing
_origins = os.environ.get("CORS_ORIGINS", "*")
if _origins == "*":
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=".*",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins.split(","),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
