"""Object storage helpers for Steno Desk.

Primary backend: S3-compatible object storage via boto3 (requires
S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_BUCKET_NAME, and optionally
S3_ENDPOINT_URL).
Fallback: local filesystem at STORE_DIR (env var, defaults to
/tmp/stenodesk-storage). The fallback activates automatically when S3
credentials are not set, so uploads work in local dev and on
Railway without an S3 account.

Both backends expose the same two functions: put_object(path, data, ctype)
and get_object(path) -> (bytes, ctype). The init_storage() function is
a no-op in local mode.
"""
import logging
import os
import threading
from pathlib import Path
from typing import Tuple

import boto3

logger = logging.getLogger(__name__)

APP_NAME = os.environ.get("APP_NAME", "stenodesk")
STORE_DIR = os.environ.get("STORE_DIR", "/tmp/stenodesk-storage")

_s3_client = None
_lock = threading.Lock()


def _has_s3_config() -> bool:
    """Return True when S3 credentials are configured."""
    return bool(
        os.environ.get("S3_ACCESS_KEY_ID", "").strip()
        and os.environ.get("S3_SECRET_ACCESS_KEY", "").strip()
    )


def _get_s3_bucket() -> str:
    return os.environ.get("S3_BUCKET_NAME", APP_NAME)


# ---------------------------------------------------------------------------
# Local filesystem fallback
# ---------------------------------------------------------------------------
def _local_put(path: str, data: bytes, content_type: str) -> dict:
    """Write bytes to the local store directory."""
    full = Path(STORE_DIR) / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(data)
    logger.info(f"Local storage: saved {len(data)} bytes to {path}")
    return {"path": path, "size": len(data), "etag": None}


def _local_get(path: str) -> Tuple[bytes, str]:
    """Read bytes from the local store directory."""
    full = Path(STORE_DIR) / path
    if not full.exists():
        raise FileNotFoundError(f"File not found: {path}")
    data = full.read_bytes()
    # Infer content type from extension if we can't determine it
    import mimetypes
    ctype = mimetypes.guess_type(str(full))[0] or "application/octet-stream"
    return data, ctype


# ---------------------------------------------------------------------------
# Public API — same signature regardless of backend
# ---------------------------------------------------------------------------
def init_storage(force: bool = False) -> str | None:
    """Initialize the S3 client for object storage.

    In local-fallback mode (no S3 credentials), this is a no-op that
    returns None. Safe to call repeatedly.
    """
    global _s3_client
    if not _has_s3_config():
        return None  # local mode — no init needed

    with _lock:
        if _s3_client and not force:
            return None
        _s3_client = boto3.client(
            "s3",
            endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None,
            aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        )
        logger.info("S3 object storage initialized")
        return None


def put_object(path: str, data: bytes, content_type: str) -> dict:
    """Upload bytes to `path`. Returns {"path","size","etag"}.

    Uses S3 object storage when S3 credentials are set, otherwise
    falls back to the local filesystem.
    """
    if not _has_s3_config():
        return _local_put(path, data, content_type)

    if not _s3_client:
        init_storage()
    _s3_client.put_object(
        Bucket=_get_s3_bucket(),
        Key=path,
        Body=data,
        ContentType=content_type,
    )
    logger.info(f"S3 storage: saved {len(data)} bytes to {path}")
    return {"path": path, "size": len(data), "etag": None}


def get_object(path: str) -> Tuple[bytes, str]:
    """Download `path`. Returns (bytes, content_type).

    Uses S3 object storage when S3 credentials are set, otherwise
    reads from the local filesystem.
    """
    if not _has_s3_config():
        return _local_get(path)

    if not _s3_client:
        init_storage()
    resp = _s3_client.get_object(Bucket=_get_s3_bucket(), Key=path)
    data = resp["Body"].read()
    ctype = resp.get("ContentType", "application/octet-stream")
    return data, ctype