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

import asyncio
import json
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel

from api.middleware import get_current_user_id
from core.database import execute, fetchrow, fetchval

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/users", tags=["integrations"])


# ─── Models ──────────────────────────────────────────────────────────────────

class DrivePrivacyUpdate(BaseModel):
    level: str  # 'none', 'goals_only', 'full'


VALID_PRIVACY_LEVELS = {"none", "goals_only", "full"}


class TwitterIngestRequest(BaseModel):
    twitter_handle: str  # e.g. "@elonmusk" or "elonmusk"


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
        from aura.agents.drive_agent_v2 import DriveAgentV2
        from aura.brain import get_brain

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


# ─── Twitter/X Signal Ingestion (Integration E) ──────────────────────────────────


@router.post("/integrations/twitter")
async def ingest_twitter(
    body: TwitterIngestRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_user_id),
):
    """
    Trigger Twitter/X likes ingestion for the current user.
    Runs as a background task — returns immediately with a 202 Accepted.
    Poll GET /api/users/integrations/twitter/status to check completion.

    Body: {"twitter_handle": "@username"}
    """
    handle = body.twitter_handle.strip()
    if not handle:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="twitter_handle is required",
        )

    async def _run_ingestion():
        try:
            from aura.brain import get_brain
            from aura.agents.twitter_agent import TwitterSignalAgent
            brain = get_brain()
            agent = TwitterSignalAgent(openai_client=brain._openai)
            await agent.ingest_twitter_signals(user_id=user_id, twitter_handle=handle)
        except Exception as e:
            logger.error(f"Twitter ingestion background task failed for user {user_id[:8]}: {e}", exc_info=True)

    background_tasks.add_task(_run_ingestion)

    return {
        "ok": True,
        "status": "ingestion_started",
        "twitter_handle": handle,
        "message": "Twitter signals are being ingested in the background. Poll /status to check.",
    }


@router.get("/integrations/twitter/status")
async def twitter_status(
    user_id: str = Depends(get_current_user_id),
):
    """
    Return the Twitter signal ingestion status for the current user.
    Also surfaces the discovered topics + interests if ingestion is complete.
    """
    try:
        from core.redis_client import get_redis
        r = await get_redis()

        status_raw = await r.get(f"user:{user_id}:twitter_ingestion_status")
        signals_raw = await r.get(f"user:{user_id}:twitter_signals")

        ingestion_status = json.loads(status_raw) if status_raw else {"status": "not_started"}
        signals = json.loads(signals_raw) if signals_raw else None

        return {
            "ok": True,
            "ingestion": ingestion_status,
            "signals": signals,
        }
    except Exception as e:
        logger.warning(f"Twitter status check failed for user {user_id[:8]}: {e}")
        return {"ok": True, "ingestion": {"status": "unknown"}, "signals": None}
