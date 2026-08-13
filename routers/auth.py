"""Auth + user settings — mounted at /api/auth"""
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from auth_core import (
    clear_auth_cookies,
    get_current_user,
    hash_password,
    is_admin_email,
    make_token,
    set_auth_cookies,
    verify_password,
)
from db import db, now_iso, strip
from email_service import send_new_signup_notification, send_password_reset_email
from models import ForgotIn, LoginIn, ResetIn, SettingsIn, SignupIn

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/signup")
async def signup(payload: SignupIn, request: Request, response: Response, background: BackgroundTasks):
    email = payload.email.lower().strip()
    if await db.users.find_one({"email": email}):
        raise HTTPException(409, "An account with this email already exists. Log in instead.")
    user_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    # Beta signup link gives 60 days; everyone else gets the standard 7-day
    # trial. The flag is set by the landing/CTA via ?beta=1 → header or body.
    is_beta_signup = (
        (request.query_params.get("beta") in ("1", "true"))
        or bool(getattr(payload, "beta", False))
    )
    trial_days = 60 if is_beta_signup else 7
    trial_end = now + timedelta(days=trial_days)
    doc = {
        "id": user_id,
        "email": email,
        "password_hash": hash_password(payload.password),
        "name": payload.name,
        "business_name": None,
        "cert_number": None,
        "cert_type": None,
        "address": None,
        "address_line1": None,
        "address_line2": None,
        "city": None,
        "state": None,
        "zip": None,
        "phone": None,
        "default_net_days": 30,
        "invoice_prefix": "SD",
        "payment_instructions_default": "Please remit payment within 30 days. Make checks payable to the reporter named above.",
        "subscribed_at": None,
        "subscription_type": None,
        "trial_started_at": now.isoformat(),
        "trial_ends_at": trial_end.isoformat(),
        "trial_days_granted": trial_days,
        "signup_source": "beta" if is_beta_signup else "direct",
        "created_at": now.isoformat(),
    }
    await db.users.insert_one(doc)
    # Fire-and-forget admin notification so support knows about every signup.
    background.add_task(send_new_signup_notification, doc)
    access = make_token(user_id, email, "access")
    refresh = make_token(user_id, email, "refresh")
    set_auth_cookies(response, access, refresh)
    user_out = strip(doc)
    user_out["is_admin"] = is_admin_email(email)
    return {"user": user_out, "access_token": access}


@router.post("/login")
async def login(payload: LoginIn, response: Response):
    email = payload.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(payload.password, user.get("password_hash", "")):
        raise HTTPException(401, "Incorrect email or password.")
    access = make_token(user["id"], email, "access")
    refresh = make_token(user["id"], email, "refresh")
    set_auth_cookies(response, access, refresh)
    user_out = strip(user)
    user_out["is_admin"] = is_admin_email(email)
    return {"user": user_out, "access_token": access}


@router.post("/logout")
async def logout(response: Response):
    clear_auth_cookies(response)
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    return await get_current_user(request)


@router.post("/forgot-password")
async def forgot_password(payload: ForgotIn, background: BackgroundTasks):
    email = payload.email.lower().strip()
    user = await db.users.find_one({"email": email}, {"_id": 0})
    # Generic response regardless of existence — but actually send if user exists.
    if user:
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=int(os.environ.get("RESET_TOKEN_EXPIRE_MINUTES", "60"))
        )
        await db.password_reset_tokens.insert_one({
            "id": str(uuid.uuid4()),
            "user_id": user["id"],
            "email": email,
            "token": token,
            "used": False,
            "expires_at": expires_at,
            "created_at": now_iso(),
        })
        frontend = os.environ.get("FRONTEND_URL", "")
        link = f"{frontend.rstrip('/')}/reset-password?token={token}"
        background.add_task(send_password_reset_email, email, link, user.get("name"))
        logger.info(f"Password reset queued for {email}")
    return {"ok": True}


@router.post("/reset-password")
async def reset_password(payload: ResetIn):
    rec = await db.password_reset_tokens.find_one({"token": payload.token, "used": False})
    if not rec:
        raise HTTPException(400, "This reset link is invalid or has already been used. Please request a new one.")
    exp = rec.get("expires_at")
    if isinstance(exp, str):
        exp = datetime.fromisoformat(exp)
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        raise HTTPException(400, "This reset link has expired. Please request a new one.")
    new_hash = hash_password(payload.new_password)
    await db.users.update_one({"id": rec["user_id"]}, {"$set": {"password_hash": new_hash}})
    await db.password_reset_tokens.update_one({"id": rec["id"]}, {"$set": {"used": True}})
    return {"ok": True}


@router.put("/settings")
async def update_settings(payload: SettingsIn, request: Request):
    user = await get_current_user(request)
    update = {k: v for k, v in payload.model_dump(exclude_none=True).items()}
    if update:
        await db.users.update_one({"id": user["id"]}, {"$set": update})
    fresh = await db.users.find_one({"id": user["id"]}, {"_id": 0, "password_hash": 0})
    return strip(fresh)
