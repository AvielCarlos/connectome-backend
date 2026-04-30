"""
Notifications API Routes
Handles notification open tracking for re-engagement push notifications.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends

from core.database import execute, fetch, fetchrow
from api.middleware import get_current_user_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("")
async def list_notifications(
    current_user_id: str = Depends(get_current_user_id),
):
    """
    Lightweight in-app notification inbox.

    This intentionally reuses scheduled_notifications so the bell can be useful
    without creating another always-on, model-heavy feature. Treat as an
    experimental/chopping-block surface until engagement proves it deserves to
    stay.
    """
    rows = await fetch(
        """
        SELECT id, goal_id, message, scheduled_for, sent, opened, created_at
        FROM scheduled_notifications
        WHERE user_id = $1
        ORDER BY COALESCE(scheduled_for, created_at) DESC
        LIMIT 20
        """,
        UUID(current_user_id),
    )
    items = []
    unread_count = 0
    for row in rows:
        unread = bool(row["sent"] and not row["opened"])
        if unread:
            unread_count += 1
        items.append({
            "id": str(row["id"]),
            "goal_id": str(row["goal_id"]) if row["goal_id"] else None,
            "message": row["message"] or "Aura has an update for your path.",
            "scheduled_for": row["scheduled_for"].isoformat() if row["scheduled_for"] else None,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "sent": bool(row["sent"]),
            "opened": bool(row["opened"]),
            "unread": unread,
            "type": "reengagement",
        })
    return {
        "items": items,
        "unread_count": unread_count,
        "feature_status": "experimental",
        "chopping_block": True,
        "recommendation": "Keep only if notifications drive meaningful return visits or path completions without noisy model/tool spend.",
    }


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
