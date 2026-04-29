"""System routes for AIOS-wide runtime state."""

import json
from typing import Any

from fastapi import APIRouter

from core.database import fetchrow

router = APIRouter(prefix="/api/system", tags=["system"])

def _jsonish(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value


DEFAULT_AI_OS_STATE = {
    "id": None,
    "ruling_goals": [],
    "featured_apps": ["iDo", "Aventi", "iVive", "Eviva"],
    "ora_mission_statement": "Ora is evolving into an AI OS for human fulfilment — guiding vitality, adventure, contribution, and discovery.",
    "evolution_notes": "No AIOS evolution run has completed yet; using default launcher order.",
    "user_count": 0,
    "computed_at": None,
}


@router.get("/aios-state")
async def get_aios_state():
    """Return the latest collective AIOS state for the frontend launcher."""
    row = await fetchrow(
        """
        SELECT id, ruling_goals, featured_apps, ora_mission_statement,
               evolution_notes, user_count, computed_at
        FROM aios_state
        ORDER BY computed_at DESC
        LIMIT 1
        """
    )
    if not row:
        return DEFAULT_AI_OS_STATE

    data = dict(row)
    data["ruling_goals"] = _jsonish(data.get("ruling_goals"), [])
    data["featured_apps"] = _jsonish(data.get("featured_apps"), DEFAULT_AI_OS_STATE["featured_apps"])
    computed_at = data.get("computed_at")
    if computed_at is not None:
        data["computed_at"] = computed_at.isoformat()
    return data
