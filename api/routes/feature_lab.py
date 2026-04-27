"""
Feature Lab API Routes

Endpoints:
  GET /api/feature-lab/proposals — list active and recent proposals
  GET /api/feature-lab/status   — summary of what Ora is currently testing
"""

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from api.middleware import get_current_user_id
from ora.brain import get_brain

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/feature-lab", tags=["feature-lab"])


@router.get("/proposals")
async def get_proposals(
    user_id: str = Depends(get_current_user_id),
) -> List[Dict[str, Any]]:
    """List active and recent feature proposals."""
    brain = get_brain()
    try:
        return await brain.feature_lab.get_proposals(limit=20)
    except Exception as e:
        logger.error(f"FeatureLab proposals error: {e}")
        return []


@router.get("/status")
async def get_lab_status(
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Get a summary of what Ora is currently testing."""
    brain = get_brain()
    try:
        return await brain.feature_lab.get_status()
    except Exception as e:
        logger.error(f"FeatureLab status error: {e}")
        return {"active": 0, "recent": [], "current_experiment": None}
