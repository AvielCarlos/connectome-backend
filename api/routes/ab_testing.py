"""
A/B Testing API Routes
Handles UI surface event recording and variant queries.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.middleware import get_current_user_id
from ora.ab_testing import record_ui_event, get_winning_variant, get_ui_variant

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ab", tags=["ab_testing"])


class UIEventBody(BaseModel):
    surface: str
    variant: str
    event_type: str
    value: float = 1.0


@router.post("/ui-event")
async def record_ui_event_endpoint(
    body: UIEventBody,
    user_id: str = Depends(get_current_user_id),
):
    """
    Record a UI interaction event for a surface/variant pair.
    Used by OraClient.recordUIEvent() on the mobile client.
    """
    try:
        await record_ui_event(
            surface=body.surface,
            variant=body.variant,
            event_type=body.event_type,
            value=body.value,
        )
    except Exception as e:
        logger.warning(f"record_ui_event failed: {e}")
        # Non-critical — don't fail the client request
    return {"ok": True}


@router.get("/variant/{surface}")
async def get_variant_endpoint(
    surface: str,
    user_id: str = Depends(get_current_user_id),
):
    """
    Get the current A/B variant for a UI surface.
    Returns the winning variant if one has been declared, otherwise
    the deterministically-assigned variant for this user.
    """
    from ora.agents.ui_ab_testing import UI_TESTS

    cfg = UI_TESTS.get(surface)
    if not cfg:
        # Unknown surface — return null gracefully
        return {"variant": None}

    try:
        variant = await get_ui_variant(
            user_id=user_id,
            surface=surface,
            variants=cfg["variants"],
            weights=cfg.get("weights"),
        )
        return {"variant": variant}
    except Exception as e:
        logger.warning(f"get_variant failed for surface={surface}: {e}")
        return {"variant": None}
