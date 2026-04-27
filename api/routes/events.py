"""
Events API Routes
=================

GET  /api/events                      — upcoming events for a city (7-day window, max 14)
GET  /api/events/recommended          — personalized events for a user
POST /api/events/sync                 — trigger a city sync (admin / cron)
POST /api/users/location              — update user city + coords + event preferences

Conveyor belt model:
  • DB stores up to 14 days of events (pipeline)
  • Endpoints serve the next 7 days by default (serving window)
  • `days_ahead` is capped at 14
  • starts_at > NOW() always — never return past events
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.middleware import get_current_user_id
from core.database import execute, fetch, fetchrow
from ora.agents.event_agent import EventAgent, SERVE_WINDOW_DAYS, MAX_SERVE_WINDOW_DAYS

logger = logging.getLogger(__name__)
router = APIRouter(tags=["events"])


# ── Response models ────────────────────────────────────────────────────────────

class EventCard(BaseModel):
    id: int
    external_id: Optional[str] = None
    title: str
    description: Optional[str] = None
    category: Optional[str] = None
    venue_name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    url: Optional[str] = None
    image_url: Optional[str] = None
    price_range: Optional[str] = None
    source: Optional[str] = None
    relevance_tags: Optional[List[str]] = None


class EventListResponse(BaseModel):
    events: List[EventCard]
    count: int
    city: str
    days_ahead: int
    window_start: datetime
    window_end: datetime


class SyncResponse(BaseModel):
    city: str
    total_fetched: int
    inserted: int
    pruned: int
    sources: dict
    synced_at: str


class LocationUpdate(BaseModel):
    city: str = Field(..., min_length=1, max_length=100, description="City name e.g. 'Vancouver'")
    location_lat: Optional[float] = Field(None, ge=-90, le=90)
    location_lng: Optional[float] = Field(None, ge=-180, le=180)
    event_preferences: Optional[List[str]] = Field(
        None,
        description="Preferred event categories e.g. ['wellness', 'tech', 'music']"
    )


# ── GET /api/events ────────────────────────────────────────────────────────────

@router.get("/api/events", response_model=EventListResponse)
async def list_events(
    city: str = Query("Vancouver", description="City to fetch events for"),
    days_ahead: int = Query(
        SERVE_WINDOW_DAYS,
        ge=1,
        le=MAX_SERVE_WINDOW_DAYS,
        description=f"Days ahead to show (default {SERVE_WINDOW_DAYS}, max {MAX_SERVE_WINDOW_DAYS})",
    ),
    category: Optional[str] = Query(None, description="Filter by category"),
    limit: int = Query(50, ge=1, le=200),
):
    """
    Return upcoming events for a city.

    Serving window: NOW() → NOW() + days_ahead (default 7, max 14).
    Events are always filtered to starts_at > NOW() — no past events.
    Results are sorted soonest first.
    """
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    until = now + timedelta(days=days_ahead)

    try:
        agent = EventAgent()
        events = await agent.get_events_for_city(
            city=city,
            days_ahead=days_ahead,
            category=category,
            limit=limit,
        )
    except Exception as e:
        logger.error(f"GET /api/events error for {city}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not fetch events")

    return EventListResponse(
        events=[EventCard(**ev) for ev in events],
        count=len(events),
        city=city,
        days_ahead=days_ahead,
        window_start=now,
        window_end=until,
    )


# ── GET /api/events/recommended ───────────────────────────────────────────────

@router.get("/api/events/recommended", response_model=EventListResponse)
async def recommended_events(
    days_ahead: int = Query(
        SERVE_WINDOW_DAYS,
        ge=1,
        le=MAX_SERVE_WINDOW_DAYS,
    ),
    limit: int = Query(10, ge=1, le=50),
    user_id: str = Depends(get_current_user_id),
):
    """
    Personalized event recommendations for the authenticated user.

    Ranks by:
      1. Semantic similarity between user embedding and event embeddings
      2. Category match against user.event_preferences
      3. Goal alignment (via relevance_tags)
      4. Soonest first

    Requires user to have `city` set via POST /api/users/location.
    """
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    until = now + timedelta(days=days_ahead)

    user = await fetchrow(
        "SELECT city FROM users WHERE id = $1", UUID(user_id)
    )
    if not user or not user.get("city"):
        raise HTTPException(
            status_code=400,
            detail="Set your city first via POST /api/users/location",
        )

    city = user["city"]

    try:
        agent = EventAgent()
        events = await agent.get_recommended_events(
            user_id=user_id,
            days_ahead=days_ahead,
            limit=limit,
        )
    except Exception as e:
        logger.error(f"GET /api/events/recommended error for user {user_id[:8]}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not fetch recommended events")

    return EventListResponse(
        events=[EventCard(**ev) for ev in events],
        count=len(events),
        city=city,
        days_ahead=days_ahead,
        window_start=now,
        window_end=until,
    )


# ── POST /api/events/sync ──────────────────────────────────────────────────────

@router.post("/api/events/sync", response_model=SyncResponse)
async def sync_events(
    city: str = Query("Vancouver", description="City to sync events for"),
    force: bool = Query(False, description="Force re-fetch even if cached"),
):
    """
    Trigger a fresh event fetch for a city.

    Fetches from SerpAPI (if SERPAPI_KEY set), Eventbrite (if EVENTBRITE_TOKEN set),
    and Meetup.com scraper. Deduplicates and upserts into DB.

    The fetch horizon is 14 days (pipeline depth).
    Cache TTL is 12 hours — use force=true to bypass.

    Intended for:
      • Manual triggering / admin
      • Railway cron job (daily sync to keep the conveyor belt stocked)
      • Mobile app "refresh" actions
    """
    try:
        from core.config import settings
        from openai import AsyncOpenAI

        openai_client = None
        if settings.has_openai:
            try:
                openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
            except Exception:
                pass

        agent = EventAgent(openai_client=openai_client)
        result = await agent.sync_city(city=city, force=force)

    except Exception as e:
        logger.error(f"POST /api/events/sync error for {city}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Sync failed: {e}")

    return SyncResponse(**result)


# ── POST /api/users/location ───────────────────────────────────────────────────

@router.post("/api/users/location")
async def update_user_location(
    body: LocationUpdate,
    user_id: str = Depends(get_current_user_id),
):
    """
    Update the authenticated user's city, coordinates, and event preferences.

    After updating, automatically triggers a background event sync for the city
    if no events exist yet — so recommendations are available quickly.

    Body:
      {
        "city": "Vancouver",
        "location_lat": 49.2827,
        "location_lng": -123.1207,
        "event_preferences": ["wellness", "tech", "music"]
      }
    """
    try:
        await execute(
            """
            UPDATE users
            SET city              = $1,
                location_lat      = $2,
                location_lng      = $3,
                event_preferences = $4
            WHERE id = $5
            """,
            body.city,
            body.location_lat,
            body.location_lng,
            body.event_preferences or [],
            UUID(user_id),
        )
    except Exception as e:
        logger.error(f"POST /api/users/location error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not update location")

    # Background sync: fill the pipeline if this city has no events yet
    import asyncio
    try:
        from core.database import fetchval
        event_count = await fetchval(
            "SELECT COUNT(*) FROM events WHERE city = $1 AND starts_at > NOW()",
            body.city,
        )
        if not event_count:
            from core.config import settings
            openai_client = None
            if settings.has_openai:
                try:
                    from openai import AsyncOpenAI
                    openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
                except Exception:
                    pass
            agent = EventAgent(openai_client=openai_client)
            asyncio.create_task(agent.sync_city(body.city))
            logger.info(f"Triggered background event sync for new city: {body.city}")
    except Exception as bg_err:
        logger.debug(f"Background sync trigger failed (non-fatal): {bg_err}")

    return {
        "status": "updated",
        "city": body.city,
        "location_lat": body.location_lat,
        "location_lng": body.location_lng,
        "event_preferences": body.event_preferences or [],
        "user_id": user_id,
    }
