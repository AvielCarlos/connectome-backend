"""Knowledge routes — Integral synthesis conflict resolution."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from ora.agents.agent_memory import AgentInsight, agent_memory_bus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", os.getenv("ADMIN_SECRET", "connectome-admin-secret"))


class ConflictResolutionRequest(BaseModel):
    conflict_id: str = Field(..., min_length=4)
    resolution: str = Field(..., min_length=2)
    source_of_truth: str = Field(..., min_length=2)


def _require_admin(x_admin_token: str = Header(default="")) -> None:
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/conflict-resolve")
async def resolve_knowledge_conflict(
    body: ConflictResolutionRequest,
    x_admin_token: str = Header(default=""),
) -> Dict[str, Any]:
    """
    Store Avi's resolution to a knowledge conflict as a permanent memory-bus insight.
    """
    _require_admin(x_admin_token)
    payload = {
        "conflict_id": body.conflict_id,
        "resolution": body.resolution,
        "source_of_truth": body.source_of_truth,
        "resolved_by": "avi",
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    insight = AgentInsight(
        source_agent="avi",
        domain="integral_knowledge",
        insight_type="conflict_resolution",
        content=json.dumps(payload, ensure_ascii=False),
        confidence=1.0,
        action_required=False,
        target_agents=[],
        expires_at=datetime.now(timezone.utc),
    )
    insight_id = await agent_memory_bus.publish(insight)
    if not insight_id:
        logger.warning("Failed to publish knowledge conflict resolution: %s", body.conflict_id)
        raise HTTPException(status_code=500, detail="Unable to store conflict resolution")
    await agent_memory_bus.promote_to_permanent(insight_id)
    return {"status": "stored", "insight_id": insight_id, **payload}
