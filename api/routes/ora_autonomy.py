"""
Ora Autonomy API Routes

POST /api/ora/autonomy/run    — trigger full autonomy cycle (admin only)
GET  /api/ora/autonomy/status — last run info, current winner, current weights
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException

from api.middleware import get_current_user_id
from core.database import fetchrow
from core.redis_client import get_redis
from uuid import UUID

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ora/autonomy", tags=["ora_autonomy"])

# Admin emails — users allowed to trigger autonomy runs
ADMIN_EMAILS = {"avi@atdao.org", "nea@atdao.org"}


async def _require_admin(user_id: str = Depends(get_current_user_id)) -> str:
    """Dependency: only allow admin users (by email)."""
    row = await fetchrow("SELECT email FROM users WHERE id = $1", UUID(user_id))
    if not row or row["email"] not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_id


@router.post("/run")
async def trigger_autonomy_run(
    user_id: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """
    Trigger a full Ora autonomy cycle:
      - A/B test auto-promotion
      - Bug detection & auto-fix suggestions
      - Feed quality optimizer
      - Daily summary report to Avi
    """
    logger.info(f"OraAutonomy: manual trigger by user {user_id}")

    try:
        from ora.agents.autonomy_agent import get_autonomy_agent
        from ora.brain import get_brain

        brain = get_brain()
        agent = get_autonomy_agent(getattr(brain, "_openai", None))
        result = await agent.run()
        return {"ok": True, "result": result}
    except Exception as e:
        logger.error(f"OraAutonomy: run failed: {e}")
        raise HTTPException(status_code=500, detail=f"Autonomy run failed: {str(e)}")


@router.get("/status")
async def get_autonomy_status(
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """
    Return current autonomy status:
      - last run time
      - current A/B winner
      - current agent weights
    """
    try:
        r = await get_redis()

        # Last run metadata
        last_run_raw = await r.get("ora:autonomy:last_run")
        last_run: Optional[Dict[str, Any]] = None
        if last_run_raw:
            try:
                last_run = json.loads(last_run_raw)
            except Exception:
                pass

        # Current A/B winner
        winner_raw = await r.get(f"ab:winner:primary_landing_v1")
        current_winner = winner_raw if isinstance(winner_raw, str) else (
            winner_raw.decode() if winner_raw else None
        )

        # Current agent weights
        weights_raw = await r.get("ora:agent_weights")
        current_weights: Optional[Dict[str, float]] = None
        if weights_raw:
            try:
                current_weights = json.loads(weights_raw)
            except Exception:
                pass

        return {
            "last_run": last_run,
            "current_ab_winner": current_winner,
            "current_agent_weights": current_weights,
            "status_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"OraAutonomy: status check failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
