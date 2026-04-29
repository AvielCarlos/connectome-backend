"""
Screens API Routes

Endpoints:
  POST /api/screens/next    — single next screen (original)
  POST /api/screens/batch   — prefetch multiple screens (new)
  POST /api/screens/save    — bookmark a screen for later (new)
  GET  /api/screens/:id     — retrieve a screen by ID
"""

import asyncio
import json
import logging
import random
import uuid as _uuid_mod
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
from ora.user_model import get_daily_screen_count, increment_daily_screen_count
from core.database import fetchrow, execute
from core.geo import get_location_for_ip, geo_to_context_hints
from uuid import UUID

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/screens", tags=["screens"])


# ---------------------------------------------------------------------------
# IOO Graph helpers — smart feed routing
# ---------------------------------------------------------------------------

async def _get_user_has_goals(user_id: str) -> bool:
    """Return True if the user has at least one active goal."""
    row = await fetchrow(
        "SELECT id FROM goals WHERE user_id = $1 AND status = 'active' LIMIT 1",
        str(user_id),
    )
    return row is not None


def _ioo_node_to_screen_dict(node: dict) -> dict:
    """
    Convert an IOO graph node into a screen spec dict.
    The dict is JSON-serialisable and compatible with the ScreenSpec Pydantic model.
    """
    components = [
        {
            "type": "category_badge",
            "text": str(node.get("type", "")).upper().replace("_", " "),
            "color": "#8b5cf6",
        },
        {"type": "headline", "text": node.get("title", "")},
        {"type": "body", "text": node.get("description") or ""},
    ]
    if node.get("requires_time_hours"):
        components.append({"type": "meta", "text": f"\u23f1 ~{node['requires_time_hours']}h"})
    if node.get("requires_finances"):
        components.append({"type": "meta", "text": f"\U0001f4b0 ~${node['requires_finances']:.0f}"})
    components.append({
        "type": "action_button",
        "text": "Start this \u2192",
        "action": {
            "type": "open_url",
            "url": f"ioo://node/{node['id']}",
            "payload": {"node_id": str(node["id"])},
        },
    })
    return {
        "screen_id": str(_uuid_mod.uuid4()),
        "type": "opportunity",
        "layout": "card_stack",
        "components": components,
        "feedback_overlay": {
            "type": "star_rating",
            "position": "bottom_right",
            "always_visible": True,
        },
        "metadata": {
            "agent": "IOOGraphAgent",
            "source": "ioo_graph",
            "node_id": str(node["id"]),
            "node_type": node.get("type"),
            "domain": node.get("domain"),
            "tags": node.get("tags") or [],
        },
        "is_limited": False,
        "daily_limit": 999,
    }


async def _store_ioo_screen_spec(spec_dict: dict) -> str:
    """Persist an IOO-sourced screen spec in screen_specs and return its DB id."""
    row = await fetchrow(
        """
        INSERT INTO screen_specs (spec, agent_type, domain)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        json.dumps(spec_dict),
        "IOOGraphAgent",
        spec_dict.get("metadata", {}).get("domain"),
    )
    return str(row["id"])


async def _try_ioo_card(
    user_id: str,
    goal_id: Optional[str],
    tier: str,
    daily_limit: int,
) -> Optional[ScreenResponse]:
    """
    Attempt to build one IOO graph-sourced card.
    Returns a ScreenResponse on success, None if the graph has nothing to offer.
    """
    try:
        from ora.agents.ioo_graph_agent import get_graph_agent as _get_ioo
        _ioo = _get_ioo()
        nodes = await _ioo.recommend_next_nodes(
            user_id=str(user_id),
            goal_id=goal_id,
            limit=5,
        )
        if not nodes:
            return None

        node = random.choice(nodes)
        spec_dict = _ioo_node_to_screen_dict(node)
        db_id = await _store_ioo_screen_spec(spec_dict)
        screens_today = await increment_daily_screen_count(user_id)

        screen = ScreenSpec(
            screen_id=spec_dict["screen_id"],
            type=spec_dict["type"],
            layout=spec_dict["layout"],
            components=spec_dict["components"],
            feedback_overlay=FeedbackOverlay(**spec_dict["feedback_overlay"]),
            metadata=ScreenMetadata(**spec_dict["metadata"]),
        )
        return ScreenResponse(
            screen=screen,
            screen_spec_db_id=db_id,
            screens_today=screens_today,
            daily_limit=daily_limit,
            is_limited=(tier == "free"),
        )
    except Exception as _err:
        logger.warning(f"IOO card build failed, falling back to brain: {_err}")
        return None


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
    # Check subscription tier via PricingAgent-backed tier guard
    from api.tier_guard import check_tier_limit, get_user_tier, build_upgrade_card, get_current_usage
    from ora.agents.pricing_agent import get_pricing_agent

    tier = await get_user_tier(user_id)
    pricing_agent = get_pricing_agent()
    limits = await pricing_agent.get_tier_limits(tier)
    daily_limit = limits.get("daily_screens", 10)

    # Get current count BEFORE incrementing (brain will increment)
    current_count = await get_daily_screen_count(user_id)

    if daily_limit != -1 and current_count >= daily_limit:
        # Return Ora's warm upgrade card instead of a cold 402
        upgrade_card = await build_upgrade_card("daily_screens", daily_limit, tier)
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "daily_limit_reached",
                "screens_today": current_count,
                "daily_limit": daily_limit,
                "tier": tier,
                "upgrade_card": upgrade_card,
                "upgrade_url": "/api/payments/checkout",
            },
            headers={"X-Upgrade-URL": "/api/payments/checkout"},
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

    # ── Smart IOO routing ──────────────────────────────────────────────────
    # When the user has an active goal, 40 % of cards come from the IOO graph;
    # the remaining 60 % (and all cards for goal-less users) use the brain.
    has_goals = bool(body.goal_id) or await _get_user_has_goals(user_id)
    if has_goals and random.random() < 0.4:
        ioo_response = await _try_ioo_card(user_id, body.goal_id, tier, daily_limit)
        if ioo_response is not None:
            return ioo_response
    # ──────────────────────────────────────────────────────────────────────

    try:
        spec_dict, db_id, screens_today = await brain.get_screen(
            user_id=user_id,
            context=body.context or "",
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
        is_limited=(tier == "free"),
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
        "SELECT subscription_tier FROM users WHERE id = $1", str(user_id)
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

    # Check if user has active goals (once, shared across batch)
    batch_has_goals = await _get_user_has_goals(user_id)

    # Fetch screens sequentially to respect rate limits and user model updates
    for _ in range(count):
        try:
            # Smart IOO routing: 40 % of batch cards from graph when user has goals
            if batch_has_goals and random.random() < 0.4:
                ioo_resp = await _try_ioo_card(user_id, None, tier, daily_limit)
                if ioo_resp is not None:
                    results.append(ioo_resp)
                    continue

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
            str(user_id),
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
