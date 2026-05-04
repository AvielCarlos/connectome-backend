"""
Session API Routes
Handles session lifecycle events, including session-end summaries.
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from core.database import fetch
from api.middleware import get_current_user_id
from aura.brain import get_brain
from aura.user_model import update_aura_memory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/session", tags=["session"])


class SessionEndRequest(BaseModel):
    user_id: str
    session_started_at: datetime


@router.post("/end")
async def end_session(
    payload: SessionEndRequest,
    current_user_id: str = Depends(get_current_user_id),
):
    """
    Called when the user ends a session (app goes to background or navigates away).
    Generates a session summary, updates the user model, and schedules a
    re-engagement notification if the user has active mid-progress goals.
    """
    # Validate: requesting user can only end their own session
    if current_user_id != payload.user_id:
        raise HTTPException(status_code=403, detail="Cannot end session for another user")

    brain = get_brain()

    # Load all interactions since session_started_at for this user
    interaction_rows = await fetch(
        """
        SELECT i.id, i.rating, i.completed, i.time_on_screen_ms, i.exit_point,
               i.created_at, s.agent_type
        FROM interactions i
        LEFT JOIN screen_specs s ON s.id = i.screen_spec_id
        WHERE i.user_id = $1 AND i.created_at >= $2
        ORDER BY i.created_at ASC
        """,
        UUID(payload.user_id),
        payload.session_started_at,
    )

    interactions = [dict(r) for r in interaction_rows]

    if not interactions:
        logger.debug(f"Session end: no interactions found for user={payload.user_id[:8]}")
        return {
            "ok": True,
            "summary": None,
            "reengagement": None,
            "message": "No interactions in this session",
        }

    # Generate session summary
    try:
        summary = await brain.generate_session_summary(
            user_id=payload.user_id,
            interactions=interactions,
            session_started_at=payload.session_started_at,
        )
    except Exception as e:
        logger.error(f"Session summary generation failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate session summary")

    # Update Aura's persistent memory for this user (non-blocking)
    try:
        openai_client = getattr(brain, '_openai', None)
        await update_aura_memory(
            user_id=payload.user_id,
            session_summary=summary,
            openai_client=openai_client,
        )
    except Exception as e:
        logger.warning(f"aura_memory update failed (non-fatal): {e}")

    # Schedule re-engagement notification (non-blocking — failure doesn't fail the request)
    reengagement = None
    try:
        reengagement = await brain.schedule_reengagement(
            user_id=payload.user_id,
            session_summary=summary,
        )
    except Exception as e:
        logger.warning(f"Re-engagement scheduling failed (non-fatal): {e}")

    logger.info(
        f"Session ended: user={payload.user_id[:8]} screens={summary.get('screens_shown', 0)} "
        f"delta={summary.get('fulfilment_delta', 0.0):+.3f} reengagement={'scheduled' if reengagement else 'none'}"
    )

    return {
        "ok": True,
        "summary": summary,
        "reengagement": reengagement,
    }


@router.get("/last-summary")
async def get_last_session_summary(
    current_user_id: str = Depends(get_current_user_id),
):
    """
    Return the most recent session summary for this user.
    Used by the mobile ProfileScreen to surface a post-session insight.
    """
    from core.database import fetchrow
    import json

    row = await fetchrow(
        """
        SELECT screens_shown, highly_rated, early_exits,
               emerging_interests, avoid_topics, aura_note,
               fulfilment_delta, session_started_at, session_ended_at
        FROM session_summaries
        WHERE user_id = $1
        ORDER BY session_ended_at DESC
        LIMIT 1
        """,
        UUID(current_user_id),
    )

    if not row:
        return None

    emerging = row["emerging_interests"]
    if isinstance(emerging, str):
        try:
            emerging = json.loads(emerging)
        except Exception:
            emerging = []

    return {
        "screens_shown": row["screens_shown"],
        "highly_rated": row["highly_rated"],
        "early_exits": row["early_exits"],
        "emerging_interests": emerging or [],
        "aura_note": row["aura_note"],
        "fulfilment_delta": float(row["fulfilment_delta"] or 0.0),
        "session_started_at": row["session_started_at"].isoformat() if row["session_started_at"] else None,
        "session_ended_at": row["session_ended_at"].isoformat() if row["session_ended_at"] else None,
    }
