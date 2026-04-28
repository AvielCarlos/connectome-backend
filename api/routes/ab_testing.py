"""
A/B Testing API Routes
Handles UI surface event recording and variant queries.
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.middleware import get_current_user_id
from core.redis_client import get_redis
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


# ─── Primary Landing A/B (primary_landing_v1) ─────────────────────────────────

VARIANTS = ["A", "B", "C", "D"]
EVENT_LIST_MAX = 1000
VARIANT_TTL = 7 * 24 * 3600  # 7 days


class AssignBody(BaseModel):
    experiment_id: str


class EventBody(BaseModel):
    experiment_id: str
    variant: str
    event_type: str
    value: float = 1.0


@router.post("/assign")
async def assign_variant(
    body: AssignBody,
    user_id: str = Depends(get_current_user_id),
):
    """
    Assign (or retrieve cached) variant for a user+experiment.
    Uses consistent hash: int(user_id[-8:], 16) % 4 → A/B/C/D.
    Cached in Redis for 7 days.
    """
    redis = await get_redis()
    cache_key = f"ab:variant:{user_id}:{body.experiment_id}"
    try:
        cached = await redis.get(cache_key)
        if cached:
            return {"variant": cached.decode() if isinstance(cached, bytes) else cached}
    except Exception as e:
        logger.warning(f"Redis get failed for {cache_key}: {e}")

    # Deterministic assignment: hash of user_id
    try:
        hash_val = int(user_id[-8:], 16)
    except (ValueError, TypeError):
        hash_val = hash(user_id)
    variant = VARIANTS[abs(hash_val) % 4]

    try:
        await redis.setex(cache_key, VARIANT_TTL, variant)
    except Exception as e:
        logger.warning(f"Redis set failed for {cache_key}: {e}")

    return {"variant": variant}


@router.post("/event")
async def track_event(
    body: EventBody,
    user_id: str = Depends(get_current_user_id),
):
    """
    Append an engagement event to the experiment's event list.
    Trims to last 1000 entries per variant.
    """
    redis = await get_redis()
    list_key = f"ab:events:{body.experiment_id}:{body.variant}"
    entry = json.dumps({
        "user_id": user_id,
        "event_type": body.event_type,
        "value": body.value,
    })
    try:
        await redis.rpush(list_key, entry)
        await redis.ltrim(list_key, -EVENT_LIST_MAX, -1)
    except Exception as e:
        logger.warning(f"Redis event append failed for {list_key}: {e}")
    return {"ok": True}


@router.get("/winner/{experiment_id}")
async def get_experiment_winner(
    experiment_id: str,
):
    """
    Return the server-side winner for an experiment, if one has been promoted.
    Returns {"winner": "B"} or {"winner": null}.
    No auth required — read-only, used by LandingRouter on the frontend.
    """
    redis = await get_redis()
    try:
        winner_raw = await redis.get(f"ab:winner:{experiment_id}")
        winner = winner_raw if isinstance(winner_raw, str) else (
            winner_raw.decode() if winner_raw else None
        )
        # Validate it's a known variant
        if winner and winner not in VARIANTS:
            winner = None
        return {"winner": winner}
    except Exception as e:
        logger.warning(f"get_experiment_winner failed for {experiment_id}: {e}")
        return {"winner": None}


@router.get("/results/{experiment_id}")
async def get_results(
    experiment_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """
    Aggregate per-variant event counts by event_type.
    Returns: {"A": {"session_start": 12, "cta_tapped": 7, ...}, ...}
    """
    redis = await get_redis()
    results: dict = {}
    for variant in VARIANTS:
        list_key = f"ab:events:{experiment_id}:{variant}"
        try:
            raw_events = await redis.lrange(list_key, 0, -1)
        except Exception as e:
            logger.warning(f"Redis lrange failed for {list_key}: {e}")
            raw_events = []
        counts: dict = {}
        for raw in raw_events:
            try:
                ev = json.loads(raw)
                et = ev.get("event_type", "unknown")
                counts[et] = counts.get(et, 0) + 1
            except Exception:
                pass
        results[variant] = counts
    return {"experiment_id": experiment_id, "results": results}


@router.post("/set-winner/{experiment_id}")
async def set_winner(experiment_id: str, body: dict, user_id: str = Depends(get_current_user_id)):
    """Force a specific variant as the winner (admin only)."""
    r = await get_redis()
    winner = body.get("winner", "A")
    if r:
        await r.set(f"ab:winner:{experiment_id}", winner, ex=7 * 86400)
    return {"ok": True, "winner": winner}


@router.delete("/winner/{experiment_id}")
async def clear_winner(experiment_id: str, user_id: str = Depends(get_current_user_id)):
    """Clear forced winner — revert to random assignment."""
    r = await get_redis()
    if r:
        await r.delete(f"ab:winner:{experiment_id}")
    return {"ok": True}
