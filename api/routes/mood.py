"""
Mood API Routes

Endpoints:
  POST /api/mood/check — receive mood index (0-4) and store it
"""

import logging
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.middleware import get_current_user_id
from core.database import execute

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/mood", tags=["mood"])

MOOD_LABELS = ["exhausted", "neutral", "okay", "good", "energised"]


class MoodCheckRequest(BaseModel):
    mood_index: int = Field(..., ge=0, le=4)


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
