"""
Notifications API Routes
Handles notification open tracking for re-engagement push notifications.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends

from core.database import execute, fetchrow
from api.middleware import get_current_user_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.post("/{notification_id}/opened")
async def mark_notification_opened(
    notification_id: str,
    current_user_id: str = Depends(get_current_user_id),
):
    """
    Mark a scheduled notification as opened.
    Used to track return_rate_signal — did the re-engagement message bring the user back?
    """
    try:
        notif_uuid = UUID(notification_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid notification ID format")

    row = await fetchrow(
        "SELECT id, user_id, sent FROM scheduled_notifications WHERE id = $1",
        notif_uuid,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")

    # Validate ownership
    if str(row["user_id"]) != current_user_id:
        raise HTTPException(status_code=403, detail="Cannot mark another user's notification")

    if not row["sent"]:
        logger.warning(f"Notification {notification_id[:8]} opened before being sent — marking anyway")

    await execute(
        "UPDATE scheduled_notifications SET opened = TRUE WHERE id = $1",
        notif_uuid,
    )

    logger.info(f"Notification opened: id={notification_id[:8]} user={current_user_id[:8]}")

    return {"ok": True, "notification_id": notification_id, "opened": True}
