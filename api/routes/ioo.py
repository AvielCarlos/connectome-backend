"""
IOO API Routes — IRL Experience Achievement Map

Endpoints:
  GET  /api/ioo/state                         — get my user state
  POST /api/ioo/state                         — update user state (partial)
  GET  /api/ioo/graph?goal_id=X               — recommended next nodes
  POST /api/ioo/progress                      — record node progress
  GET  /api/ioo/progress                      — get my progress
  GET  /api/ioo/nodes                         — browse nodes (filters: type, domain, tag)
  GET  /api/ioo/nodes/{id}                    — node detail
  POST /api/ioo/surfaces/{node_id}            — spawn a surface for a node
  GET  /api/ioo/surfaces/{node_id}            — get active surfaces for a node
  POST /api/ioo/surfaces/{surface_id}/interact — record interaction/completion
  POST /api/ioo/seed                          — seed initial nodes (admin/dev)
"""

import logging
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core.database import fetch, fetchrow, fetchval, execute
from api.middleware import get_current_user_id
from ora.agents.ioo_graph_agent import get_graph_agent
from ora.agents.ioo_enrichment_agent import get_ioo_enrichment_agent
from ora.agents.surface_generator import SurfaceGenerator
from ora.agents.surface_lifecycle import SurfaceLifecycleManager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ioo", tags=["ioo"])


async def _ioo_xp_for_node(node_id: str, user_id: str) -> int:
    """Calculate XP for completing an IOO node, scaled by difficulty."""
    node = await fetchrow(
        "SELECT difficulty_level, type, goal_category, step_type FROM ioo_nodes WHERE id = $1::uuid",
        str(node_id),
    )
    if not node:
        return 0

    difficulty = int(node["difficulty_level"] or 5)
    xp_by_difficulty = {
        1: 25, 2: 40, 3: 60, 4: 80, 5: 100,
        6: 150, 7: 200, 8: 300, 9: 400, 10: 500,
    }
    base_xp = xp_by_difficulty.get(difficulty, 100)
    if (node["step_type"] or "digital") == "physical":
        base_xp = int(base_xp * 1.5)
    return base_xp


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class UserStateUpdate(BaseModel):
    finances_level: Optional[str] = None          # unknown/tight/moderate/comfortable/wealthy
    finances_monthly_budget_usd: Optional[float] = None
    location_city: Optional[str] = None
    location_country: Optional[str] = None
    fitness_level: Optional[int] = None           # 0-10
    known_skills: Optional[List[str]] = None
    has_partner: Optional[bool] = None
    has_car: Optional[bool] = None
    free_time_weekday_hours: Optional[float] = None
    free_time_weekend_hours: Optional[float] = None
    state_json: Optional[dict] = None


class ProgressUpdate(BaseModel):
    node_id: str
    goal_id: Optional[str] = None
    status: str                                    # suggested/viewed/started/completed/abandoned
    surface_type: Optional[str] = None
    surface_id: Optional[str] = None
    hours_taken: Optional[float] = None           # for completed nodes


class SurfaceSpawnRequest(BaseModel):
    surface_type: str = "info_card"               # booking_flow/habit_tracker/challenge/checklist/info_card
    title: Optional[str] = None


class SurfaceInteractRequest(BaseModel):
    action: str = "view"                          # view/interact/complete/goal_success
    goal_id: Optional[str] = None


# ---------------------------------------------------------------------------
# User State
# ---------------------------------------------------------------------------

@router.get("/state")
async def get_user_state(user_id: str = Depends(get_current_user_id)):
    """Return the current user state for the IOO graph."""
    row = await fetchrow(
        "SELECT * FROM ioo_user_state WHERE user_id = $1",
        str(user_id),
    )
    if not row:
        # Return empty defaults
        return {
            "user_id": str(user_id),
            "finances_level": "unknown",
            "finances_monthly_budget_usd": None,
            "location_city": None,
            "location_country": None,
            "fitness_level": 5,
            "known_skills": [],
            "has_partner": None,
            "has_car": None,
            "free_time_weekday_hours": None,
            "free_time_weekend_hours": None,
            "state_json": {},
        }
    return dict(row)


@router.post("/state")
async def update_user_state(
    body: UserStateUpdate,
    user_id: str = Depends(get_current_user_id),
):
    """Upsert user state. Only provided fields are updated."""
    # Upsert base record
    await execute(
        """
        INSERT INTO ioo_user_state (user_id) VALUES ($1)
        ON CONFLICT (user_id) DO NOTHING
        """,
        str(user_id),
    )

    updates: list = []
    params: list = [str(user_id)]
    idx = 2

    def add(col: str, val):
        nonlocal idx
        updates.append(f"{col} = ${idx}")
        params.append(val)
        idx += 1

    if body.finances_level is not None:
        add("finances_level", body.finances_level)
    if body.finances_monthly_budget_usd is not None:
        add("finances_monthly_budget_usd", body.finances_monthly_budget_usd)
    if body.location_city is not None:
        add("location_city", body.location_city)
    if body.location_country is not None:
        add("location_country", body.location_country)
    if body.fitness_level is not None:
        add("fitness_level", body.fitness_level)
    if body.known_skills is not None:
        add("known_skills", body.known_skills)
    if body.has_partner is not None:
        add("has_partner", body.has_partner)
    if body.has_car is not None:
        add("has_car", body.has_car)
    if body.free_time_weekday_hours is not None:
        add("free_time_weekday_hours", body.free_time_weekday_hours)
    if body.free_time_weekend_hours is not None:
        add("free_time_weekend_hours", body.free_time_weekend_hours)
    if body.state_json is not None:
        import json
        add("state_json", json.dumps(body.state_json))

    if updates:
        updates.append("last_updated = NOW()")
        sql = f"UPDATE ioo_user_state SET {', '.join(updates)} WHERE user_id = $1"
        await execute(sql, *params)

    return {"ok": True}


# ---------------------------------------------------------------------------
# Graph / Recommendations
# ---------------------------------------------------------------------------

@router.get("/graph")
async def get_graph_recommendations(
    goal_id: Optional[str] = Query(None),
    limit: int = Query(5, ge=1, le=20),
    preference: str = Query("mixed", pattern="^(prefer_digital|prefer_physical|mixed)$"),
    user_id: str = Depends(get_current_user_id),
):
    """Return recommended next nodes toward a goal, filtered by user capabilities."""
    if goal_id:
        try:
            UUID(goal_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid goal node ID")

    agent = get_graph_agent()
    try:
        nodes = await agent.recommend_next_nodes(
            user_id=str(user_id),
            goal_id=goal_id,
            limit=limit,
        )
        suggested_path = []
        if goal_id:
            suggested_path = await agent.build_personalised_path(
                user_id=str(user_id),
                goal_node_id=goal_id,
                max_steps=10,
                preference=preference,
            )
    except Exception as e:
        logger.error(f"Graph recommendation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Graph recommendation failed")

    return {"nodes": nodes, "count": len(nodes), "preference": preference, "suggested_path": suggested_path}


@router.get("/path")
async def get_path_to_goal(
    goal_id: str = Query(..., description="Destination IOO goal node ID"),
    max_steps: int = Query(10, ge=1, le=25),
    preference: str = Query("mixed", pattern="^(prefer_digital|prefer_physical|mixed)$"),
    user_id: str = Depends(get_current_user_id),
):
    """Return Google-Maps-style step-by-step path to a goal node."""
    try:
        UUID(goal_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid goal node ID")

    agent = get_graph_agent()
    try:
        path = await agent.build_personalised_path(
            user_id=str(user_id),
            goal_node_id=goal_id,
            max_steps=max_steps,
            preference=preference,
        )
    except Exception as e:
        logger.error(f"IOO pathfinding error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Pathfinding failed")
    if not path:
        goal_exists = await fetchval(
            "SELECT EXISTS(SELECT 1 FROM ioo_nodes WHERE id = $1::uuid AND is_active = TRUE)",
            goal_id,
        )
        if not goal_exists:
            raise HTTPException(status_code=404, detail="Goal node not found")
    return {"goal_id": goal_id, "preference": preference, "path": path, "steps": len(path)}


@router.get("/eligibility/{node_id}")
async def get_node_eligibility(
    node_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Check whether current user can attempt a node and which bridge nodes they need."""
    try:
        UUID(node_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid node ID")
    agent = get_graph_agent()
    result = await agent.check_node_eligibility(str(user_id), node_id)
    if result.get("gaps") and result["gaps"][0].get("type") == "missing_node":
        raise HTTPException(status_code=404, detail="Node not found")
    return result


@router.get("/vector")
@router.get("/user-vector")
async def get_my_ioo_vector_summary(user_id: str = Depends(get_current_user_id)):
    """Return a safe summary of the current user's IOO vector fingerprint."""
    agent = get_graph_agent()
    return await agent.get_user_vector_summary(str(user_id))


@router.get("/vector-recommend")
@router.get("/vector-recommendations")
async def get_vector_recommendations(
    goal_context: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
    preference: str = Query("mixed", pattern="^(prefer_digital|prefer_physical|mixed)$"),
    user_id: str = Depends(get_current_user_id),
):
    """Return IOO nodes ranked by semantic similarity to a goal/context string."""
    agent = get_graph_agent()
    nodes = await agent.vector_recommend(
        str(user_id),
        goal_context=goal_context,
        limit=limit,
        preference=preference,
    )
    return {"nodes": nodes, "count": len(nodes), "preference": preference}


@router.post("/embed-nodes")
async def embed_ioo_nodes(user_id: str = Depends(get_current_user_id)):
    """Embed IOO nodes that do not have embeddings yet. Available to all for Phase 1."""
    agent = get_graph_agent()
    embedded = await agent.embed_all_nodes()
    return {"ok": True, "embedded": embedded}


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

@router.post("/progress")
async def record_progress(
    body: ProgressUpdate,
    user_id: str = Depends(get_current_user_id),
):
    """Record or update a user's progress on a node."""
    # Validate node exists
    node = await fetchrow(
        "SELECT id, type FROM ioo_nodes WHERE id = $1",
        str(body.node_id),
    )
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    # Upsert progress row
    existing = await fetchrow(
        """
        SELECT id, status FROM ioo_user_progress
        WHERE user_id = $1 AND node_id = $2
        """,
        str(user_id),
        str(body.node_id),
    )

    started_at_sql = "started_at = NOW()," if body.status == "started" else ""
    completed_at_sql = "completed_at = NOW()," if body.status == "completed" else ""
    abandoned_at_sql = "abandoned_at = NOW()," if body.status == "abandoned" else ""

    if existing:
        await execute(
            f"""
            UPDATE ioo_user_progress SET
                status = $3,
                {started_at_sql}
                {completed_at_sql}
                {abandoned_at_sql}
                surface_type = COALESCE($4, surface_type),
                surface_id = COALESCE($5, surface_id)
            WHERE id = $1 AND user_id = $2
            """,
            str(existing["id"]),
            str(user_id),
            body.status,
            body.surface_type,
            body.surface_id,
        )
    else:
        await execute(
            f"""
            INSERT INTO ioo_user_progress
                (user_id, node_id, goal_id, status, surface_type, surface_id,
                 started_at, completed_at, abandoned_at)
            VALUES ($1, $2, $3, $4, $5, $6,
                    {'NOW()' if body.status == 'started' else 'NULL'},
                    {'NOW()' if body.status == 'completed' else 'NULL'},
                    {'NOW()' if body.status == 'abandoned' else 'NULL'})
            """,
            str(user_id),
            str(body.node_id),
            str(body.goal_id) if body.goal_id else None,
            body.status,
            body.surface_type,
            body.surface_id,
        )

    # Update node aggregate stats when completed/abandoned
    agent = get_graph_agent()
    challenge_awards = []
    if body.status == "completed":
        await agent.record_node_outcome(
            str(user_id),
            str(body.node_id),
            success=True,
            hours_taken=body.hours_taken or 0.0,
        )
        xp_amount = await _ioo_xp_for_node(str(body.node_id), str(user_id))
        if xp_amount > 0:
            try:
                await execute(
                    "INSERT INTO xp_log (user_id, amount, reason, ref_id) VALUES ($1, $2, $3, $4)",
                    UUID(str(user_id)), xp_amount, "ioo_node_complete", str(body.node_id),
                )
            except Exception as e:
                logger.warning(f"IOO XP award failed: {e}")
        try:
            from api.routes.friends import award_completed_challenges

            challenge_awards = await award_completed_challenges(str(user_id), str(body.node_id))
        except Exception as e:
            logger.warning(f"IOO challenge completion awards failed: {e}")
            challenge_awards = []
    elif body.status == "abandoned":
        await agent.record_node_outcome(
            str(user_id),
            str(body.node_id),
            success=False,
        )

    if body.status in ("viewed", "started", "completed"):
        try:
            await agent.build_user_ioo_vector(str(user_id))
        except Exception as e:
            logger.debug(f"IOO user vector update skipped: {e}")

    return {"ok": True, "status": body.status, "challenge_awards": challenge_awards}


@router.get("/progress")
async def get_progress(
    goal_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    user_id: str = Depends(get_current_user_id),
):
    """Get the user's progress through the graph."""
    conditions = ["p.user_id = $1"]
    params: list = [str(user_id)]
    idx = 2

    if goal_id:
        conditions.append(f"p.goal_id = ${idx}")
        params.append(str(goal_id))
        idx += 1
    if status:
        conditions.append(f"p.status = ${idx}")
        params.append(status)
        idx += 1

    where = " AND ".join(conditions)
    rows = await fetch(
        f"""
        SELECT p.*, n.title, n.type, n.domain, n.requires_time_hours, n.requires_finances
        FROM ioo_user_progress p
        JOIN ioo_nodes n ON n.id = p.node_id
        WHERE {where}
        ORDER BY p.created_at DESC
        LIMIT 100
        """,
        *params,
    )
    return {"progress": [dict(r) for r in rows]}




# ---------------------------------------------------------------------------
# Proposals / Enrichment
# ---------------------------------------------------------------------------

@router.get("/proposals")
async def list_node_proposals(
    status: str = Query("pending", pattern="^(pending|approved|rejected|all)$"),
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(get_current_user_id),
):
    """Return IOO node proposals for review. Admin-light for Phase 1."""
    if status == "all":
        rows = await fetch(
            "SELECT * FROM ioo_node_proposals ORDER BY created_at DESC LIMIT $1",
            limit,
        )
    else:
        rows = await fetch(
            "SELECT * FROM ioo_node_proposals WHERE status = $1 ORDER BY created_at DESC LIMIT $2",
            status, limit,
        )
    return {"proposals": [dict(r) for r in rows], "count": len(rows)}


@router.post("/proposals/{proposal_id}/approve")
async def approve_node_proposal(
    proposal_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Promote a pending proposal into a live IOO node."""
    try:
        UUID(proposal_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid proposal ID")

    proposal = await fetchrow("SELECT * FROM ioo_node_proposals WHERE id = $1::uuid", proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    existing = await fetchrow(
        "SELECT id FROM ioo_nodes WHERE lower(title) = lower($1) LIMIT 1",
        proposal["title"],
    )
    if existing:
        await execute("UPDATE ioo_node_proposals SET status = 'approved' WHERE id = $1::uuid", proposal_id)
        return {"ok": True, "node_id": str(existing["id"]), "already_exists": True}

    node = await fetchrow(
        """
        INSERT INTO ioo_nodes
            (type, title, description, tags, domain, step_type, goal_category, difficulty_level)
        VALUES ('activity', $1, $2, $3, $4, $5, $6, 5)
        RETURNING id
        """,
        proposal["title"],
        proposal["description"],
        proposal["tags"] or [],
        proposal["domain"],
        proposal["step_type"] or "hybrid",
        proposal["goal_category"],
    )
    await execute("UPDATE ioo_node_proposals SET status = 'approved' WHERE id = $1::uuid", proposal_id)
    try:
        await get_graph_agent().embed_all_nodes()
    except Exception as e:
        logger.debug(f"Embedding approved IOO proposal skipped: {e}")
    return {"ok": True, "node_id": str(node["id"])}


@router.post("/enrich")
async def run_ioo_enrichment(user_id: str = Depends(get_current_user_id)):
    """Run daily IOO graph enrichment now. Admin-light for Phase 1."""
    result = await get_ioo_enrichment_agent().run_daily()
    return result


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

@router.get("/nodes")
async def list_nodes(
    type: Optional[str] = Query(None),
    domain: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user_id: str = Depends(get_current_user_id),
):
    """Browse all active IOO nodes with optional filters."""
    conditions = ["is_active = TRUE"]
    params: list = []
    idx = 1

    if type:
        conditions.append(f"type = ${idx}")
        params.append(type)
        idx += 1
    if domain:
        conditions.append(f"domain = ${idx}")
        params.append(domain)
        idx += 1
    if tag:
        conditions.append(f"${idx} = ANY(tags)")
        params.append(tag)
        idx += 1

    where = " AND ".join(conditions)
    rows = await fetch(
        f"""
        SELECT * FROM ioo_nodes
        WHERE {where}
        ORDER BY
            CASE WHEN attempt_count > 0 THEN success_count::float / attempt_count ELSE 0.5 END DESC,
            created_at DESC
        LIMIT ${idx} OFFSET ${idx+1}
        """,
        *params,
        limit,
        offset,
    )
    total = await fetchval(f"SELECT COUNT(*) FROM ioo_nodes WHERE {where}", *params)
    return {"nodes": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/nodes/{node_id}")
async def get_node(
    node_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Get a single IOO node with outgoing edges."""
    try:
        uid = UUID(node_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid node ID")

    node = await fetchrow("SELECT * FROM ioo_nodes WHERE id = $1", uid)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    # Get outgoing edges with destination node titles
    edges = await fetch(
        """
        SELECT e.*, n.title AS to_title, n.type AS to_type
        FROM ioo_edges e
        JOIN ioo_nodes n ON n.id = e.to_node_id
        WHERE e.from_node_id = $1
        ORDER BY e.weight DESC
        LIMIT 10
        """,
        uid,
    )

    # Get user's progress on this node
    progress = await fetchrow(
        "SELECT * FROM ioo_user_progress WHERE user_id = $1 AND node_id = $2",
        str(user_id),
        uid,
    )

    return {
        **dict(node),
        "next_nodes": [dict(e) for e in edges],
        "my_progress": dict(progress) if progress else None,
    }


# ---------------------------------------------------------------------------
# Surfaces
# ---------------------------------------------------------------------------

@router.post("/surfaces/lifecycle/sweep")
async def lifecycle_sweep(
    user_id: str = Depends(get_current_user_id),
):
    """Run a lifecycle sweep over all non-killed surfaces (admin utility)."""
    mgr = SurfaceLifecycleManager()
    try:
        summary = await mgr.run_lifecycle_sweep()
    except Exception as e:
        logger.error(f"Lifecycle sweep failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Lifecycle sweep failed")
    return summary


@router.post("/surfaces/{node_id}")
async def spawn_surface(
    node_id: str,
    body: SurfaceSpawnRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Spawn an Ora-generated surface for a node."""
    try:
        nid = UUID(node_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid node ID")

    node = await fetchrow("SELECT * FROM ioo_nodes WHERE id = $1", nid)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    title = body.title or node["title"]

    # Use SurfaceGenerator to produce a rich spec
    gen = SurfaceGenerator()
    spec = gen.generate_spec(dict(node))

    # surface_type comes from the generator's template pick
    surface_type = spec.get("template", body.surface_type)

    row = await fetchrow(
        """
        INSERT INTO ioo_surfaces (node_id, surface_type, title, spec)
        VALUES ($1, $2, $3, $4)
        RETURNING id, node_id, surface_type, title, spec, status, created_at
        """,
        nid,
        surface_type,
        title,
        spec,
    )
    return dict(row)


@router.get("/surfaces/{node_id}")
async def get_surfaces(
    node_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Get active surfaces for a node."""
    try:
        nid = UUID(node_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid node ID")

    rows = await fetch(
        """
        SELECT * FROM ioo_surfaces
        WHERE node_id = $1 AND status IN ('testing','active')
        ORDER BY created_at DESC
        """,
        nid,
    )
    return {"surfaces": [dict(r) for r in rows]}


@router.post("/surfaces/{surface_id}/interact")
async def record_interaction(
    surface_id: str,
    body: SurfaceInteractRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Record a surface interaction (view/interact/complete/goal_success)."""
    try:
        sid = UUID(surface_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid surface ID")

    surface = await fetchrow("SELECT id FROM ioo_surfaces WHERE id = $1", sid)
    if not surface:
        raise HTTPException(status_code=404, detail="Surface not found")

    if body.action == "view":
        await execute(
            "UPDATE ioo_surfaces SET view_count = view_count + 1, updated_at = NOW() WHERE id = $1",
            sid,
        )
        # Auto-kill if no interactions after kill_at_views
        row = await fetchrow(
            "SELECT view_count, interaction_count, kill_at_views FROM ioo_surfaces WHERE id = $1",
            sid,
        )
        if row and row["view_count"] >= row["kill_at_views"] and row["interaction_count"] == 0:
            await execute(
                "UPDATE ioo_surfaces SET status = 'killed', updated_at = NOW() WHERE id = $1",
                sid,
            )
    elif body.action == "interact":
        await execute(
            "UPDATE ioo_surfaces SET interaction_count = interaction_count + 1, updated_at = NOW() WHERE id = $1",
            sid,
        )
    elif body.action == "complete":
        await execute(
            "UPDATE ioo_surfaces SET completion_count = completion_count + 1, status = 'active', updated_at = NOW() WHERE id = $1",
            sid,
        )
    elif body.action == "goal_success":
        await execute(
            "UPDATE ioo_surfaces SET goal_success_count = goal_success_count + 1, updated_at = NOW() WHERE id = $1",
            sid,
        )

    return {"ok": True}


# ---------------------------------------------------------------------------
# Admin/Dev: seed
# ---------------------------------------------------------------------------

@router.post("/seed")
async def seed_nodes(user_id: str = Depends(get_current_user_id)):
    """Seed initial IOO nodes (idempotent). Available to all for Phase 1."""
    agent = get_graph_agent()
    result = await agent.seed_initial_nodes()
    return result


# ---------------------------------------------------------------------------
# (Legacy _generate_surface_spec removed — now handled by SurfaceGenerator)
# ---------------------------------------------------------------------------
