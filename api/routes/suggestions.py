"""
Suggestions / Contributions Routes

GET  /api/suggestions         — list recent community suggestions
POST /api/suggestions         — submit a new suggestion
POST /api/suggestions/{id}/vote — upvote a suggestion
"""

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.routes.users import get_current_user
from core.database import fetch, fetchrow, execute

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/suggestions", tags=["suggestions"])


class SuggestionCreate(BaseModel):
    title: str
    body: str
    category: Optional[str] = "general"


@router.get("")
async def list_suggestions(limit: int = 20) -> List[Dict[str, Any]]:
    """Return recent community suggestions ordered by vote count."""
    try:
        rows = await fetch(
            """
            SELECT id, title, body, category, status, vote_count, created_at
            FROM contributions
            ORDER BY vote_count DESC, created_at DESC
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"Suggestions list failed: {e}")
        return []


@router.post("")
async def create_suggestion(
    payload: SuggestionCreate,
    current_user: Dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Submit a new community suggestion."""
    user_id = current_user["id"]
    try:
        row = await fetchrow(
            """
            INSERT INTO contributions (user_id, title, body, category, status, vote_count)
            VALUES ($1, $2, $3, $4, 'pending', 0)
            RETURNING id, title, status, created_at
            """,
            UUID(user_id),
            payload.title,
            payload.body,
            payload.category,
        )
        return dict(row) if row else {}
    except Exception as e:
        logger.error(f"Suggestion create failed: {e}")
        raise HTTPException(status_code=500, detail="Could not create suggestion")


@router.post("/{suggestion_id}/vote")
async def vote_suggestion(
    suggestion_id: str,
    current_user: Dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Upvote a suggestion."""
    try:
        row = await fetchrow(
            """
            UPDATE contributions
            SET vote_count = vote_count + 1
            WHERE id = $1
            RETURNING id, title, vote_count
            """,
            UUID(suggestion_id),
        )
        if not row:
            raise HTTPException(status_code=404, detail="Suggestion not found")
        return dict(row)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Suggestion vote failed: {e}")
        raise HTTPException(status_code=500, detail="Could not vote")
