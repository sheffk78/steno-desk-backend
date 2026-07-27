"""Admin endpoints — mounted at /api/admin. Gated by the ADMIN_EMAILS env list.

The model is intentionally small: list users + extend a trial + comp a beta
tester. Nothing here mutates a user's password or email — those are still
self-serve flows the user owns.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import EmailStr

from auth_core import require_admin
from db import get_db, now_iso
from models import StrictModel

logger = logging.getLogger(__name__)
router = APIRouter()


class ExtendTrialIn(StrictModel):
    """Either add N days OR set an absolute end date. Days takes precedence."""
    days: Optional[int] = None       # +N days from current trial_ends_at (or today if expired)
    until: Optional[str] = None      # absolute ISO date


class CompBetaIn(StrictModel):
    """Convert a user to a free "beta" subscription. No expiry = unlimited."""
    expires_at: Optional[str] = None  # ISO date or None for indefinite


def _strip(u: dict) -> dict:
    return {k: v for k, v in u.items() if k not in ("_id", "password_hash")}


async def _attach_counts(users: list[dict]) -> list[dict]:
    """Decorate each user with their job/invoice/client counts for the list view.
    Uses a single grouped aggregation per collection so we don't N+1 the DB."""
    if not users:
        return users
    ids = [u["id"] for u in users]
    for coll, key in (("jobs", "jobs_count"), ("invoices", "invoices_count"), ("clients", "clients_count")):
        pipe = [
            {"$match": {"user_id": {"$in": ids}}},
            {"$group": {"_id": "$user_id", "n": {"$sum": 1}}},
        ]
        m: dict = {}
        async for row in db[coll].aggregate(pipe):
            m[row["_id"]] = row["n"]
        for u in users:
            u[key] = int(m.get(u["id"], 0))
    return users


def _status(u: dict) -> str:
    """Derived status label — what the admin sees in the table."""
    if u.get("subscription_type") == "beta":
        return "Beta (comped)"
    if u.get("subscription_type") in ("monthly", "annual"):
        return "Subscribed"
    trial_ends = (u.get("trial_ends_at") or "")[:10]
    today = date.today().isoformat()
    if not trial_ends:
        return "Active"
    return "Trialing" if trial_ends >= today else "Trial expired"


@router.get("/users")
async def list_users(request: Request, q: Optional[str] = None, status: Optional[str] = None):
    """Admin user directory. `q` filters by email substring (case-insensitive).
    `status` filters by derived status: trialing | subscribed | beta | expired."""
    await require_admin(request)
    find: dict = {}
    if q:
        find["email"] = {"$regex": q.strip(), "$options": "i"}
    users = await db.users.find(find, {"password_hash": 0, "_id": 0}).sort("created_at", -1).to_list(2000)
    users = await _attach_counts(users)
    rows = []
    for u in users:
        rows.append({
            "id": u["id"],
            "email": u["email"],
            "name": u.get("name"),
            "business_name": u.get("business_name"),
            "created_at": u.get("created_at"),
            "trial_started_at": u.get("trial_started_at"),
            "trial_ends_at": u.get("trial_ends_at"),
            "subscription_type": u.get("subscription_type"),
            "subscription_expires_at": u.get("subscription_expires_at"),
            "subscribed_at": u.get("subscribed_at"),
            "signup_source": u.get("signup_source") or "direct",
            "trial_days_granted": u.get("trial_days_granted"),
            "jobs_count": u.get("jobs_count", 0),
            "invoices_count": u.get("invoices_count", 0),
            "clients_count": u.get("clients_count", 0),
            "status": _status(u),
        })
    if status:
        wanted = status.lower()
        rows = [r for r in rows if wanted in r["status"].lower()]
    return rows


@router.get("/stats")
async def admin_stats(request: Request):
    """Top-of-page rollup: signups, trial counts, expiring-soon count."""
    await require_admin(request)
    users = await db.users.find({}, {"password_hash": 0, "_id": 0}).to_list(5000)
    today = date.today()
    soon = (today + timedelta(days=3)).isoformat()
    trialing = 0
    expiring_soon = 0
    subscribed = 0
    beta = 0
    for u in users:
        st = _status(u)
        if st == "Trialing":
            trialing += 1
            if (u.get("trial_ends_at") or "")[:10] <= soon:
                expiring_soon += 1
        elif st == "Subscribed":
            subscribed += 1
        elif st == "Beta (comped)":
            beta += 1
    # Signups this week
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    signups_week = sum(1 for u in users if (u.get("created_at") or "") >= week_ago)
    return {
        "total_users": len(users),
        "trialing": trialing,
        "subscribed": subscribed,
        "beta": beta,
        "expiring_within_3_days": expiring_soon,
        "signups_last_7_days": signups_week,
    }


@router.get("/users/{user_id}")
async def get_user_detail(user_id: str, request: Request):
    await require_admin(request)
    u = await db.users.find_one({"id": user_id}, {"_id": 0, "password_hash": 0})
    if not u:
        raise HTTPException(404, "User not found")
    [u] = await _attach_counts([u])
    return {**_strip(u), "status": _status(u)}


@router.post("/users/{user_id}/extend-trial")
async def extend_trial(user_id: str, payload: ExtendTrialIn, request: Request):
    """Push the user's trial_ends_at forward — either by `days` or to an
    absolute `until` date. Returns the updated user record."""
    admin = await require_admin(request)
    u = await db.users.find_one({"id": user_id}, {"_id": 0})
    if not u:
        raise HTTPException(404, "User not found")

    if payload.days is None and not payload.until:
        raise HTTPException(400, "Pass `days` or `until` (ISO date).")

    today = date.today()
    if payload.days is not None:
        # Start from whichever is later: current trial_ends_at OR today
        cur_iso = (u.get("trial_ends_at") or "")[:10]
        try:
            base = date.fromisoformat(cur_iso) if cur_iso else today
        except ValueError:
            base = today
        if base < today:
            base = today
        new_end = base + timedelta(days=int(payload.days))
    else:
        try:
            new_end = date.fromisoformat(payload.until)  # type: ignore[arg-type]
        except ValueError:
            raise HTTPException(400, "`until` must be an ISO date (YYYY-MM-DD).")

    new_iso = datetime.combine(new_end, datetime.min.time()).replace(tzinfo=timezone.utc).isoformat()
    await db.users.update_one(
        {"id": user_id},
        {"$set": {"trial_ends_at": new_iso},
         "$push": {"admin_actions": {
             "kind": "extend_trial",
             "by": admin["email"],
             "at": now_iso(),
             "new_trial_ends_at": new_iso,
             "days_added": payload.days,
             "absolute_until": payload.until,
         }}},
    )
    updated = await db.users.find_one({"id": user_id}, {"_id": 0, "password_hash": 0})
    [updated] = await _attach_counts([updated])
    return {**_strip(updated), "status": _status(updated)}


@router.post("/users/{user_id}/comp-beta")
async def comp_beta(user_id: str, payload: CompBetaIn, request: Request):
    """Comp a user to a free beta subscription. They no longer see the
    trial-ending banner and the app treats them as paid."""
    admin = await require_admin(request)
    u = await db.users.find_one({"id": user_id}, {"_id": 0})
    if not u:
        raise HTTPException(404, "User not found")
    expires_iso = None
    if payload.expires_at:
        try:
            expires_iso = (date.fromisoformat(payload.expires_at)).isoformat()
        except ValueError:
            raise HTTPException(400, "`expires_at` must be an ISO date (YYYY-MM-DD).")
    await db.users.update_one(
        {"id": user_id},
        {"$set": {
            "subscription_type": "beta",
            "subscription_expires_at": expires_iso,
            "subscribed_at": now_iso(),
         },
         "$push": {"admin_actions": {
             "kind": "comp_beta",
             "by": admin["email"],
             "at": now_iso(),
             "expires_at": expires_iso,
         }}},
    )
    updated = await db.users.find_one({"id": user_id}, {"_id": 0, "password_hash": 0})
    [updated] = await _attach_counts([updated])
    return {**_strip(updated), "status": _status(updated)}


@router.post("/users/{user_id}/revoke-comp")
async def revoke_comp(user_id: str, request: Request):
    """Undo a comp — reverts the user back to a 0-day trial state (expired).
    They can then be re-comped or extended."""
    admin = await require_admin(request)
    u = await db.users.find_one({"id": user_id}, {"_id": 0})
    if not u:
        raise HTTPException(404, "User not found")
    await db.users.update_one(
        {"id": user_id},
        {"$set": {
            "subscription_type": None,
            "subscription_expires_at": None,
            "subscribed_at": None,
         },
         "$push": {"admin_actions": {
             "kind": "revoke_comp",
             "by": admin["email"],
             "at": now_iso(),
         }}},
    )
    updated = await db.users.find_one({"id": user_id}, {"_id": 0, "password_hash": 0})
    [updated] = await _attach_counts([updated])
    return {**_strip(updated), "status": _status(updated)}



class BulkDeleteIn(StrictModel):
    """Multi-select delete from the admin user list."""
    user_ids: List[str]


@router.post("/users/bulk-delete")
async def bulk_delete_users(payload: BulkDeleteIn, request: Request):
    """Hard-delete a batch of users + all their owned data (jobs, invoices,
    clients, attorneys, scopists, templates, recurring schedules, expenses,
    leads created from their app).

    Refuses to delete:
      - the current admin (you can't delete yourself)
      - any other user whose email is in the admin allowlist

    Returns counts of what was removed for transparency.
    """
    from auth_core import is_admin_email

    admin = await require_admin(request)
    raw_ids = list({uid for uid in payload.user_ids if isinstance(uid, str) and uid})
    if not raw_ids:
        raise HTTPException(400, "No user IDs supplied.")

    # Resolve to docs so we can filter out admins and the caller themselves.
    candidates = await db.users.find(
        {"id": {"$in": raw_ids}}, {"_id": 0, "id": 1, "email": 1}
    ).to_list(length=len(raw_ids))

    deletable: list[str] = []
    skipped: list[dict] = []
    found_ids = {c["id"] for c in candidates}
    missing = [uid for uid in raw_ids if uid not in found_ids]

    for c in candidates:
        if c["id"] == admin["id"]:
            skipped.append({"id": c["id"], "email": c["email"], "reason": "self"})
            continue
        if is_admin_email(c["email"]):
            skipped.append({"id": c["id"], "email": c["email"], "reason": "admin"})
            continue
        deletable.append(c["id"])

    if not deletable:
        return {
            "ok": True,
            "deleted_users": 0,
            "skipped": skipped,
            "missing": missing,
            "owned_data_removed": {},
        }

    # Wipe owned data across every per-user collection. user_id is the
    # canonical owner key on each — see /backend/db.py indexes.
    owned_counts: dict = {}
    for coll in ("jobs", "invoices", "clients", "attorneys", "scopists",
                 "templates", "recurring_invoices", "expenses",
                 "password_resets"):
        res = await db[coll].delete_many({"user_id": {"$in": deletable}})
        if res.deleted_count:
            owned_counts[coll] = res.deleted_count

    # Now wipe the user docs themselves.
    res = await db.users.delete_many({"id": {"$in": deletable}})
    logger.info(
        f"admin {admin['email']} bulk-deleted {res.deleted_count} users "
        f"(skipped={len(skipped)}, missing={len(missing)})"
    )

    return {
        "ok": True,
        "deleted_users": res.deleted_count,
        "deleted_user_ids": deletable,
        "skipped": skipped,
        "missing": missing,
        "owned_data_removed": owned_counts,
    }
