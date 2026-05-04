"""
Mood API Routes

Endpoints:
  POST /api/mood/check — receive mood index (0-4) and store it
  POST /api/mood/daily-checkin — 30-second Ora momentum check-in
"""

import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.middleware import get_current_user_id
from core.database import execute, fetchrow

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/mood", tags=["mood"])

MOOD_LABELS = ["exhausted", "neutral", "okay", "good", "energised"]


class MoodCheckRequest(BaseModel):
    mood_index: int = Field(..., ge=0, le=4)


class DailyCheckinRequest(BaseModel):
    mood_index: int = Field(..., ge=0, le=4)
    capacity: str = Field(..., min_length=1, max_length=40)
    focus: str = Field(..., min_length=1, max_length=180)
    blocker: Optional[str] = Field(default=None, max_length=220)


def _focus_prompt(mood_label: str, capacity: str, focus: str, blocker: Optional[str]) -> str:
    if mood_label in {"exhausted", "neutral"} or "5" in capacity:
        prefix = "Keep it tiny and nervous-system friendly"
    elif blocker:
        prefix = "Remove one point of friction first"
    else:
        prefix = "Turn the energy into one visible win"
    blocker_text = f" Blocker to watch: {blocker}." if blocker else ""
    return f"{prefix}: spend {capacity} on “{focus}”.{blocker_text}"


@router.post("/check")
async def check_mood(
    body: MoodCheckRequest,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """
    Record the user's mood for this session.
    Stores in interactions table and caches in Redis for 8 hours.
    """
    mood_label = MOOD_LABELS[body.mood_index]

    # Store in interactions table (screen_spec_id is nullable)
    try:
        await execute(
            """
            INSERT INTO interactions (id, user_id, exit_point)
            VALUES ($1, $2, $3)
            """,
            uuid.uuid4(),
            uuid.UUID(user_id),
            f"mood_check:{body.mood_index}",
        )
    except Exception as e:
        # Non-fatal — continue even if storage fails
        logger.warning(f"Mood storage failed: {e}")

    # Cache in Redis for 8 hours
    try:
        from core.redis_client import get_redis
        r = await get_redis()
        await r.setex(f"mood:{user_id}", 8 * 3600, str(body.mood_index))
    except Exception as e:
        logger.warning(f"Mood Redis cache failed: {e}")

    logger.info(f"Mood check: user={user_id[:8]} mood={body.mood_index} ({mood_label})")

    return {"ok": True, "mood": mood_label}


@router.post("/daily-checkin")
async def daily_checkin(
    body: DailyCheckinRequest,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """
    Capture a lightweight morning momentum check-in.
    Stores the signal in interactions.exit_point as JSON to avoid schema churn,
    awards engagement XP when available, and returns a concrete Ora focus line.
    """
    mood_label = MOOD_LABELS[body.mood_index]
    payload = {
        "type": "daily_momentum_checkin",
        "mood_index": body.mood_index,
        "mood": mood_label,
        "capacity": body.capacity,
        "focus": body.focus,
        "blocker": body.blocker,
    }

    try:
        await execute(
            """
            INSERT INTO interactions (id, user_id, exit_point)
            VALUES ($1, $2, $3)
            """,
            uuid.uuid4(),
            uuid.UUID(user_id),
            "daily_checkin:" + str(payload),
        )
    except Exception as e:
        logger.warning(f"Daily check-in storage failed: {e}")

    try:
        from api.routes.gamification import _award_xp
        xp_awarded = await _award_xp(user_id, "daily_login", context={"source": "daily_momentum_checkin"})
    except Exception as e:
        logger.warning(f"Daily check-in XP failed: {e}")
        xp_awarded = 0

    active_goal = None
    try:
        row = await fetchrow(
            "SELECT id, title FROM goals WHERE user_id = $1 AND status = 'active' ORDER BY created_at DESC LIMIT 1",
            uuid.UUID(user_id),
        )
        if row:
            active_goal = {"id": str(row["id"]), "title": row["title"]}
    except Exception as e:
        logger.warning(f"Daily check-in goal lookup failed: {e}")

    return {
        "ok": True,
        "mood": mood_label,
        "xp_awarded": xp_awarded,
        "active_goal": active_goal,
        "ora_focus": _focus_prompt(mood_label, body.capacity, body.focus, body.blocker),
    }
