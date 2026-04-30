"""
Screens API Routes

Endpoints:
  POST /api/screens/next    — single next screen (original)
  POST /api/screens/batch   — prefetch multiple screens (new)
  POST /api/screens/save    — bookmark a screen for later (new)
  GET  /api/screens/:id     — retrieve a screen by ID
"""

import asyncio
import hashlib
import json
import logging
import random
import uuid as _uuid_mod
from datetime import datetime, timezone
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
    metadata = spec_dict.get("metadata", {}) or {}
    node_id = metadata.get("node_id")
    screen_role = metadata.get("screen_role") or "recommend"
    node_uuid = None
    if node_id:
        try:
            node_uuid = UUID(str(node_id))
        except ValueError:
            node_uuid = None

    row = await fetchrow(
        """
        INSERT INTO screen_specs (spec, agent_type, domain, ioo_node_id, screen_role)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        json.dumps(spec_dict),
        "IOOGraphAgent",
        metadata.get("domain"),
        node_uuid,
        screen_role,
    )
    db_id = str(row["id"])

    # TODO(ScreenGraph): generated IOO screens are pathway nodes, not throwaway
    # UI. This initial edge attaches the screen to its source IOO node; future
    # generation should also create leads_to/requires/clarifies/executes edges
    # between neighbouring generated screens and execution runs.
    if node_uuid:
        try:
            from ora.agents.screen_graph_agent import create_screen_edge

            await create_screen_edge(
                from_screen_id=db_id,
                relation_type="belongs_to_ioo_node",
                ioo_node_id=node_uuid,
                evidence={"source": "screens._store_ioo_screen_spec", "screen_role": screen_role},
            )
        except Exception as err:
            logger.debug("Screen graph edge creation skipped for screen %s: %s", db_id[:8], err)

    return db_id


_STATIC_FALLBACK_CARDS = [
    {
        "title": "Take a 10-minute Aventi walk",
        "body": "A tiny real-world exploration: step outside, choose one direction, and let curiosity pick the next turn. Notice one place, person, event poster, cafe, path, or view you could return to later.",
        "domain": "Aventi",
        "tag": "exploration",
        "button": "Start the walk →",
        "why": "Aventi grows aliveness through lived experience. This is deliberately small so it works even when motivation is low.",
        "steps": [
            "Check your energy and weather; keep it easy.",
            "Walk for 10 minutes with no productivity goal.",
            "Save one thing you noticed as a possible future experience.",
        ],
        "needs": ["10 minutes", "safe place to walk", "curiosity"],
        "result": "Ora learns what kinds of places and experiences make you feel more alive.",
    },
    {
        "title": "Reset your system with iVive",
        "body": "A nervous-system reset for when your body is carrying friction. Drink water, loosen your jaw, drop your shoulders, and take five slow breaths before choosing the next task.",
        "domain": "iVive",
        "tag": "vitality",
        "button": "Do the reset →",
        "why": "iVive is about maintaining and growing the self. A regulated body makes better decisions and lowers the cost of starting.",
        "steps": [
            "Drink water or take one sip if that is all you can do.",
            "Relax jaw, shoulders, hands, and belly.",
            "Take five slow breaths, then ask: what is the smallest next step?",
        ],
        "needs": ["1–3 minutes", "water if available", "somewhere to pause"],
        "result": "Ora learns whether you need restoration before action.",
    },
    {
        "title": "Send one Eviva spark",
        "body": "A contribution-through-connection action: send one person a real appreciation, useful intro, helpful link, or warm check-in. Keep it one sentence and make it genuine.",
        "domain": "Eviva",
        "tag": "connection",
        "button": "Send appreciation →",
        "why": "Eviva grows contribution, relationships, and service. Small signals of care can reopen human pathways without needing a big project.",
        "steps": [
            "Choose one person who would genuinely benefit from warmth or recognition.",
            "Write one specific sentence — no performance, no ask.",
            "Send it, or save it if now is not socially appropriate.",
        ],
        "needs": ["one person", "one sentence", "phone or messaging app"],
        "result": "Ora learns which relationships and contribution channels are alive for you.",
    },
    {
        "title": "Choose Rest on purpose",
        "body": "A deliberate recovery micro-step: put your phone down for three minutes and let your nervous system learn that nothing needs to be chased right now.",
        "domain": "Rest",
        "tag": "recovery",
        "button": "Begin rest →",
        "why": "Rest is the substrate under iVive, Eviva, and Aventi. Sometimes the best next action is reducing load before adding direction.",
        "steps": [
            "Put the phone face down or away from your hand.",
            "Let your eyes rest on one still object.",
            "After three minutes, choose whether to continue resting or return to action.",
        ],
        "needs": ["3 minutes", "quiet enough space", "permission to pause"],
        "result": "Ora learns when your next best step is recovery, not more input.",
    },
]


async def _static_fallback_card(
    user_id: str,
    tier: str,
    daily_limit: int,
    reason: str,
) -> ScreenResponse:
    """Build a deterministic local card so the main feed never appears empty."""
    # Deterministic across process restarts so repeated failures do not create
    # a chaotic feed. These are seed cards, not the long-term feed model: IOO
    # graph generation should remain the primary path and learn from responses.
    digest = hashlib.sha256(f"{user_id}:{datetime.now(timezone.utc).date()}".encode("utf-8")).hexdigest()
    idx = int(digest[:8], 16) % len(_STATIC_FALLBACK_CARDS)
    item = _STATIC_FALLBACK_CARDS[idx]
    screen_id = str(_uuid_mod.uuid4())
    spec_dict = {
        "screen_id": screen_id,
        "type": "activity",
        "layout": "card_stack",
        "components": [
            {"type": "category_badge", "text": item["domain"].upper(), "color": "#00d4aa"},
            {"type": "headline", "text": item["title"]},
            {"type": "body", "text": item["body"]},
            {"type": "section_header", "text": "What this is"},
            {"type": "body_text", "text": item["why"]},
            {"type": "section_header", "text": "Needs"},
            {"type": "body_text", "text": ", ".join(item["needs"])},
            {
                "type": "action_button",
                "label": item["button"],
                "action": {
                    "type": "open_url",
                    "url": f"ido://fallback/{item['tag']}",
                    "payload": {"tag": item["tag"], "source": "static_fallback"},
                },
            },
        ],
        "feedback_overlay": {"type": "star_rating", "position": "bottom_right", "always_visible": True},
            "metadata": {
            "agent": "StaticFallbackAgent",
            "source": "static_fallback",
            "domain": item["domain"],
            "tags": [item["tag"], "mvp_stability"],
            "fallback_reason": reason,
            "ioo_execution_status": "pending_user_response",
            "ioo_learning_event": "fallback_card_shown",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "card_data": {
            "title": item["title"],
            "body": item["body"],
            "deep_dive": {
                "time_to_start": item["needs"][0],
                "difficulty": "easy",
                "why_it_matters": item["why"],
                "steps": item["steps"],
                "resources": [],
                "stat": item["result"],
            },
        },
        "deep_dive": {
            "time_to_start": item["needs"][0],
            "difficulty": "easy",
            "why_it_matters": item["why"],
            "steps": item["steps"],
            "resources": [],
            "stat": item["result"],
        },
    }

    # TODO(IOO): when the user chooses "do now", trigger the IOO Execution
    # Protocol for this fallback action. "do later" should schedule/resurface,
    # and "not interested" should become a graph-learning signal/refinement.
    # TODO(ScreenGraph): once fallback cards are mapped to IOO candidate nodes,
    # create screen_graph_edges here too so Do Now / Do Later / Not Interested
    # can adjust user-specific pathway weights instead of only aggregate ratings.

    try:
        db_id = await _store_ioo_screen_spec(spec_dict)
    except Exception as err:
        db_id = screen_id
        logger.error("Static fallback card persistence failed for user %s: %s", user_id[:8], err, exc_info=True)

    try:
        screens_today = await increment_daily_screen_count(user_id)
    except Exception as err:
        screens_today = await get_daily_screen_count(user_id)
        logger.warning("Static fallback count increment failed for user %s: %s", user_id[:8], err)

    logger.warning("Using static fallback screen for user %s: %s", user_id[:8], reason)

    return ScreenResponse(
        screen=ScreenSpec(
            screen_id=spec_dict["screen_id"],
            type=spec_dict["type"],
            layout=spec_dict["layout"],
            components=spec_dict["components"],
            feedback_overlay=FeedbackOverlay(**spec_dict["feedback_overlay"]),
            metadata=ScreenMetadata(**spec_dict["metadata"]),
        ),
        screen_spec_db_id=db_id,
        screens_today=screens_today,
        daily_limit=daily_limit,
        is_limited=False,
    )


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
        if goal_id:
            # Goal-specific: recommend nodes aligned to that goal
            nodes = await _ioo.recommend_next_nodes(
                user_id=str(user_id),
                goal_id=goal_id,
                limit=8,
            )
        else:
            # Discovery mode: surface diverse nodes from across all domains
            # Mix: 60% personalised (vector recommend) + 40% random exploration
            if random.random() < 0.6:
                try:
                    nodes = await _ioo.vector_recommend(
                        user_id=str(user_id),
                        goal_context="life improvement discovery exploration",
                        limit=10,
                        preference="mixed",
                    )
                except Exception:
                    nodes = await _ioo.recommend_next_nodes(user_id=str(user_id), goal_id=None, limit=8)
            else:
                # Pure exploration: pick from any domain the user hasn't seen recently
                nodes = await _ioo.recommend_next_nodes(user_id=str(user_id), goal_id=None, limit=8)

        if not nodes:
            return None

        # Semi-random selection with slight weighting toward higher-difficulty nodes
        # (easier nodes are boring; discovery should stretch people slightly)
        weights = [1 + (n.get('difficulty_level', 5) * 0.1) for n in nodes]
        total = sum(weights)
        r = random.random() * total
        cumulative = 0
        node = nodes[-1]
        for n, w in zip(nodes, weights):
            cumulative += w
            if r <= cumulative:
                node = n
                break
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
    MVP/stability: preserves auth but does not hard-block the main feed on
    daily limits. Paywall enforcement can return after feed stability is proven.
    """
    # Check subscription tier via PricingAgent-backed tier guard
    from api.tier_guard import check_tier_limit, get_user_tier, build_upgrade_card, get_current_usage
    from ora.agents.pricing_agent import get_pricing_agent

    tier = await get_user_tier(user_id)
    pricing_agent = get_pricing_agent()
    limits = await pricing_agent.get_tier_limits(tier)
    configured_daily_limit = limits.get("daily_screens", 10)
    daily_limit = configured_daily_limit if configured_daily_limit == -1 else max(configured_daily_limit, 999)

    # Get current count BEFORE incrementing (brain will increment)
    current_count = await get_daily_screen_count(user_id)

    if False and daily_limit != -1 and current_count >= daily_limit:
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
    # Feed is primarily IOO discovery — ~75% IOO nodes, ~25% brain coaching content
    # No goals gate: discovery mode surfaces nodes across all domains, not just goal-aligned ones
    if random.random() < 0.75:
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
        logger.error(f"Brain error for user {user_id[:8]}, using static fallback: {e}", exc_info=True)
        return await _static_fallback_card(user_id, tier, daily_limit, f"brain_exception:{type(e).__name__}")

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
        logger.error(f"Spec parse error, using static fallback: {e}", exc_info=True)
        return await _static_fallback_card(user_id, tier, daily_limit, f"spec_parse_error:{type(e).__name__}")

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
    goal_id: Optional[str] = None
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
    daily_limit = max(settings.FREE_TIER_DAILY_SCREENS, 999)

    if is_free:
        # MVP/stability: keep authentication, but do not hard-block the primary
        # feed on daily limits. A broken empty feed is worse than over-serving
        # during pre-product-stability.
        current_count = await get_daily_screen_count(user_id)
        if current_count >= settings.FREE_TIER_DAILY_SCREENS:
            logger.info(
                "Free user %s exceeded configured daily screen limit (%s/%s); serving feed for MVP stability",
                user_id[:8],
                current_count,
                settings.FREE_TIER_DAILY_SCREENS,
            )

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
    # Fetch screens sequentially to respect rate limits and user model updates
    for _ in range(count):
        try:
            # Feed is primarily IOO discovery — ~75% IOO nodes, ~25% brain coaching content
            # No goals gate: anyone can discover IOO nodes regardless of whether they have active goals
            if random.random() < 0.75:
                ioo_resp = await _try_ioo_card(user_id, body.goal_id, tier, daily_limit)
                if ioo_resp is not None:
                    results.append(ioo_resp)
                    continue

            spec_dict, db_id, screens_today = await brain.get_screen(
                user_id=user_id,
                context=None,
                goal_id=body.goal_id,
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
            logger.error(f"Batch screen error for user {user_id[:8]}, using fallback if needed: {e}", exc_info=True)
            if not results:
                results.append(await _static_fallback_card(user_id, tier, daily_limit, f"batch_exception:{type(e).__name__}"))
            break

    if not results:
        results.append(await _static_fallback_card(user_id, tier, daily_limit, "batch_empty"))

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
    # TODO(ScreenGraph): treat this as the explicit "Do Later" graph-learning
    # signal. Increase the relevant user-specific screen_graph_edges weight,
    # connect this screen to its resurfacing/reminder screen, and avoid treating
    # a save as either completion or rejection.
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
