"""
Events API Routes
=================

GET  /api/events                      — upcoming events for a city (7-day window, max 14)
GET  /api/events/recommended          — personalized events for a user
POST /api/events/sync                 — trigger a city sync (admin / cron)
POST /api/users/location              — update user city + coords + event preferences
POST /api/users/location/live         — browser GPS → city + IOO/event location sync

Conveyor belt model:
  • DB stores up to 14 days of events (pipeline)
  • Endpoints serve the next 7 days by default (serving window)
  • `days_ahead` is capped at 14
  • starts_at > NOW() always — never return past events
"""

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.middleware import get_current_user_id
from core.database import execute, fetch, fetchrow
from aura.agents.event_agent import EventAgent, SERVE_WINDOW_DAYS, MAX_SERVE_WINDOW_DAYS

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
    country: Optional[str] = None
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
    country: Optional[str] = Field(None, min_length=1, max_length=100)
    location_lat: Optional[float] = Field(None, ge=-90, le=90)
    location_lng: Optional[float] = Field(None, ge=-180, le=180)
    event_preferences: Optional[List[str]] = Field(
        None,
        description="Preferred event categories e.g. ['wellness', 'tech', 'music']"
    )


class LiveLocationUpdate(BaseModel):
    location_lat: float = Field(..., ge=-90, le=90)
    location_lng: float = Field(..., ge=-180, le=180)
    accuracy_m: Optional[float] = Field(None, ge=0)
    city: Optional[str] = Field(None, min_length=1, max_length=100)
    country: Optional[str] = Field(None, min_length=1, max_length=100)
    event_preferences: Optional[List[str]] = None


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
        city=f"{city}, {country}" if country else city,
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
        """
        SELECT COALESCE(NULLIF(s.location_city, ''), NULLIF(u.city, '')) AS city,
               NULLIF(s.location_country, '') AS country,
               u.location_lat, u.location_lng
        FROM users u
        LEFT JOIN ioo_user_state s ON s.user_id = u.id
        WHERE u.id = $1
        """,
        UUID(user_id),
    )
    if not user or not user.get("city") or not user.get("country"):
        raise HTTPException(
            status_code=400,
            detail="Set your city/country first via live location or Travel Mode",
        )

    city = user["city"]
    country = user.get("country")

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
        city=f"{city}, {country}" if country else city,
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
    country = (body.country or "").strip()
    if (not country) and body.location_lat is not None and body.location_lng is not None:
        try:
            from core.geo import reverse_geocode_lat_lng
            geo = await reverse_geocode_lat_lng(body.location_lat, body.location_lng)
            country = (geo or {}).get("country") or ""
        except Exception:
            country = ""

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
        await execute(
            """
            INSERT INTO ioo_user_state (user_id, location_city, location_country, state_json, last_updated)
            VALUES ($1, $2, $3, $4::jsonb, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                location_city = EXCLUDED.location_city,
                location_country = COALESCE(EXCLUDED.location_country, ioo_user_state.location_country),
                state_json = COALESCE(ioo_user_state.state_json, '{}'::jsonb) || EXCLUDED.state_json,
                last_updated = NOW()
            """,
            str(user_id),
            body.city,
            country or None,
            json.dumps({
                "location_lat": body.location_lat,
                "location_lng": body.location_lng,
                "geo_source": "manual_or_travel_location",
            }),
        )
        try:
            from aura.user_model import update_user_embedding_from_context
            location_context = {
                "location_city": body.city,
                "location_country": country or None,
                "location_lat": body.location_lat,
                "location_lng": body.location_lng,
                "location_source": "manual_or_travel_location",
            }
            await update_user_embedding_from_context(str(user_id), location_context, "now")
            await update_user_embedding_from_context(str(user_id), location_context, "later")
        except Exception as vec_e:
            logger.debug(f"Manual/travel location user vector refresh skipped for {user_id[:8]}: {vec_e}")
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
        "country": country or None,
        "location_lat": body.location_lat,
        "location_lng": body.location_lng,
        "event_preferences": body.event_preferences or [],
        "user_id": user_id,
    }


@router.post("/api/users/location/live")
async def update_user_live_location(
    body: LiveLocationUpdate,
    user_id: str = Depends(get_current_user_id),
):
    """
    Activate browser GPS location for both local events and IOO graph filtering.

    The web app sends user-approved coordinates. Backend resolves city/country
    when not supplied, updates `users` for event recommendations, updates
    `ioo_user_state` for location-aware graph recommendations, and triggers a
    local event sync if needed.
    """
    geo = None
    city = (body.city or "").strip()
    country = (body.country or "").strip()
    if not city or not country:
        try:
            from core.geo import reverse_geocode_lat_lng
            geo = await reverse_geocode_lat_lng(body.location_lat, body.location_lng)
            city = city or (geo or {}).get("city") or ""
            country = country or (geo or {}).get("country") or ""
        except Exception as e:
            logger.debug(f"Live location reverse geocode skipped for user {user_id[:8]}: {e}")

    if not city:
        # Keep the coordinates useful even when reverse geocoding is unavailable.
        city = f"Near {body.location_lat:.3f},{body.location_lng:.3f}"

    prefs = body.event_preferences or ["wellness", "music", "arts", "community", "sports", "tech"]

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
            city,
            body.location_lat,
            body.location_lng,
            prefs,
            UUID(user_id),
        )
        await execute(
            """
            INSERT INTO ioo_user_state (user_id, location_city, location_country, state_json, last_updated)
            VALUES ($1, $2, $3, $4::jsonb, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                location_city = EXCLUDED.location_city,
                location_country = COALESCE(EXCLUDED.location_country, ioo_user_state.location_country),
                state_json = COALESCE(ioo_user_state.state_json, '{}'::jsonb) || EXCLUDED.state_json,
                last_updated = NOW()
            """,
            str(user_id),
            city,
            country or None,
            __import__("json").dumps({
                "live_location_enabled": True,
                "location_lat": body.location_lat,
                "location_lng": body.location_lng,
                "accuracy_m": body.accuracy_m,
                "geo_source": "browser_gps",
            }),
        )
        try:
            from aura.agents.ioo_graph_agent import get_graph_agent
            await get_graph_agent().build_user_ioo_vector(str(user_id))
        except Exception as e:
            logger.debug(f"Live location IOO vector refresh skipped for {user_id[:8]}: {e}")
        try:
            from aura.user_model import update_user_embedding_from_context
            location_context = {
                "location_city": city,
                "location_country": country or None,
                "location_lat": body.location_lat,
                "location_lng": body.location_lng,
                "location_source": "browser_gps",
            }
            await update_user_embedding_from_context(str(user_id), location_context, "now")
            await update_user_embedding_from_context(str(user_id), location_context, "later")
        except Exception as e:
            logger.debug(f"Live location user vector refresh skipped for {user_id[:8]}: {e}")
    except Exception as e:
        logger.error(f"POST /api/users/location/live error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not update live location")

    import asyncio
    try:
        from core.database import fetchval
        event_count = await fetchval(
            "SELECT COUNT(*) FROM events WHERE city = $1 AND starts_at > NOW()",
            city,
        )
        if not event_count and not city.startswith("Near "):
            from core.config import settings
            openai_client = None
            if settings.has_openai:
                try:
                    from openai import AsyncOpenAI
                    openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
                except Exception:
                    pass
            asyncio.create_task(EventAgent(openai_client=openai_client).sync_city(city))
            logger.info(f"Triggered live-location event sync for city: {city}")
    except Exception as bg_err:
        logger.debug(f"Live location background event sync skipped: {bg_err}")

    return {
        "status": "updated",
        "city": city,
        "country": country or None,
        "location_lat": body.location_lat,
        "location_lng": body.location_lng,
        "accuracy_m": body.accuracy_m,
        "ioo_location_active": True,
        "events_location_active": True,
        "reverse_geocoded": bool(geo),
        "user_id": user_id,
    }
