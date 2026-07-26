"""Auth primitives: JWT, bcrypt, cookie helpers, FastAPI dependency."""
import os
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import HTTPException, Request, Response

from db import db

JWT_ALGO = "HS256"
ACCESS_MIN = 60 * 24      # 1 day — friendly for a trial-driven app
REFRESH_DAYS = 30


# The founder's email — always an admin, regardless of whether the
# ADMIN_EMAILS env var is set. This guarantees the owner of the SaaS
# never gets locked out of /admin if an env var is forgotten during a
# deploy. Additional admins can still be added via ADMIN_EMAILS
# (comma-separated).
FOUNDER_EMAIL = "support@stenodesk.co"


def admin_emails() -> set[str]:
    """Founder/admin emails are configured via .env (comma-separated) so nobody
    can self-promote via the database. The hardcoded founder email is always
    included so the owner can never lose admin access to their own product."""
    raw = os.environ.get("ADMIN_EMAILS", "")
    emails = {e.strip().lower() for e in raw.split(",") if e.strip()}
    emails.add(FOUNDER_EMAIL)
    return emails


def is_admin_email(email: str | None) -> bool:
    if not email:
        return False
    return email.lower().strip() in admin_emails()



def subscription_state(user: dict) -> dict:
    """Compute the canonical subscription state for a user. Returns
    {is_active, reason, expires_at, days_left} so both backend (gating)
    and frontend (banner) can render consistently.

    Active reasons (in priority order):
      - "admin"            → never expires, hardcoded founder + ADMIN_EMAILS
      - "monthly"/"annual" → Stripe-active subscription
      - "beta"             → comped-as-beta, optionally with an expiry date
      - "trial"            → still inside the trial window

    Inactive reasons:
      - "trial_expired"    → had a trial, now past the date, no subscription
      - "beta_expired"     → was comped beta but the expiry has passed
      - "no_subscription"  → never had any state (shouldn't really happen)
    """
    from datetime import date as _date

    email = (user.get("email") or "").lower().strip()
    if email and email in admin_emails():
        return {"is_active": True, "reason": "admin", "expires_at": None, "days_left": None}

    sub = user.get("subscription_type")
    if sub in ("active_monthly", "active_annual"):
        return {"is_active": True, "reason": sub.replace("active_", ""),
                "expires_at": user.get("subscription_current_period_end"),
                "days_left": None}

    if sub == "beta":
        expires = (user.get("beta_expires_at") or "")[:10]
        if not expires:
            return {"is_active": True, "reason": "beta", "expires_at": None, "days_left": None}
        try:
            d = _date.fromisoformat(expires)
            delta = (d - _date.today()).days
        except ValueError:
            return {"is_active": True, "reason": "beta", "expires_at": expires, "days_left": None}
        if delta >= 0:
            return {"is_active": True, "reason": "beta", "expires_at": expires, "days_left": delta}
        return {"is_active": False, "reason": "beta_expired", "expires_at": expires, "days_left": delta}

    # No active subscription — check trial
    trial_ends = (user.get("trial_ends_at") or "")[:10]
    if not trial_ends:
        return {"is_active": False, "reason": "no_subscription", "expires_at": None, "days_left": None}
    try:
        d = _date.fromisoformat(trial_ends)
        delta = (d - _date.today()).days
    except ValueError:
        return {"is_active": False, "reason": "trial_expired", "expires_at": trial_ends, "days_left": None}
    if delta >= 0:
        return {"is_active": True, "reason": "trial", "expires_at": trial_ends, "days_left": delta}
    return {"is_active": False, "reason": "trial_expired", "expires_at": trial_ends, "days_left": delta}



def jwt_secret() -> str:
    return os.environ["JWT_SECRET"]


def hash_password(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()


def verify_password(p: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(p.encode(), h.encode())
    except Exception:
        return False


def make_token(user_id: str, email: str, kind: str = "access") -> str:
    if kind == "access":
        exp = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_MIN)
    else:
        exp = datetime.now(timezone.utc) + timedelta(days=REFRESH_DAYS)
    return jwt.encode(
        {"sub": user_id, "email": email, "type": kind, "exp": exp},
        jwt_secret(),
        algorithm=JWT_ALGO,
    )


def set_auth_cookies(response: Response, access: str, refresh: str) -> None:
    common = dict(httponly=True, secure=True, samesite="none", path="/")
    response.set_cookie("access_token", access, max_age=ACCESS_MIN * 60, **common)
    response.set_cookie("refresh_token", refresh, max_age=REFRESH_DAYS * 86400, **common)


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")


async def get_current_user(request: Request) -> dict:
    """FastAPI dependency: pull the user from the access_token cookie or
    `Authorization: Bearer <jwt>` header. Raises 401 with friendly messages."""
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(token, jwt_secret(), algorithms=[JWT_ALGO])
        if payload.get("type") != "access":
            raise HTTPException(401, "Invalid token")
        user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
        if not user:
            raise HTTPException(401, "User not found")
        # Attach a derived `is_admin` flag — purely server-side, recomputed
        # from the env allowlist on every request so adding/removing admins
        # never requires a DB migration.
        user["is_admin"] = is_admin_email(user.get("email"))
        return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session expired. Please sign in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")


async def require_admin(request: Request) -> dict:
    """FastAPI dependency for admin-only endpoints."""
    user = await get_current_user(request)
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required.")
    return user



async def require_active_subscription(request: Request) -> dict:
    """FastAPI dependency for create/write endpoints. Read-only access is
    deliberately preserved when subscription lapses — users can still view
    their data, download PDFs, and export reports. Only WRITE actions
    (creating jobs/invoices/clients, sending invoices) are gated, which
    encourages the user to upgrade to resume the workflow that generates
    value, without holding their existing data hostage.

    Raises HTTP 402 (Payment Required) with a structured error code so
    the frontend can render the upgrade modal predictably.
    """
    user = await get_current_user(request)
    state = subscription_state(user)
    if state["is_active"]:
        return user
    raise HTTPException(
        status_code=402,
        detail={
            "code": "subscription_required",
            "message": (
                "Your trial has ended. Upgrade to keep creating jobs and "
                "sending invoices — your existing data stays exactly where it is."
                if state["reason"] == "trial_expired"
                else "Your beta access has ended. Upgrade to continue."
                if state["reason"] == "beta_expired"
                else "An active subscription is required for this action."
            ),
            "reason": state["reason"],
            "expires_at": state["expires_at"],
        },
    )
