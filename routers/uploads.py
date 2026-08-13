"""Letterhead upload — mounted at /api/uploads"""
import logging
import uuid

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from auth_core import get_current_user
from db import db, now_iso
from storage_service import APP_NAME, put_object

logger = logging.getLogger(__name__)
router = APIRouter()

ALLOWED_LETTERHEAD_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/svg+xml"}
LETTERHEAD_MAX_BYTES = 2 * 1024 * 1024  # 2 MB — letterheads are tiny


@router.post("/letterhead")
async def upload_letterhead(request: Request, file: UploadFile = File(...)):
    user = await get_current_user(request)
    ctype = (file.content_type or "").lower()
    if ctype not in ALLOWED_LETTERHEAD_TYPES:
        raise HTTPException(400, "Please upload a PNG, JPG, or SVG image.")
    data = await file.read()
    if len(data) > LETTERHEAD_MAX_BYTES:
        raise HTTPException(413, "Please use an image smaller than 2 MB.")
    if len(data) == 0:
        raise HTTPException(400, "That file appears to be empty.")
    ext_map = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/svg+xml": "svg"}
    ext = ext_map.get(ctype, "bin")
    path = f"{APP_NAME}/letterhead/{user['id']}/{uuid.uuid4()}.{ext}"
    try:
        result = put_object(path, data, ctype)
    except Exception as e:
        logger.exception(f"Letterhead upload failed: {e}")
        raise HTTPException(502, "We couldn't save that image right now. Please try again in a moment.")

    stored_path = result.get("path", path)
    public_url = f"/api/files/{stored_path}"
    await db.users.update_one(
        {"id": user["id"]},
        {"$set": {
            "letterhead_path": stored_path,
            "letterhead_url": public_url,
            "letterhead_content_type": ctype,
            "letterhead_size": result.get("size", len(data)),
            "letterhead_uploaded_at": now_iso(),
        }},
    )
    return {"ok": True, "url": public_url, "content_type": ctype, "size": result.get("size", len(data))}


@router.delete("/letterhead")
async def delete_letterhead(request: Request):
    user = await get_current_user(request)
    await db.users.update_one(
        {"id": user["id"]},
        {"$unset": {
            "letterhead_path": "",
            "letterhead_url": "",
            "letterhead_content_type": "",
            "letterhead_size": "",
            "letterhead_uploaded_at": "",
        }},
    )
    # Storage has no delete API per playbook — soft-delete via DB only.
    return {"ok": True}


# -------------------------------------------------------------- receipts ---
ALLOWED_RECEIPT_TYPES = {
    "image/png", "image/jpeg", "image/jpg", "image/webp",
    "image/heic", "image/heif", "application/pdf",
}
RECEIPT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB — phone-photo receipts fit easily


@router.post("/receipt")
async def upload_receipt(request: Request, file: UploadFile = File(...)):
    """Stash an expense receipt (image or PDF). Returns the `path` + a
    `/api/files/...` URL the frontend can stick on the expense record."""
    user = await get_current_user(request)
    ctype = (file.content_type or "").lower()
    if ctype not in ALLOWED_RECEIPT_TYPES:
        raise HTTPException(400, "Please upload a PNG, JPG, WebP, HEIC, or PDF.")
    data = await file.read()
    if len(data) > RECEIPT_MAX_BYTES:
        raise HTTPException(413, "Please use a file smaller than 5 MB.")
    if len(data) == 0:
        raise HTTPException(400, "That file appears to be empty.")
    ext_map = {
        "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
        "image/webp": "webp", "image/heic": "heic", "image/heif": "heif",
        "application/pdf": "pdf",
    }
    ext = ext_map.get(ctype, "bin")
    path = f"{APP_NAME}/receipt/{user['id']}/{uuid.uuid4()}.{ext}"
    try:
        result = put_object(path, data, ctype)
    except Exception as e:
        logger.exception(f"Receipt upload failed: {e}")
        raise HTTPException(502, "We couldn't save that receipt right now. Please try again in a moment.")

    stored_path = result.get("path", path)
    return {
        "ok": True,
        "path": stored_path,
        "url": f"/api/files/{stored_path}",
        "content_type": ctype,
        "size": result.get("size", len(data)),
    }
