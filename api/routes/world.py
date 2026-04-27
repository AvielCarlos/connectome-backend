"""
World API Routes

GET  /api/world/signals    — current world signals (public, cached hourly)
POST /api/world/discovery  — generate a world-aware card on demand
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ora.agents.world_signal_agent import get_world_signal_agent
from ora.agents.world_discovery_agent import WorldDiscoveryAgent
from core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/world", tags=["world"])


# ---------------------------------------------------------------------------
# GET /api/world/signals
# ---------------------------------------------------------------------------

@router.get("/signals")
async def get_world_signals(
    city: str = "Vancouver",
    request: Request = None,
):
    """
    Return current world signals — weather, time, season, moon phase,
    historical events, trending topics, and serendipity seed.
    Cached hourly in Redis. Public endpoint — no auth required.
    """
    # Resolve city from geo header if not provided
    if city == "Vancouver" and request is not None:
        forwarded = request.headers.get("X-Forwarded-For", "")
        ip = forwarded.split(",")[0].strip() if forwarded else ""
        if ip:
            try:
                from core.geo import get_location_for_ip
                geo = await get_location_for_ip(ip)
                if geo and geo.get("city"):
                    city = geo["city"]
            except Exception:
                pass

    try:
        agent = get_world_signal_agent()
        signals = await agent.get_signals(city=city)

        # Build clean public response (omit internal fields)
        weather = signals.get("weather", {})
        on_this_day_events = signals.get("on_this_day", [])
        on_this_day_text = None
        if on_this_day_events:
            evt = on_this_day_events[0]
            on_this_day_text = f"In {evt['year']}, {evt['text']}"

        trending = signals.get("trending_hn", [])
        trending_topic = trending[0] if trending else None

        return {
            "weather": {
                "condition": weather.get("condition"),
                "condition_raw": weather.get("condition_raw"),
                "temp_c": weather.get("temp_c"),
                "feels_like_c": weather.get("feels_like_c"),
                "city": weather.get("city", city),
            },
            "time_of_day": signals.get("time_of_day"),
            "hour": signals.get("hour"),
            "day_of_week": signals.get("day_of_week"),
            "season": signals.get("season"),
            "moon_phase": signals.get("moon_phase"),
            "moon_emoji": signals.get("moon_emoji"),
            "on_this_day": on_this_day_text,
            "on_this_day_events": on_this_day_events,
            "trending_topic": trending_topic,
            "trending_hn": trending,
            "serendipity_seed": signals.get("serendipity_seed"),
            "generated_at": signals.get("generated_at"),
        }
    except Exception as e:
        logger.error(f"World signals error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not fetch world signals")


# ---------------------------------------------------------------------------
# POST /api/world/discovery
# ---------------------------------------------------------------------------

class DiscoveryRequest(BaseModel):
    context: Optional[str] = None
    mood: Optional[str] = None       # e.g., "reflective", "energetic", "curious"
    location_hint: Optional[str] = None
    card_type: Optional[str] = None  # force a specific card type
    count: int = 1                   # number of cards (1-5)


@router.post("/discovery")
async def generate_world_discovery(body: DiscoveryRequest):
    """
    Generate world-aware life suggestion card(s) on demand.

    Returns a ScreenSpec (or list of ScreenSpecs if count > 1) driven by
    live world signals: weather, time, season, moon phase, history, trending.

    Optional body fields:
      - context      : user context string (goals, mood notes)
      - mood         : current mood hint ("reflective", "energetic", etc.)
      - location_hint: override city for weather signal
      - card_type    : force a specific type ("right_now", "on_this_day", "try_something",
                       "world_pulse", "seasonal")
      - count        : number of diverse cards (1-5, default 1)
    """
    count = max(1, min(body.count, 5))
    city = body.location_hint or "Vancouver"

    # Build a minimal user_context
    user_context: Dict[str, Any] = {
        "user_id": "anonymous",
        "user_city": city,
        "active_goals": [],
        "interests": [],
        "domain": "iVive",
    }
    if body.mood:
        user_context["session_mood_label"] = body.mood
    if body.context:
        user_context["context_note"] = body.context

    try:
        # Initialize OpenAI if available
        openai_client = None
        if settings.has_openai:
            try:
                from openai import AsyncOpenAI
                openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
            except Exception:
                pass

        agent = WorldDiscoveryAgent(openai_client=openai_client)

        if count == 1:
            card = await agent.generate_screen(
                user_context=user_context,
                force_card_type=body.card_type,
            )
            return card
        else:
            cards = await agent.generate_card_batch(
                user_context=user_context,
                count=count,
            )
            return {"cards": cards, "count": len(cards)}

    except Exception as e:
        logger.error(f"World discovery error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not generate discovery card")
