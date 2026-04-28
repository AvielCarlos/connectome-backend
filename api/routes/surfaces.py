"""
Surfaces API — Ora's WebSpawn endpoints.

Endpoints:
  POST   /api/surfaces/spawn              — spawn a new surface (explorer/sovereign only)
  GET    /api/surfaces/my                 — list all surfaces for the authenticated user
  GET    /api/surfaces/{surface_id}/data  — serve live data for a surface (owner-only)
  POST   /api/surfaces/{surface_id}/action — handle interactive surface actions
  DELETE /api/surfaces/{surface_id}       — retire a surface

Tier gating:
  - Free users → 402 with Ora's warm upgrade message
  - Explorer + Sovereign → full access
"""

import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from api.middleware import get_current_user_id
from api.tier_guard import get_user_tier
from ora.agents.web_spawn_agent import WebSpawnAgent
from ora.surface_registry import SurfaceRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/surfaces", tags=["surfaces"])

# ─── Request / Response models ────────────────────────────────────────────────

class SpawnRequest(BaseModel):
    """
    Open-ended surface spawn request.
    Ora figures out what to build — no surface_type needed.
    """
    request: str  # "I want to quit smoking", "Prep me for my YC interview", etc.


class ActionRequest(BaseModel):
    action_type: str          # e.g. "check_item", "update_metric", "submit_form"
    payload: Dict[str, Any]   # action-specific data


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_spawn_agent() -> WebSpawnAgent:
    """Lazy-load WebSpawnAgent with the current OraBrain OpenAI client."""
    try:
        from ora.brain import get_brain
        brain = get_brain()
        return WebSpawnAgent(openai_client=getattr(brain, "_openai", None))
    except Exception:
        return WebSpawnAgent()


def _tier_gate(tier: str) -> None:
    """
    Raise 402 if the user is on the free tier.
    Explorer and Sovereign users pass through.
    """
    if tier not in ("explorer", "sovereign"):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "premium_required",
                "message": (
                    "WebSpawn is available on Explorer and Sovereign plans. "
                    "Ora can build you a personalized page for any goal — "
                    "a tracker, a plan, a dashboard, whatever would genuinely help. "
                    "Upgrade to unlock it."
                ),
                "upgrade_url": "/api/payments/checkout",
                "tier_required": "explorer",
            },
        )


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.post("/spawn")
async def spawn_surface(
    body: SpawnRequest,
    user_id: str = Depends(get_current_user_id),
):
    """
    Spawn a new personalized web surface. Explorer/Sovereign tier required.

    Ora freely designs whatever page and API would best serve the user's request.
    No templates. No fixed types.

    Returns the surface URL and a 2-minute estimated ready time (Railway deploys
    the backing route in the background).
    """
    if not body.request or not body.request.strip():
        raise HTTPException(status_code=400, detail="Request cannot be empty")

    # Tier gate
    tier = await get_user_tier(user_id)
    _tier_gate(tier)

    agent = _get_spawn_agent()
    try:
        result = await agent.spawn_surface(
            user_id=user_id,
            request=body.request.strip(),
        )
    except Exception as e:
        logger.error(f"spawn_surface failed for user {user_id[:8]}: {e}")
        raise HTTPException(
            status_code=500,
            detail="Ora ran into a problem building your surface. Please try again in a moment.",
        )

    return result


@router.get("/my")
async def get_my_surfaces(
    user_id: str = Depends(get_current_user_id),
):
    """List all active surfaces spawned for the authenticated user."""
    registry = SurfaceRegistry()
    surfaces = await registry.get_user_surfaces(user_id)

    # Strip bulky generated source from listing (keep spec metadata only)
    stripped = []
    for s in surfaces:
        spec = s.get("spec", {})
        stripped.append({
            "id":           s["id"],
            "title":        s.get("title", ""),
            "description":  spec.get("description", ""),
            "inferred_type": s.get("surface_type", "custom"),
            "slug":         spec.get("slug", ""),
            "url":          f"https://avielcarlos.github.io/connectome-web/surfaces/{s['id']}",
            "api_endpoint": f"https://connectome-api-production.up.railway.app/api/surfaces/{s['id']}/data",
            "status":       s.get("status", "active"),
            "view_count":   s.get("view_count", 0),
            "created_at":   s.get("created_at", ""),
        })

    return {"surfaces": stripped, "count": len(stripped)}


@router.get("/{surface_id}/data")
async def get_surface_data(
    surface_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """
    Serve the surface spec + user-specific data. Owner-only.

    Returns everything the SurfacePage.tsx needs to render:
    - title, description, sections, inferred_type
    - user_data (mutable state stored in spec.user_data)
    """
    registry = SurfaceRegistry()
    surface = await registry.get_surface(surface_id)

    if not surface:
        raise HTTPException(status_code=404, detail="Surface not found")
    if surface.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Not your surface")
    if surface.get("status") == "retired":
        raise HTTPException(status_code=410, detail="This surface has been retired")

    # Increment view count (best-effort, non-blocking)
    import asyncio
    asyncio.create_task(registry.increment_view_count(surface_id))

    spec = surface.get("spec", {})
    return {
        "surface_id":   surface_id,
        "title":        surface.get("title", spec.get("title", "")),
        "description":  spec.get("description", ""),
        "inferred_type": surface.get("surface_type", "custom"),
        "sections":     spec.get("sections", []),
        "user_data":    spec.get("user_data", {}),
        "created_at":   surface.get("created_at", ""),
    }


@router.post("/{surface_id}/action")
async def surface_action(
    surface_id: str,
    body: ActionRequest,
    user_id: str = Depends(get_current_user_id),
):
    """
    Handle interactive actions on a surface.

    Supported action_types:
      check_item      — toggle a checklist item: { item_id, done }
      update_metric   — update a metric value:  { metric_id, value }
      update_step     — mark a step done/undone: { step_index, done }
      kanban_move     — move a card:             { card_id, from_col, to_col }
      form_submit     — generic form submission:  { fields: {...} }
      data_patch      — patch arbitrary user_data: { patch: {...} }
    """
    registry = SurfaceRegistry()
    surface = await registry.get_surface(surface_id)

    if not surface:
        raise HTTPException(status_code=404, detail="Surface not found")
    if surface.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Not your surface")

    spec       = surface.get("spec", {})
    user_data  = spec.get("user_data", {})
    action_type = body.action_type
    payload    = body.payload

    # Apply the action mutation to user_data
    updated_user_data = _apply_action(action_type, payload, user_data, spec)

    # Persist
    spec["user_data"] = updated_user_data
    await registry.update_spec(surface_id, spec)

    return {
        "ok":        True,
        "action":    action_type,
        "user_data": updated_user_data,
    }


@router.delete("/{surface_id}")
async def retire_surface(
    surface_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Retire (soft-delete) a surface and remove its GitHub files."""
    agent = _get_spawn_agent()
    try:
        await agent.retire_surface(surface_id=surface_id, user_id=user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"retire_surface {surface_id} failed: {e}")
        raise HTTPException(status_code=500, detail="Could not retire surface")

    return {"ok": True, "surface_id": surface_id, "status": "retired"}


# ─── Action handler ───────────────────────────────────────────────────────────

def _apply_action(
    action_type: str,
    payload: Dict[str, Any],
    user_data: Dict[str, Any],
    spec: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Apply a surface action mutation to user_data.
    Returns the updated user_data dict.
    """
    import copy
    data = copy.deepcopy(user_data)

    if action_type == "check_item":
        item_id = payload.get("item_id")
        done    = bool(payload.get("done", True))
        checks  = data.get("checks", {})
        if item_id is not None:
            checks[str(item_id)] = done
        data["checks"] = checks

    elif action_type == "update_metric":
        metric_id = payload.get("metric_id")
        value     = payload.get("value")
        metrics   = data.get("metrics", {})
        if metric_id is not None and value is not None:
            metrics[str(metric_id)] = value
        data["metrics"] = metrics

    elif action_type == "update_step":
        step_index = payload.get("step_index")
        done       = bool(payload.get("done", True))
        steps_done = data.get("steps_done", {})
        if step_index is not None:
            steps_done[str(step_index)] = done
        data["steps_done"] = steps_done

    elif action_type == "kanban_move":
        card_id  = str(payload.get("card_id", ""))
        to_col   = str(payload.get("to_col", ""))
        kanban   = data.get("kanban", {})
        kanban[card_id] = to_col
        data["kanban"] = kanban

    elif action_type == "form_submit":
        fields = payload.get("fields", {})
        submissions = data.get("form_submissions", [])
        from datetime import datetime, timezone
        submissions.append({
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "fields": fields,
        })
        data["form_submissions"] = submissions

    elif action_type == "data_patch":
        patch = payload.get("patch", {})
        data.update(patch)

    else:
        # Unknown action — store raw for forward compatibility
        events = data.get("_events", [])
        events.append({"type": action_type, "payload": payload})
        data["_events"] = events

    return data
