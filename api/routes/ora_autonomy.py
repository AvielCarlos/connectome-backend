"""
Ora Autonomy API Routes

POST /api/ora/autonomy/run                      — trigger full autonomy cycle (admin only)
GET  /api/ora/autonomy/status                   — last run info, current winner, current weights
GET  /api/ora/autonomy/proposals                — list pending self-improvement proposals (admin only)
POST /api/ora/autonomy/proposals/{id}/approve   — approve and apply a high-risk proposal
POST /api/ora/autonomy/proposals/{id}/reject    — reject a proposal
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import os
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from api.middleware import decode_token
from core.database import fetchrow
from core.redis_client import get_redis
from uuid import UUID

PROPOSALS_REDIS_KEY = "ora:self_improvement:proposals"

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ora/autonomy", tags=["ora_autonomy"])

# Admin emails — users allowed to trigger autonomy runs
ADMIN_EMAILS = {"avi@atdao.org", "nea@atdao.org", "carlosandromeda8@gmail.com"}
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "connectome-admin-secret")


async def _require_admin(
    request: Request,
    x_admin_token: Optional[str] = Header(default=None, alias="x-admin-token"),
    authorization: Optional[str] = Header(default=None),
) -> str:
    """Dependency: allow admin users (by email) OR valid X-Admin-Token header.

    Important: token auth must work without a Bearer token for automation crons.
    Do not depend on get_current_user_id here because its OAuth dependency raises
    401 before this function can accept X-Admin-Token.
    """
    # Token-based auth for automated crons
    if x_admin_token and x_admin_token == ADMIN_TOKEN:
        return "admin-token"

    # Optional email-based auth for logged-in admin users
    user_id: Optional[str] = None
    if authorization and authorization.lower().startswith("bearer "):
        user_id = decode_token(authorization.split(" ", 1)[1].strip())
    if user_id:
        row = await fetchrow("SELECT email FROM users WHERE id = $1", UUID(user_id))
        if row and row["email"] in ADMIN_EMAILS:
            return user_id

    raise HTTPException(status_code=403, detail="Admin access required")


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


@router.get("/proposals")
async def list_proposals(
    user_id: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """
    List all pending self-improvement proposals Ora has generated.
    High-risk changes (logic rewrites, new routes, DB changes) land here
    awaiting admin review.
    """
    try:
        r = await get_redis()
        raw = await r.get(PROPOSALS_REDIS_KEY)
        proposals = json.loads(raw) if raw else []
        return {"proposals": list(reversed(proposals)), "count": len(proposals)}
    except Exception as e:
        logger.error(f"OraAutonomy: proposals list failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: str,
    user_id: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """
    Approve a high-risk proposal — attempts to apply the change via GitHub.
    """
    try:
        r = await get_redis()
        raw = await r.get(PROPOSALS_REDIS_KEY)
        proposals: list = json.loads(raw) if raw else []

        target = next((p for p in proposals if p.get("id") == proposal_id), None)
        if not target:
            raise HTTPException(status_code=404, detail="Proposal not found")

        from ora.brain import get_brain
        brain = get_brain()
        from ora.agents.self_improvement_agent import SelfImprovementAgent
        agent = SelfImprovementAgent(getattr(brain, "_openai", None))

        # Force to prompt_text for the apply path (admin-approved)
        target_copy = {**target, "risk": "prompt_text"}
        applied = await agent._auto_apply(target_copy) if target.get("target_file") else False

        for p in proposals:
            if p.get("id") == proposal_id:
                p["status"] = "applied" if applied else "approved_pending"
                p["approved_by"] = user_id
                p["approved_at"] = datetime.now(timezone.utc).isoformat()

        await r.set(PROPOSALS_REDIS_KEY, json.dumps(proposals), ex=30 * 24 * 3600)
        return {"ok": True, "applied": applied, "proposal": target}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OraAutonomy: approve failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: str,
    user_id: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """
    Reject a self-improvement proposal (removes it from the list).
    """
    try:
        r = await get_redis()
        raw = await r.get(PROPOSALS_REDIS_KEY)
        proposals: list = json.loads(raw) if raw else []

        target = next((p for p in proposals if p.get("id") == proposal_id), None)
        if not target:
            raise HTTPException(status_code=404, detail="Proposal not found")

        for p in proposals:
            if p.get("id") == proposal_id:
                p["status"] = "rejected"
                p["rejected_by"] = user_id
                p["rejected_at"] = datetime.now(timezone.utc).isoformat()

        await r.set(PROPOSALS_REDIS_KEY, json.dumps(proposals), ex=30 * 24 * 3600)
        return {"ok": True, "proposal": target}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OraAutonomy: reject failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Evolution API endpoints
# ---------------------------------------------------------------------------

EVOLUTION_PROPOSALS_KEY = "ora:evolution:proposals"


@router.get("/evolution/population")
async def get_evolution_population(
    user_id: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """
    Current agent population with fitness scores.
    """
    try:
        from ora.agent_registry import AgentRegistry
        from ora.agents.evolution_agent import OraEvolutionAgent
        from ora.brain import get_brain

        registry = AgentRegistry()
        all_agents = await registry.get_all_agents()

        # Try to get fitness data
        brain = get_brain()
        evolution_agent = OraEvolutionAgent(getattr(brain, "_openai", None))
        try:
            fitness = await evolution_agent.analyze_population_fitness()
        except Exception:
            fitness = {}

        # Enrich agents with fitness
        enriched = []
        for agent in all_agents:
            name = agent["name"]
            agent_fitness = fitness.get(name, {})
            enriched.append({
                **agent,
                "fitness": agent_fitness,
                "health": (
                    "thriving" if (agent_fitness.get("fitness_score") or 0) >= 3.5
                    else "struggling" if (agent_fitness.get("fitness_score") or 0) < 1.5
                    else "average"
                ) if agent_fitness.get("fitness_score") is not None else "unknown",
            })

        return {
            "population": enriched,
            "total": len(enriched),
            "active": sum(1 for a in enriched if a["status"] == "active"),
            "retired": sum(1 for a in enriched if a["status"] in ("retired", "partitioned", "cannibalized")),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"OraEvolution: population fetch failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/evolution/history")
async def get_evolution_history(
    user_id: str = Depends(_require_admin),
    limit: int = 50,
) -> Dict[str, Any]:
    """
    Full evolutionary history from ora_lessons.
    """
    try:
        from core.database import fetch as db_fetch
        rows = await db_fetch(
            """
            SELECT lesson, confidence, source, created_at
            FROM ora_lessons
            WHERE source LIKE 'OraEvolutionAgent%'
               OR source LIKE 'AgentRegistry%'
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )
        history = [
            {
                "lesson": row["lesson"],
                "confidence": float(row["confidence"]),
                "source": row["source"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            for row in rows
        ]
        return {"history": history, "count": len(history)}
    except Exception as e:
        logger.error(f"OraEvolution: history fetch failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/evolution/run")
async def trigger_evolution_run(
    user_id: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """
    Trigger an evolution cycle manually (admin only).
    """
    logger.info(f"OraEvolution: manual trigger by {user_id}")
    try:
        from ora.agents.evolution_agent import OraEvolutionAgent
        from ora.brain import get_brain

        brain = get_brain()
        evolution_agent = OraEvolutionAgent(getattr(brain, "_openai", None))
        result = await evolution_agent.run()
        return {"ok": True, "result": result}
    except Exception as e:
        logger.error(f"OraEvolution: manual run failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/evolution/lineage/{agent_name}")
async def get_evolution_lineage(
    agent_name: str,
    user_id: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """
    Evolutionary family tree for a given agent.
    """
    try:
        from ora.agent_registry import AgentRegistry
        registry = AgentRegistry()
        lineage = await registry.get_lineage(agent_name)
        all_agents = await registry.get_all_agents()
        agent_map = {a["name"]: a for a in all_agents}

        tree = [
            {
                "name": n,
                "type": agent_map.get(n, {}).get("type", "unknown"),
                "status": agent_map.get(n, {}).get("status", "unknown"),
                "generation": agent_map.get(n, {}).get("generation", 0),
                "created_at": agent_map.get(n, {}).get("created_at"),
            }
            for n in lineage
        ]
        return {"agent": agent_name, "lineage": tree, "depth": len(tree)}
    except Exception as e:
        logger.error(f"OraEvolution: lineage fetch failed for {agent_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/evolution/proposals")
async def get_evolution_proposals(
    user_id: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """List pending evolution proposals requiring Avi's approval."""
    try:
        r = await get_redis()
        raw = await r.get(EVOLUTION_PROPOSALS_KEY)
        proposals = json.loads(raw) if raw else []
        pending = [p for p in proposals if p.get("status") == "pending"]
        return {"proposals": list(reversed(pending)), "count": len(pending)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/evolution/proposals/{proposal_id}/approve")
async def approve_evolution_proposal(
    proposal_id: str,
    user_id: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """Approve and execute an escalated evolution proposal."""
    try:
        r = await get_redis()
        raw = await r.get(EVOLUTION_PROPOSALS_KEY)
        proposals: list = json.loads(raw) if raw else []

        target = next((p for p in proposals if p.get("id") == proposal_id), None)
        if not target:
            raise HTTPException(status_code=404, detail="Proposal not found")

        from ora.agents.evolution_agent import OraEvolutionAgent
        from ora.brain import get_brain

        brain = get_brain()
        evolution_agent = OraEvolutionAgent(getattr(brain, "_openai", None))

        proposal_type = target.get("type")
        executed = False

        if proposal_type == "partition":
            await evolution_agent.execute_partition(
                target["agent_name"],
                target["spec_a"],
                target["spec_b"],
            )
            executed = True
        elif proposal_type == "merge":
            await evolution_agent.execute_merge(
                target["agent_a"],
                target["agent_b"],
                target["merged_spec"],
            )
            executed = True
        elif proposal_type == "cannibalize":
            await evolution_agent.execute_cannibalization(
                target["weak_agent"],
                target["strong_agent"],
            )
            executed = True

        for p in proposals:
            if p.get("id") == proposal_id:
                p["status"] = "approved" if executed else "approved_pending"
                p["approved_by"] = user_id
                p["approved_at"] = datetime.now(timezone.utc).isoformat()

        await r.set(EVOLUTION_PROPOSALS_KEY, json.dumps(proposals), ex=30 * 24 * 3600)
        return {"ok": True, "executed": executed, "proposal": target}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OraEvolution: approve failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/evolution/proposals/{proposal_id}/reject")
async def reject_evolution_proposal(
    proposal_id: str,
    user_id: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """Reject an evolution proposal."""
    try:
        r = await get_redis()
        raw = await r.get(EVOLUTION_PROPOSALS_KEY)
        proposals: list = json.loads(raw) if raw else []

        for p in proposals:
            if p.get("id") == proposal_id:
                p["status"] = "rejected"
                p["rejected_by"] = user_id
                p["rejected_at"] = datetime.now(timezone.utc).isoformat()

        await r.set(EVOLUTION_PROPOSALS_KEY, json.dumps(proposals), ex=30 * 24 * 3600)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/evolution/spawn")
async def spawn_new_agent(
    body: Dict[str, Any],
    user_id: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """Manually spawn a new agent to fill a described gap."""
    gap_description = body.get("gap_description", "")
    if not gap_description:
        raise HTTPException(status_code=400, detail="gap_description is required")

    try:
        from ora.agents.evolution_agent import OraEvolutionAgent
        from ora.brain import get_brain

        brain = get_brain()
        evolution_agent = OraEvolutionAgent(getattr(brain, "_openai", None))
        agent_record = await evolution_agent.spawn_new_agent(gap_description)
        return {"ok": True, "agent": agent_record}
    except Exception as e:
        logger.error(f"OraEvolution: spawn failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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


@router.get("/onboarding/results")
async def get_onboarding_results(
    user_id: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """Get onboarding A/B test retention results."""
    try:
        from ora.agents.onboarding_agent_v2 import OnboardingOptimizationAgent
        agent = OnboardingOptimizationAgent()
        results = await agent.analyze_retention()
        return {"ok": True, "results": results}
    except Exception as e:
        logger.error(f"OraAutonomy: onboarding results failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/product/proposals")
async def get_product_proposals(
    user_id: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """Get pending UI + feature proposals from Ora."""
    try:
        r = await get_redis()
        ui_raw = await r.get("ora:ui_proposals")
        feature_raw = await r.get("ora:feature_proposals")

        ui_data = json.loads(ui_raw) if ui_raw else {}
        feature_data = json.loads(feature_raw) if feature_raw else {}

        return {
            "ok": True,
            "ui_proposals": ui_data.get("proposals", []),
            "ui_applied": ui_data.get("applied", []),
            "ui_issues": ui_data.get("issues", []),
            "feature_proposals": feature_data.get("proposals", []),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"OraAutonomy: product proposals failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

