"""Private file serving — mounted at /api/files"""
import logging
from typing import Optional

import jwt
from fastapi import APIRouter, HTTPException, Query, Request, Response

from auth_core import JWT_ALGO, get_current_user, jwt_secret
from db import db
from storage_service import APP_NAME, get_object

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/{path:path}")
async def serve_file(path: str, request: Request, auth: Optional[str] = Query(None)):
    """Serve a private user file. <img> tags can't send Authorization headers,
    so we accept the cookie OR an `?auth=<jwt>` query string."""
    user = None
    try:
        user = await get_current_user(request)
    except HTTPException:
        if not auth:
            raise
        try:
            payload = jwt.decode(auth, jwt_secret(), algorithms=[JWT_ALGO])
            if payload.get("type") != "access":
                raise HTTPException(401, "Invalid token")
            user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
            if not user:
                raise HTTPException(401, "User not found")
        except jwt.PyJWTError:
            raise HTTPException(401, "Invalid token")

    # Authorization: only the owning user may read their own letterhead or
    # expense-receipt paths. Both live under <APP_NAME>/<kind>/<user_id>/...
    user_prefixes = (
        f"{APP_NAME}/letterhead/{user['id']}/",
        f"{APP_NAME}/receipt/{user['id']}/",
    )
    if not any(path.startswith(p) for p in user_prefixes):
        raise HTTPException(403, "Not yours.")
    try:
        data, ctype = get_object(path)
    except Exception as e:
        logger.warning(f"file fetch failed for {path}: {e}")
        raise HTTPException(404, "File not found")
    return Response(content=data, media_type=ctype)
