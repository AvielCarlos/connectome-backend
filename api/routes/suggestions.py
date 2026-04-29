"""
Suggestions Routes

GET  /api/suggestions         — list recent community suggestions
POST /api/suggestions         — submit a new suggestion (content + category)
POST /api/suggestions/{id}/vote — upvote a suggestion
"""

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.middleware import get_current_user_id
from core.database import fetch, fetchrow, execute

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/suggestions", tags=["suggestions"])


class SuggestionCreate(BaseModel):
    content: Optional[str] = None
    title: Optional[str] = None   # legacy field alias
    body: Optional[str] = None    # legacy field alias
    category: Optional[str] = "general"

    def get_content(self) -> str:
        return self.content or self.body or self.title or ""


@router.get("")
async def list_suggestions(limit: int = 20) -> List[Dict[str, Any]]:
    """Return recent community suggestions ordered by vote count."""
    try:
        rows = await fetch(
            """
            SELECT id, content, category, status, vote_count, cp_earned, created_at
            FROM user_suggestions
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
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Submit a new community suggestion. Awards 10 CP per submission."""
    content = payload.get_content()
    if not content:
        raise HTTPException(status_code=422, detail="content is required")
    try:
        row = await fetchrow(
            """
            INSERT INTO user_suggestions (user_id, content, category, status, vote_count, cp_earned)
            VALUES ($1, $2, $3, 'pending', 0, 10)
            RETURNING id, content, category, status, cp_earned, created_at
            """,
            UUID(user_id),
            content,
            payload.category or "general",
        )
        if not row:
            raise HTTPException(status_code=500, detail="Could not create suggestion")

        # Credit CP to user_cp_balance
        cp_earned = int(row["cp_earned"] or 10)
        suggestion_id = str(row["id"])
        try:
            await execute(
                """
                INSERT INTO user_cp_balance (user_id, cp_balance, total_cp_earned, last_updated)
                VALUES ($1, $2, $2, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    cp_balance = user_cp_balance.cp_balance + $2,
                    total_cp_earned = user_cp_balance.total_cp_earned + $2,
                    last_updated = NOW()
                """,
                UUID(user_id), cp_earned
            )
        except Exception as _cp_err:
            logger.warning(f"CP credit failed (non-fatal): {_cp_err}")

        # Record in cp_transactions ledger
        try:
            await execute(
                """
                INSERT INTO cp_transactions (user_id, amount, reason, reference_id)
                VALUES ($1, $2, 'suggestion', $3)
                """,
                UUID(user_id), cp_earned, suggestion_id,
            )
        except Exception as _tx_err:
            logger.warning(f"CP transaction ledger write failed (non-fatal): {_tx_err}")

        # Get updated totals
        cp_row = await fetchrow(
            "SELECT cp_balance, total_cp_earned FROM user_cp_balance WHERE user_id = $1",
            UUID(user_id)
        )
        result = dict(row)
        result["suggestion"] = result["content"]
        result["cp_earned"] = cp_earned
        result["total_dao_cp"] = int(cp_row["total_cp_earned"] or 0) if cp_row else cp_earned
        result["cp_balance"] = int(cp_row["cp_balance"] or 0) if cp_row else cp_earned
        result["message"] = f"Earned {cp_earned} CP! Total: {result['total_dao_cp']} CP"
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Suggestion create failed: {e}")
        raise HTTPException(status_code=500, detail="Could not create suggestion")


@router.post("/{suggestion_id}/vote")
async def vote_suggestion(
    suggestion_id: str,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Upvote a suggestion."""
    try:
        row = await fetchrow(
            """
            UPDATE user_suggestions
            SET vote_count = vote_count + 1
            WHERE id = $1
            RETURNING id, content, vote_count
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


@router.get("/mine")
async def get_my_suggestions(
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Get the current user's suggestions and CP balance."""
    try:
        suggestions = await fetch(
            """
            SELECT id, content, category, status, vote_count, cp_earned, created_at
            FROM user_suggestions
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT 50
            """,
            UUID(user_id),
        )
        cp_row = await fetchrow(
            "SELECT cp_balance, total_cp_earned FROM user_cp_balance WHERE user_id = $1",
            UUID(user_id),
        )
        return {
            "suggestions": [dict(r) for r in suggestions],
            "total_suggestions": len(suggestions),
            "total_cp_earned": int(cp_row["total_cp_earned"] or 0) if cp_row else 0,
            "total_dao_cp": int(cp_row["total_cp_earned"] or 0) if cp_row else 0,
            "cp_balance": int(cp_row["cp_balance"] or 0) if cp_row else 0,
            "tier": "contributor" if (cp_row and int(cp_row["total_cp_earned"] or 0) >= 100) else "observer",
        }
    except Exception as e:
        logger.error(f"Get my suggestions failed: {e}")
        return {"suggestions": [], "total_suggestions": 0, "total_cp_earned": 0, "total_dao_cp": 0, "cp_balance": 0, "tier": "observer"}
