"""
Explore API Routes

Endpoints:
  POST /api/explore/cards       — generates explore cards for the user
  GET  /api/explore/categories  — returns available categories with emoji and description
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from api.middleware import get_current_user_id
from ora.brain import get_brain
from ora.user_model import load_user_model
from core.geo import get_location_for_ip, geo_to_context_hints

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/explore", tags=["explore"])


class ExploreCardsRequest(BaseModel):
    category: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    explore_depth: str = "surface"  # surface | deep | serendipitous


@router.post("/cards")
async def get_explore_cards(
    body: ExploreCardsRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> List[Dict[str, Any]]:
    """
    Generate explore cards tailored to the user's goals, location, and interests.
    """
    brain = get_brain()

    # Load user context
    user_model = await load_user_model(user_id)
    user_context: Dict[str, Any] = {}
    if user_model:
        user_context = user_model.to_context_dict()

    # Enrich with geo if not provided
    lat = body.lat
    lon = body.lon
    if not lat or not lon:
        try:
            forwarded = request.headers.get("X-Forwarded-For", "")
            ip = forwarded.split(",")[0].strip() if forwarded else (
                request.client.host if request.client else ""
            )
            geo = await get_location_for_ip(ip)
            if geo:
                hints = geo_to_context_hints(geo)
                user_context.update(hints)
                lat = geo.get("lat")
                lon = geo.get("lon")
        except Exception as e:
            logger.debug(f"Geo enrichment failed: {e}")

    # Validate depth
    depth = body.explore_depth if body.explore_depth in ("surface", "deep", "serendipitous") else "surface"

    try:
        cards = await brain.explore.generate_cards(
            user_context=user_context,
            category=body.category,
            lat=lat,
            lon=lon,
            explore_depth=depth,
        )
        return cards
    except Exception as e:
        logger.error(f"ExploreAgent failed: {e}")
        return []


@router.get("/categories")
async def get_explore_categories(
    user_id: str = Depends(get_current_user_id),
) -> List[Dict[str, Any]]:
    """Return all available explore categories with emoji and description."""
    brain = get_brain()
    return await brain.explore.get_categories()
