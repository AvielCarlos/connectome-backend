"""
Feedback screenshot storage.

Stores data-URL screenshots outside Postgres when a temporary object reference is
needed. Production can use any S3/R2-compatible bucket; development falls back to
local files so text feedback is never coupled to object storage availability.

Privacy principle: callers should treat screenshots as ephemeral context. Analyse
or extract the useful signal, then delete the raw image unless an explicit product
workflow requires retaining it.
"""

from __future__ import annotations

import base64
import binascii
import logging
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from uuid import uuid4

from core.config import settings

logger = logging.getLogger(__name__)

_DATA_URL_RE = re.compile(r"^data:(?P<content_type>[-\w.+/]+);base64,(?P<payload>.+)$", re.DOTALL)
_ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp"}


@dataclass(frozen=True)
class StoredScreenshot:
    """Durable screenshot reference stored with a feedback record."""

    key: str
    url: Optional[str]
    content_type: str
    size_bytes: int
    backend: str


class ScreenshotStorageError(Exception):
    """Raised when screenshot payload parsing or storage fails."""


def _extension_for_content_type(content_type: str) -> str:
    if content_type == "image/jpeg":
        return ".jpg"
    return mimetypes.guess_extension(content_type) or ".bin"


def _decode_data_url(data_url: str) -> tuple[bytes, str]:
    match = _DATA_URL_RE.match(data_url.strip())
    if not match:
        raise ScreenshotStorageError("screenshot must be a base64 data URL")

    content_type = match.group("content_type").lower()
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise ScreenshotStorageError(f"unsupported screenshot content type: {content_type}")

    try:
        payload = base64.b64decode(match.group("payload"), validate=True)
    except (binascii.Error, ValueError) as err:
        raise ScreenshotStorageError("invalid screenshot base64 payload") from err

    max_bytes = settings.FEEDBACK_SCREENSHOT_MAX_BYTES
    if len(payload) > max_bytes:
        raise ScreenshotStorageError(f"screenshot exceeds max size of {max_bytes} bytes")

    return payload, content_type


def _build_key(user_id: str, content_type: str) -> str:
    ext = _extension_for_content_type(content_type)
    safe_user = user_id.replace("/", "_")
    return f"feedback-screenshots/{safe_user}/{uuid4().hex}{ext}"


def _join_url(base_url: str, key: str) -> str:
    return f"{base_url.rstrip('/')}/{key.lstrip('/')}"


def _store_local(payload: bytes, key: str, content_type: str) -> StoredScreenshot:
    root = Path(settings.FEEDBACK_SCREENSHOT_LOCAL_DIR)
    target = root / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)

    public_base = settings.FEEDBACK_SCREENSHOT_PUBLIC_BASE_URL.strip()
    url = _join_url(public_base, key) if public_base else target.resolve().as_uri()
    return StoredScreenshot(
        key=key,
        url=url,
        content_type=content_type,
        size_bytes=len(payload),
        backend="local",
    )


def _store_s3(payload: bytes, key: str, content_type: str) -> StoredScreenshot:
    try:
        import boto3  # type: ignore
    except ImportError as err:
        raise ScreenshotStorageError("boto3 is required for S3/R2 screenshot storage") from err

    bucket = settings.FEEDBACK_SCREENSHOT_S3_BUCKET.strip()
    if not bucket:
        raise ScreenshotStorageError("FEEDBACK_SCREENSHOT_S3_BUCKET is required")

    session = boto3.session.Session(
        aws_access_key_id=settings.FEEDBACK_SCREENSHOT_S3_ACCESS_KEY_ID or None,
        aws_secret_access_key=settings.FEEDBACK_SCREENSHOT_S3_SECRET_ACCESS_KEY or None,
        region_name=settings.FEEDBACK_SCREENSHOT_S3_REGION or None,
    )
    client = session.client(
        "s3",
        endpoint_url=settings.FEEDBACK_SCREENSHOT_S3_ENDPOINT_URL or None,
    )
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=payload,
        ContentType=content_type,
    )

    public_base = settings.FEEDBACK_SCREENSHOT_PUBLIC_BASE_URL.strip()
    if public_base:
        url = _join_url(public_base, key)
    elif (
        not settings.FEEDBACK_SCREENSHOT_S3_ENDPOINT_URL
        and settings.FEEDBACK_SCREENSHOT_S3_REGION
        and settings.FEEDBACK_SCREENSHOT_S3_REGION != "auto"
    ):
        url = f"https://{bucket}.s3.{settings.FEEDBACK_SCREENSHOT_S3_REGION}.amazonaws.com/{key}"
    else:
        url = None

    return StoredScreenshot(
        key=key,
        url=url,
        content_type=content_type,
        size_bytes=len(payload),
        backend="s3",
    )


async def store_feedback_screenshot(data_url: str, user_id: str) -> StoredScreenshot:
    """
    Persist a feedback screenshot and return durable references.

    Raises ScreenshotStorageError on failure. Callers should treat this as non-fatal
    so the feedback text still lands in Postgres.
    """

    payload, content_type = _decode_data_url(data_url)
    key = _build_key(user_id, content_type)

    backend = settings.FEEDBACK_SCREENSHOT_STORAGE_BACKEND.lower().strip()
    if backend == "s3":
        return _store_s3(payload, key, content_type)
    if backend != "local":
        logger.warning("Unknown feedback screenshot backend %r; using local fallback", backend)
    return _store_local(payload, key, content_type)


def _delete_local(key: str) -> bool:
    target = Path(settings.FEEDBACK_SCREENSHOT_LOCAL_DIR) / key
    try:
        if target.exists():
            target.unlink()
            return True
    except Exception as err:
        logger.warning("Local feedback screenshot delete failed for %s: %s", key, err)
    return False


def _delete_s3(key: str) -> bool:
    try:
        import boto3  # type: ignore
    except ImportError as err:
        raise ScreenshotStorageError("boto3 is required for S3/R2 screenshot deletion") from err

    bucket = settings.FEEDBACK_SCREENSHOT_S3_BUCKET.strip()
    if not bucket:
        raise ScreenshotStorageError("FEEDBACK_SCREENSHOT_S3_BUCKET is required")

    session = boto3.session.Session(
        aws_access_key_id=settings.FEEDBACK_SCREENSHOT_S3_ACCESS_KEY_ID or None,
        aws_secret_access_key=settings.FEEDBACK_SCREENSHOT_S3_SECRET_ACCESS_KEY or None,
        region_name=settings.FEEDBACK_SCREENSHOT_S3_REGION or None,
    )
    client = session.client(
        "s3",
        endpoint_url=settings.FEEDBACK_SCREENSHOT_S3_ENDPOINT_URL or None,
    )
    client.delete_object(Bucket=bucket, Key=key)
    return True


async def delete_feedback_screenshot(stored: StoredScreenshot) -> bool:
    """Delete a previously stored feedback screenshot object.

    Best-effort by design: raw screenshot deletion should be attempted quickly,
    while the feedback submission itself must still succeed. Returns True when a
    delete request was successfully issued/performed.
    """

    backend = (stored.backend or settings.FEEDBACK_SCREENSHOT_STORAGE_BACKEND).lower().strip()
    if backend == "s3":
        return _delete_s3(stored.key)
    if backend == "local":
        return _delete_local(stored.key)
    logger.warning("Unknown feedback screenshot backend %r; cannot delete %s", backend, stored.key)
    return False
