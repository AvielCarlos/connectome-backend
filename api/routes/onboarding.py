"""
Onboarding API Routes

POST /api/users/onboarding/variant  — get/assign the user's A/B onboarding variant
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from api.middleware import get_current_user_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["onboarding"])


@router.post("/onboarding/variant")
async def get_onboarding_variant(
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """
    Assign and return the user's onboarding variant.
    Variant is deterministically derived from user_id (consistent across calls).
    Once a winner is promoted it is returned for all users.
    """
    try:
        from aura.agents.onboarding_agent_v2 import OnboardingOptimizationAgent
        agent = OnboardingOptimizationAgent()
        variant = await agent.assign_onboarding_variant(user_id)
        name = OnboardingOptimizationAgent.ONBOARDING_VARIANTS[variant]
        return {"variant": variant, "name": name}
    except Exception as e:
        logger.error(f"Onboarding: variant assignment failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
