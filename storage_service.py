"""Emergent object storage helpers for Steno Desk.

The storage service issues short-lived `storage_key` session tokens off the
EMERGENT_LLM_KEY. We init once at FastAPI startup and reuse for the lifetime
of the worker (re-init on 403).
"""
import logging
import os
import threading
from typing import Tuple

import requests

logger = logging.getLogger(__name__)

STORAGE_URL = "https://integrations.emergentagent.com/objstore/api/v1/storage"
APP_NAME = os.environ.get("APP_NAME", "stenodesk")

_storage_key: str | None = None
_lock = threading.Lock()


def _emergent_key() -> str:
    return os.environ["EMERGENT_LLM_KEY"]


def init_storage(force: bool = False) -> str:
    """Get (or refresh) the session storage_key. Safe to call repeatedly."""
    global _storage_key
    with _lock:
        if _storage_key and not force:
            return _storage_key
        resp = requests.post(
            f"{STORAGE_URL}/init",
            json={"emergent_key": _emergent_key()},
            timeout=30,
        )
        resp.raise_for_status()
        _storage_key = resp.json()["storage_key"]
        logger.info("Object storage initialized")
        return _storage_key


def put_object(path: str, data: bytes, content_type: str) -> dict:
    """Upload bytes to `path`. Returns {"path","size","etag"}."""
    key = init_storage()
    resp = requests.put(
        f"{STORAGE_URL}/objects/{path}",
        headers={"X-Storage-Key": key, "Content-Type": content_type},
        data=data,
        timeout=120,
    )
    if resp.status_code == 403:
        # Refresh and retry once
        key = init_storage(force=True)
        resp = requests.put(
            f"{STORAGE_URL}/objects/{path}",
            headers={"X-Storage-Key": key, "Content-Type": content_type},
            data=data,
            timeout=120,
        )
    resp.raise_for_status()
    return resp.json()


def get_object(path: str) -> Tuple[bytes, str]:
    """Download `path`. Returns (bytes, content_type)."""
    key = init_storage()
    resp = requests.get(
        f"{STORAGE_URL}/objects/{path}",
        headers={"X-Storage-Key": key},
        timeout=60,
    )
    if resp.status_code == 403:
        key = init_storage(force=True)
        resp = requests.get(
            f"{STORAGE_URL}/objects/{path}",
            headers={"X-Storage-Key": key},
            timeout=60,
        )
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type", "application/octet-stream")
