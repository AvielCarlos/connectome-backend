"""
Screens API Routes

Endpoints:
  POST /api/screens/next    — single next screen (original)
  POST /api/screens/batch   — prefetch multiple screens (new)
  POST /api/screens/save    — bookmark a screen for later (new)
  GET  /api/screens/:id     — retrieve a screen by ID
"""

import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from core.models import (
    ScreenRequest,
    ScreenResponse,
    ScreenSpec,
    FeedbackOverlay,
    ScreenMetadata,
    DomainType,
)
from core.config import settings
from api.middleware import get_current_user_id
from ora.brain import get_brain
from ora.user_model import get_daily_screen_count
from core.database import fetchrow, execute
from core.geo import get_location_for_ip, geo_to_context_hints
from uuid import UUID

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/screens", tags=["screens"])


def _client_ip(request: Request) -> str:
    """Extract real client IP, respecting X-Forwarded-For from reverse proxy."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.post("/next", response_model=ScreenResponse)
async def get_next_screen(
    body: ScreenRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    """
    Request the next screen from Ora.
    Respects freemium limits (10/day for free users).
    """
    # Check subscription tier for rate limiting
    user_row = await fetchrow(
        "SELECT subscription_tier FROM users WHERE id = $1", UUID(user_id)
    )
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    tier = user_row["subscription_tier"]
    is_free = tier == "free"

    # Get current count BEFORE incrementing (brain will increment)
    current_count = await get_daily_screen_count(user_id)
    daily_limit = settings.FREE_TIER_DAILY_SCREENS

    if is_free and current_count >= daily_limit:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"Daily limit of {daily_limit} screens reached. Upgrade to premium for unlimited access.",
            headers={"X-Upgrade-URL": "/api/monetization/upgrade"},
        )

    brain = get_brain()

    # Geo context — resolve IP asynchronously, inject into brain call
    geo_hints = {}
    try:
        ip = _client_ip(request)
        geo = await get_location_for_ip(ip)
        geo_hints = geo_to_context_hints(geo)
    except Exception:
        pass

    try:
        spec_dict, db_id, screens_today = await brain.get_screen(
            user_id=user_id,
            context=body.context,
            goal_id=body.goal_id,
            domain=body.domain,
            geo_hints=geo_hints,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Brain error for user {user_id[:8]}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Ora is temporarily unavailable")

    # Parse spec into Pydantic model (validates structure)
    try:
        screen = ScreenSpec(
            screen_id=spec_dict["screen_id"],
            type=spec_dict.get("type", "unknown"),
            layout=spec_dict.get("layout", "scroll"),
            components=spec_dict.get("components", []),
            feedback_overlay=FeedbackOverlay(
                **spec_dict.get(
                    "feedback_overlay",
                    {"type": "star_rating", "position": "bottom_right", "always_visible": True},
                )
            ),
            metadata=ScreenMetadata(**spec_dict.get("metadata", {"agent": "OraBrain"})),
        )
    except Exception as e:
        logger.error(f"Spec parse error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Screen spec generation error")

    return ScreenResponse(
        screen=screen,
        screen_spec_db_id=db_id,
        screens_today=screens_today,
        daily_limit=daily_limit,
        is_limited=is_free,
    )


# ---------------------------------------------------------------------------
# Batch endpoint — prefetch multiple screens at once
# ---------------------------------------------------------------------------

class BatchScreenRequest(BaseModel):
    count: int = 3
    domain: Optional[DomainType] = None


@router.post("/batch", response_model=List[ScreenResponse])
async def get_screen_batch(
    body: BatchScreenRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    """
    Fetch up to 5 screens at once for TikTok-style prefetching.
    Each screen goes through the same logic as /next.
    Returns a list of ScreenResponse objects.
    """
    count = max(1, min(body.count, 5))  # cap at 5

    # Check subscription tier once
    user_row = await fetchrow(
        "SELECT subscription_tier FROM users WHERE id = $1", UUID(user_id)
    )
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    tier = user_row["subscription_tier"]
    is_free = tier == "free"
    daily_limit = settings.FREE_TIER_DAILY_SCREENS

    if is_free:
        current_count = await get_daily_screen_count(user_id)
        available = daily_limit - current_count
        if available <= 0:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Daily limit of {daily_limit} screens reached.",
            )
        count = min(count, available)

    brain = get_brain()
    results: List[ScreenResponse] = []

    # Geo context for batch (resolve once, reuse)
    batch_geo_hints = {}
    try:
        ip = _client_ip(request)
        geo = await get_location_for_ip(ip)
        batch_geo_hints = geo_to_context_hints(geo)
    except Exception:
        pass

    # Fetch screens sequentially to respect rate limits and user model updates
    for _ in range(count):
        try:
            spec_dict, db_id, screens_today = await brain.get_screen(
                user_id=user_id,
                context=None,
                goal_id=None,
                domain=body.domain,
                geo_hints=batch_geo_hints,
            )
            screen = ScreenSpec(
                screen_id=spec_dict["screen_id"],
                type=spec_dict.get("type", "unknown"),
                layout=spec_dict.get("layout", "scroll"),
                components=spec_dict.get("components", []),
                feedback_overlay=FeedbackOverlay(
                    **spec_dict.get(
                        "feedback_overlay",
                        {"type": "star_rating", "position": "bottom_right", "always_visible": True},
                    )
                ),
                metadata=ScreenMetadata(**spec_dict.get("metadata", {"agent": "OraBrain"})),
            )
            results.append(
                ScreenResponse(
                    screen=screen,
                    screen_spec_db_id=db_id,
                    screens_today=screens_today,
                    daily_limit=daily_limit,
                    is_limited=is_free,
                )
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Batch screen error for user {user_id[:8]}: {e}", exc_info=True)
            # Return partial results if we got some
            break

    if not results:
        raise HTTPException(status_code=500, detail="Could not generate screens")

    return results


# ---------------------------------------------------------------------------
# Save-for-later endpoint
# ---------------------------------------------------------------------------

class SaveScreenRequest(BaseModel):
    screen_spec_id: str


@router.post("/save")
async def save_screen_for_later(
    body: SaveScreenRequest,
    user_id: str = Depends(get_current_user_id),
):
    """
    Bookmark a screen so Ora resurfaces it in ~24 hours.
    Upserts an interaction row with saved=true.
    """
    try:
        screen_uuid = UUID(body.screen_spec_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid screen_spec_id")

    try:
        await execute(
            """
            INSERT INTO interactions (user_id, screen_spec_id, saved, created_at)
            VALUES ($1, $2, TRUE, NOW())
            ON CONFLICT (user_id, screen_spec_id)
            DO UPDATE SET saved = TRUE
            """,
            UUID(user_id),
            screen_uuid,
        )
    except Exception as e:
        logger.error(f"Save screen error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to save screen")

    return {"ok": True, "message": "Saved! Ora will resurface this in 24h."}


# ---------------------------------------------------------------------------
# Get by ID
# ---------------------------------------------------------------------------

@router.get("/{screen_id}")
async def get_screen_by_id(
    screen_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Retrieve a previously generated screen by its DB ID."""
    try:
        row = await fetchrow(
            "SELECT spec, agent_type, global_rating FROM screen_specs WHERE id = $1",
            UUID(screen_id),
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid screen ID")

    if not row:
        raise HTTPException(status_code=404, detail="Screen not found")

    return {
        "spec": dict(row["spec"]),
        "agent_type": row["agent_type"],
        "global_rating": row["global_rating"],
    }
