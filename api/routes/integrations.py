"""
User Integrations API Routes
=============================
Manages connected third-party integrations (currently Google Drive).

Endpoints:
  GET    /api/users/integrations               — list connected integrations + privacy levels
  POST   /api/users/integrations/drive         — update Drive privacy level
  POST   /api/users/integrations/drive/sync    — trigger manual Drive sync
  DELETE /api/users/integrations/drive         — disconnect Drive (revoke + delete docs)
"""

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from api.middleware import get_current_user_id
from core.database import execute, fetchrow, fetchval

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/users", tags=["integrations"])


# ─── Models ──────────────────────────────────────────────────────────────────

class DrivePrivacyUpdate(BaseModel):
    level: str  # 'none', 'goals_only', 'full'


VALID_PRIVACY_LEVELS = {"none", "goals_only", "full"}


# ─── Routes ──────────────────────────────────────────────────────────────────

@router.get("/integrations")
async def get_integrations(user_id: str = Depends(get_current_user_id)):
    """
    Return the current user's connected integrations and their privacy settings.
    """
    row = await fetchrow(
        """
        SELECT drive_connected, drive_privacy_level, scopes, token_expiry
        FROM google_oauth_tokens
        WHERE user_id = $1
        """,
        UUID(user_id),
    )

    if not row:
        return {
            "ok": True,
            "integrations": {
                "google_drive": {
                    "connected": False,
                    "privacy_level": "none",
                    "scopes": [],
                }
            },
        }

    return {
        "ok": True,
        "integrations": {
            "google_drive": {
                "connected": bool(row["drive_connected"]),
                "privacy_level": row["drive_privacy_level"] or "none",
                "scopes": list(row["scopes"] or []),
                "token_expiry": row["token_expiry"].isoformat() if row["token_expiry"] else None,
            }
        },
    }


@router.post("/integrations/drive")
async def update_drive_privacy(
    body: DrivePrivacyUpdate,
    user_id: str = Depends(get_current_user_id),
):
    """
    Update Drive privacy level.
    Levels: 'none' | 'goals_only' | 'full'
    """
    if body.level not in VALID_PRIVACY_LEVELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid privacy level. Must be one of: {', '.join(VALID_PRIVACY_LEVELS)}",
        )

    # Ensure a token record exists (user must have connected Google at least for login)
    existing = await fetchrow(
        "SELECT id FROM google_oauth_tokens WHERE user_id = $1", UUID(user_id)
    )
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Google account connected. Sign in with Google first.",
        )

    await execute(
        """
        UPDATE google_oauth_tokens
        SET drive_privacy_level = $2, updated_at = NOW()
        WHERE user_id = $1
        """,
        UUID(user_id),
        body.level,
    )

    return {
        "ok": True,
        "drive_privacy_level": body.level,
        "message": f"Drive privacy level updated to '{body.level}'",
    }


@router.post("/integrations/drive/sync")
async def sync_drive(
    user_id: str = Depends(get_current_user_id),
):
    """
    Trigger a manual Google Drive sync for the current user.
    Uses their stored OAuth tokens and respects their privacy level.
    """
    row = await fetchrow(
        "SELECT drive_connected, drive_privacy_level FROM google_oauth_tokens WHERE user_id = $1",
        UUID(user_id),
    )

    if not row or not row["drive_connected"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google Drive is not connected. Connect Drive first.",
        )

    privacy_level = row["drive_privacy_level"] or "none"
    if privacy_level == "none":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Drive sharing is set to 'none'. Update your privacy level first.",
        )

    # Run Drive sync using DriveAgentV2
    try:
        from ora.agents.drive_agent_v2 import DriveAgentV2
        from ora.brain import get_brain

        brain = get_brain()
        agent = DriveAgentV2(openai_client=brain._openai)
        summary = await agent.sync(user_id=user_id)
        return {"ok": True, "sync": summary}
    except Exception as e:
        logger.error(f"Drive sync failed for user {user_id[:8]}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Drive sync failed: {e}",
        )


@router.delete("/integrations/drive")
async def disconnect_drive(
    user_id: str = Depends(get_current_user_id),
):
    """
    Disconnect Google Drive: revoke tokens and delete all indexed documents.
    The user stays logged in.
    """
    # Delegate to google_auth route logic
    from api.routes.google_auth import drive_disconnect as _disconnect
    return await _disconnect(user_id=user_id)
