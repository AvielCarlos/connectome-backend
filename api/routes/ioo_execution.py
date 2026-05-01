"""
IOO Execution Protocol Routes.

IOO Graph = possibility map.
IOO Execution Protocol = turns a selected node into reality through a structured,
human-confirmed execution plan. This foundation plans external actions but does
not perform bookings, purchases, messages, or other irreversible actions.
"""

from __future__ import annotations

import json
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.middleware import get_current_user_id
from core.database import execute, fetchrow
from ora.agents.ioo_execution_agent import build_execution_protocol
from ora.agents.evolution_engine import record_action_evidence, run_background_evolution

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ioo", tags=["ioo-execution"])


class ExecuteIOORequest(BaseModel):
    node_id: UUID
    intent: str = Field(default="do_now", pattern="^(do_now|do_later)$")


class CompleteExecutionRequest(BaseModel):
    completion_note: Optional[str] = None
    evidence: Optional[dict] = None


def _to_plain(value):
    """Convert asyncpg/DB values into JSON-safe Python structures."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_plain(v) for v in value]
    try:
        from decimal import Decimal

        if isinstance(value, Decimal):
            return float(value)
    except Exception:
        pass
    return value


def _record_to_dict(row) -> dict:
    return {key: _to_plain(row[key]) for key in row.keys()} if row else {}


async def _load_execution_context(user_id: str) -> dict:
    user = await fetchrow(
        """
        SELECT id, email, display_name, city, location_lat, location_lng,
               event_preferences, profile, fulfilment_score, xp_level, streak_current
        FROM users
        WHERE id = $1::uuid
        """,
        str(user_id),
    )
    state = await fetchrow(
        "SELECT * FROM ioo_user_state WHERE user_id = $1::uuid",
        str(user_id),
    )

    user_dict = _record_to_dict(user)
    state_dict = _record_to_dict(state)
    profile = user_dict.get("profile") or {}
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except Exception:
            profile = {}

    return {
        **user_dict,
        **state_dict,
        "profile": profile,
        "user": user_dict,
        "ioo_user_state": state_dict,
    }


def _xp_reward_from_protocol(protocol: dict) -> int:
    criteria = protocol.get("execution_plan", {}).get("completion_criteria", [])
    for item in criteria:
        text = str(item)
        marker = " XP"
        if marker in text:
            prefix = text.split(marker)[0]
            digits = ""
            for char in reversed(prefix):
                if char.isdigit():
                    digits = char + digits
                elif digits:
                    break
            if digits:
                return int(digits)
    return 100


@router.post("/execute")
async def execute_ioo_node(
    body: ExecuteIOORequest,
    user_id: str = Depends(get_current_user_id),
):
    """Build and persist an IOO Execution Protocol for a selected node."""
    node = await fetchrow(
        """
        SELECT id, type, title, description, tags, domain, goal_category,
               requires_finances, requires_fitness_level, requires_skills,
               requires_location, requires_time_hours, step_type, physical_context,
               best_time, requirements, difficulty_level
        FROM ioo_nodes
        WHERE id = $1::uuid AND is_active = TRUE
        """,
        str(body.node_id),
    )
    if not node:
        raise HTTPException(status_code=404, detail="IOO node not found")

    user_context = await _load_execution_context(user_id)
    protocol = build_execution_protocol(_record_to_dict(node), user_context, body.intent)

    # SearchAgent enrichment is included in the protocol as side-effect-free
    # candidates/query surfaces. Live providers can replace the graceful fallback
    # without changing the persisted execution-run shape.
    run = await fetchrow(
        """
        INSERT INTO ioo_execution_runs (user_id, node_id, intent, status, protocol, updated_at)
        VALUES ($1::uuid, $2::uuid, $3, $4, $5::jsonb, NOW())
        RETURNING id, status, created_at, updated_at
        """,
        str(user_id),
        str(body.node_id),
        body.intent,
        protocol["status"],
        json.dumps(protocol),
    )

    # Mark that the user has moved from possibility-map browsing into execution.
    await execute(
        """
        INSERT INTO ioo_user_progress (user_id, node_id, status, started_at, created_at)
        VALUES ($1::uuid, $2::uuid, 'started', NOW(), NOW())
        ON CONFLICT DO NOTHING
        """,
        str(user_id),
        str(body.node_id),
    )

    return {
        "run_id": str(run["id"]),
        "status": run["status"],
        "protocol": protocol,
        "created_at": _to_plain(run["created_at"]),
        "updated_at": _to_plain(run["updated_at"]),
    }


@router.get("/execution/{run_id}")
async def get_execution_run(
    run_id: UUID,
    user_id: str = Depends(get_current_user_id),
):
    """Return a persisted IOO execution run for the current user."""
    run = await fetchrow(
        """
        SELECT id, user_id, node_id, intent, status, protocol,
               created_at, updated_at, completed_at
        FROM ioo_execution_runs
        WHERE id = $1::uuid AND user_id = $2::uuid
        """,
        str(run_id),
        str(user_id),
    )
    if not run:
        raise HTTPException(status_code=404, detail="Execution run not found")
    return _record_to_dict(run)


@router.post("/execution/{run_id}/complete")
async def complete_execution_run(
    run_id: UUID,
    body: CompleteExecutionRequest | None = None,
    user_id: str = Depends(get_current_user_id),
):
    """Mark an execution run complete and return a reward-hook placeholder."""
    run = await fetchrow(
        """
        SELECT id, node_id, protocol, status
        FROM ioo_execution_runs
        WHERE id = $1::uuid AND user_id = $2::uuid
        """,
        str(run_id),
        str(user_id),
    )
    if not run:
        raise HTTPException(status_code=404, detail="Execution run not found")

    protocol = run["protocol"] or {}
    if isinstance(protocol, str):
        try:
            protocol = json.loads(protocol)
        except Exception:
            protocol = {}

    completion_payload = {
        "completion_note": body.completion_note if body else None,
        "evidence": body.evidence if body else None,
    }
    protocol.setdefault("completion", {}).update(completion_payload)
    protocol["status"] = "completed"

    updated = await fetchrow(
        """
        UPDATE ioo_execution_runs
        SET status = 'completed', protocol = $3::jsonb, updated_at = NOW(), completed_at = NOW()
        WHERE id = $1::uuid AND user_id = $2::uuid
        RETURNING id, status, completed_at, protocol
        """,
        str(run_id),
        str(user_id),
        json.dumps(protocol),
    )

    await execute(
        """
        UPDATE ioo_user_progress
        SET status = 'completed', completed_at = COALESCE(completed_at, NOW())
        WHERE user_id = $1::uuid AND node_id = $2::uuid
        """,
        str(user_id),
        str(run["node_id"]),
    )

    # Feed completion back into the living IOO neural graph so successful
    # experiences reinforce their node/edges, reward proof, spawn adjacent
    # possibilities, and trim weak branches conservatively.
    evolution = {"cp_awarded": 0, "cp_message": "Evidence received."}
    try:
        evolution = await record_action_evidence(
            user_id=str(user_id),
            node_id=str(run["node_id"]),
            run_id=str(run_id),
            evidence=completion_payload.get("evidence"),
            completion_note=completion_payload.get("completion_note"),
        )
    except Exception as e:
        logger.warning(f"IOO evolution update failed for run {run_id}: {e}")

    xp_reward = _xp_reward_from_protocol(protocol)
    return {
        "run_id": str(updated["id"]),
        "status": updated["status"],
        "completed_at": _to_plain(updated["completed_at"]),
        "xp_reward_hook": {
            "status": "awarded" if evolution.get("cp_awarded") else "recorded",
            "xp": xp_reward,
            "cp": int(evolution.get("cp_awarded") or 0),
            "message": evolution.get("cp_message") or "Evidence recorded. Aura is learning from the action.",
        },
        "evolution": evolution,
        "protocol": _to_plain(updated["protocol"]),
    }

@router.post("/evolution/run")
async def run_evolution_cycle(
    user_id: str = Depends(get_current_user_id),
):
    """Run a conservative Evolution Engine v1 maintenance cycle.

    This is intentionally authenticated and small: it refreshes edge weights and
    prunes only underperforming nodes with enough attempts. Scheduled cloud jobs
    can call the same underlying function later.
    """
    result = await run_background_evolution(max_nodes=25)
    return {
        "status": "ok",
        "engine": "evolution_v1",
        "result": result,
        "message": "Aura refreshed edge weights and conservatively trimmed weak branches.",
    }
