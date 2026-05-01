"""
Screens API Routes

Endpoints:
  POST /api/screens/next    — single next screen (original)
  POST /api/screens/batch   — prefetch multiple screens (new)
  POST /api/screens/save    — bookmark a screen for later (new)
  GET  /api/screens/:id     — retrieve a screen by ID
"""

import hashlib
import json
import logging
import random
import re
import uuid as _uuid_mod
from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from core.models import (
    ScreenRequest,
    ScreenResponse,
    ScreenSpec,
    FeedbackOverlay,
    ScreenMetadata,
    DomainType,
)
from core.config import settings
from api.middleware import get_current_user_id
from ora.brain import get_brain
from ora.user_model import get_daily_screen_count, increment_daily_screen_count
from core.database import fetch, fetchrow, execute
from core.geo import get_location_for_ip, geo_to_context_hints
from uuid import UUID

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/screens", tags=["screens"])


# ---------------------------------------------------------------------------
# IOO Graph helpers — smart feed routing
# ---------------------------------------------------------------------------

async def _get_user_has_goals(user_id: str) -> bool:
    """Return True if the user has at least one active goal."""
    row = await fetchrow(
        "SELECT id FROM goals WHERE user_id = $1 AND status = 'active' LIMIT 1",
        str(user_id),
    )
    return row is not None


def _node_text(node: dict) -> str:
    return " ".join(str(node.get(k) or "") for k in ["title", "description", "type", "domain", "goal_category", "step_type"]).lower()


def _screen_pattern_for_ioo_node(node: dict) -> str:
    """Choose a UI pattern based on node semantics, not a single generic card."""
    text = _node_text(node)
    node_type = str(node.get("type") or node.get("step_type") or "").lower()
    domain = str(node.get("domain") or "").lower()
    if any(k in text for k in ["travel", "event", "restaurant", "date", "walk", "adventure", "place", "city", "trip"]):
        return "experience_plan"
    if any(k in text for k in ["money", "income", "job", "client", "business", "revenue", "work", "contribution", "volunteer"]):
        return "opportunity_pipeline"
    if any(k in text for k in ["health", "fitness", "energy", "sleep", "diet", "body", "breath", "workout", "nervous"]):
        return "capacity_protocol"
    if any(k in text for k in ["learn", "skill", "course", "practice", "study", "write", "create", "music", "code"]):
        return "skill_sprint"
    if any(k in text for k in ["clarify", "choose", "decide", "reflect", "intention", "goal"]):
        return "decision_canvas"
    if any(k in node_type for k in ["subroutine", "prerequisite", "bridge"]):
        return "bridge_node"
    if domain == "aventi":
        return "experience_plan"
    if domain == "eviva":
        return "opportunity_pipeline"
    if domain == "ivive":
        return "capacity_protocol"
    return "adaptive_canvas"


def _ioo_pattern_components(node: dict, pattern: str) -> list[dict]:
    title = node.get("title", "")
    desc = node.get("description") or ""
    time = node.get("requires_time_hours")
    money = node.get("requires_finances")
    prereqs = node.get("prerequisites") or node.get("requirements") or []
    if isinstance(prereqs, str):
        prereqs = [prereqs]

    base = [
        {"type": "pattern_badge", "text": pattern.replace("_", " ").upper(), "color": "#00d4aa"},
        {"type": "headline", "text": title},
        {"type": "body", "text": desc},
        _path_progression_component(kind="ioo_node", domain=node.get("domain"), current_stage="choose_path"),
    ]

    context = []
    if time:
        context.append({"label": "Time", "value": f"~{time}h"})
    if money:
        context.append({"label": "Money", "value": f"~${float(money):.0f}"})
    if node.get("difficulty_level"):
        context.append({"label": "Difficulty", "value": f"{node.get('difficulty_level')}/10"})
    if context:
        base.append({"type": "context_strip", "items": context})

    if pattern == "experience_plan":
        base += [
            {"type": "section_header", "text": "Experience variables"},
            {"type": "choice_grid", "items": [
                {"label": "Solo", "value": "solo"}, {"label": "With someone", "value": "social"},
                {"label": "Low cost", "value": "budget"}, {"label": "Peak aliveness", "value": "stretch"},
            ]},
            {"type": "timeline_steps", "items": [
                {"title": "Check fit", "body": "Time, budget, location, energy."},
                {"title": "Find option", "body": "Ora can scout real places/events."},
                {"title": "Commit", "body": "Book, invite, or schedule."},
            ]},
        ]
    elif pattern == "opportunity_pipeline":
        base += [
            {"type": "section_header", "text": "Opportunity pipeline"},
            {"type": "kanban_lite", "columns": [
                {"label": "Find", "items": ["Options", "People", "Signals"]},
                {"label": "Qualify", "items": ["Fit", "Reward", "Next ask"]},
                {"label": "Act", "items": ["Message", "Apply", "Deliver"]},
            ]},
        ]
    elif pattern == "capacity_protocol":
        base += [
            {"type": "section_header", "text": "Capacity protocol"},
            {"type": "readiness_meter", "items": ["Energy", "Time", "Body", "Focus"]},
            {"type": "timeline_steps", "items": [
                {"title": "Regulate", "body": "Lower friction first."},
                {"title": "Tiny action", "body": "Do the minimum viable version."},
                {"title": "Record signal", "body": "Ora updates your current state."},
            ]},
        ]
    elif pattern == "skill_sprint":
        base += [
            {"type": "section_header", "text": "Skill sprint"},
            {"type": "timeline_steps", "items": [
                {"title": "Target", "body": "Define the proof of skill."},
                {"title": "Practice loop", "body": "One focused repetition."},
                {"title": "Feedback", "body": "Ora or a human critiques output."},
                {"title": "Ship", "body": "Make one result real."},
            ]},
        ]
    elif pattern == "decision_canvas":
        base += [
            {"type": "section_header", "text": "Clarify before action"},
            {"type": "question_stack", "items": [
                "What would prove this worked?",
                "What constraint matters most?",
                "What can Ora do vs what must you do?",
            ]},
        ]
    else:
        base += [
            {"type": "section_header", "text": "Bridge node" if pattern == "bridge_node" else "Adaptive path"},
            {"type": "split_actions", "items": [
                {"owner": "You", "text": "Confirm reality: time, energy, money, access."},
                {"owner": "Ora", "text": "Map prerequisites, options, and next interface."},
            ]},
        ]

    if prereqs:
        base.append({"type": "constraint_panel", "items": [{"label": str(p), "status": "needed"} for p in prereqs[:5]]})

    base.append({
        "type": "action_button",
        "text": "Start this path →",
        "action": {"type": "open_url", "url": f"ioo://node/{node['id']}", "payload": {"node_id": str(node["id"]), "pattern": pattern}},
    })
    return base


def _ioo_node_to_screen_dict(node: dict) -> dict:
    """
    Convert an IOO graph node into a screen spec dict.
    The dict is JSON-serialisable and compatible with the ScreenSpec Pydantic model.
    """
    pattern = _screen_pattern_for_ioo_node(node)
    components = _ioo_pattern_components(node, pattern)
    return {
        "screen_id": str(_uuid_mod.uuid4()),
        "type": "opportunity",
        "layout": pattern,
        "components": components,
        "feedback_overlay": {
            "type": "star_rating",
            "position": "bottom_right",
            "always_visible": True,
        },
        "metadata": {
            "agent": "IOOGraphAgent",
            "source": "ioo_graph",
            "node_id": str(node["id"]),
            "node_type": node.get("type"),
            "screen_pattern": pattern,
            "pattern_version": "adaptive_v1",
            "path_progression": _path_progression_metadata(
                kind="ioo_node",
                domain=node.get("domain"),
                current_stage="choose_path",
                source_node_id=str(node["id"]),
            ),
            "domain": node.get("domain"),
            "tags": node.get("tags") or [],
        },
        "is_limited": False,
        "daily_limit": 999,
    }


async def _store_ioo_screen_spec(spec_dict: dict) -> str:
    """Persist an IOO-sourced screen spec in screen_specs and return its DB id."""
    metadata = spec_dict.get("metadata", {}) or {}
    node_id = metadata.get("node_id")
    screen_role = metadata.get("screen_role") or "recommend"
    node_uuid = None
    if node_id:
        try:
            node_uuid = UUID(str(node_id))
        except ValueError:
            node_uuid = None

    row = await fetchrow(
        """
        INSERT INTO screen_specs (spec, agent_type, domain, ioo_node_id, screen_role)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        json.dumps(spec_dict),
        metadata.get("agent") or "IOOGraphAgent",
        metadata.get("domain"),
        node_uuid,
        screen_role,
    )
    db_id = str(row["id"])

    # Every generated card should participate in the IOO neural graph, not live
    # only as disposable UI JSON. Existing IOO cards link to their node; real
    # action/fallback cards become screen_card/world-signal IOO nodes.
    try:
        from ora.agents.ioo_graph_agent import get_graph_agent

        await get_graph_agent().integrate_screen_spec(
            spec=spec_dict,
            screen_spec_id=db_id,
            agent_type=metadata.get("agent") or "IOOGraphAgent",
            domain=metadata.get("domain"),
        )
    except Exception as err:
        logger.debug("IOO screen integration skipped for screen %s: %s", db_id[:8], err)

    # TODO(ScreenGraph): generated IOO screens are pathway nodes, not throwaway
    # UI. This initial edge attaches the screen to its source IOO node; future
    # generation should also create leads_to/requires/clarifies/executes edges
    # between neighbouring generated screens and execution runs.
    if node_uuid:
        try:
            from ora.agents.screen_graph_agent import create_screen_edge

            await create_screen_edge(
                from_screen_id=db_id,
                relation_type="belongs_to_ioo_node",
                ioo_node_id=node_uuid,
                evidence={"source": "screens._store_ioo_screen_spec", "screen_role": screen_role},
            )
        except Exception as err:
            logger.debug("Screen graph edge creation skipped for screen %s: %s", db_id[:8], err)

    return db_id


# ---------------------------------------------------------------------------
# Path Feed progression graph — user-facing path mechanics
# ---------------------------------------------------------------------------

PATH_PROGRESSION_STAGES: list[dict[str, str]] = [
    {
        "id": "discover",
        "label": "Discover",
        "user_role": "Notice what feels alive or useful.",
        "aura_role": "Search the possibility graph and surface a candidate.",
    },
    {
        "id": "choose_domain",
        "label": "Focus",
        "user_role": "Choose iVive, Aventi, Eviva, or stay open.",
        "aura_role": "Filter and rebalance the feed around that domain.",
    },
    {
        "id": "choose_path",
        "label": "Choose path",
        "user_role": "Pick the route that feels worth testing.",
        "aura_role": "Compare options, prerequisites, reviews, timing, and fit.",
    },
    {
        "id": "confirm_micro_node",
        "label": "Confirm micro-node",
        "user_role": "Confirm the real action, time, budget, location, and energy.",
        "aura_role": "Collapse the card into a doable real-world step.",
    },
    {
        "id": "schedule_book_start",
        "label": "Commit",
        "user_role": "Schedule, book, invite, open, or start.",
        "aura_role": "Create calendar scaffolding, links, reminders, and fallback options.",
    },
    {
        "id": "complete_evidence",
        "label": "Do + prove",
        "user_role": "Do the thing and capture evidence or reflection.",
        "aura_role": "Update state, completion confidence, and graph weights.",
    },
    {
        "id": "learn_reroute",
        "label": "Learn / reroute",
        "user_role": "Rate whether it helped and what changed.",
        "aura_role": "Re-rank similar nodes and unlock the next pathway.",
    },
]


PATH_PROGRESSION_VARIANTS: dict[str, dict[str, Any]] = {
    "baseline": {
        "label": "Path progression",
        "mutation": "baseline",
        "description": "The standard feed → micro-node → evidence loop.",
        "stages": PATH_PROGRESSION_STAGES,
    },
    "trimmed_decision": {
        "label": "Fast path",
        "mutation": "trim",
        "description": "A shorter path for cards that already have a concrete link or booking action.",
        "stages": [
            PATH_PROGRESSION_STAGES[0],
            PATH_PROGRESSION_STAGES[3],
            PATH_PROGRESSION_STAGES[4],
            PATH_PROGRESSION_STAGES[5],
            PATH_PROGRESSION_STAGES[6],
        ],
    },
    "split_micro_node": {
        "label": "Mini-app path",
        "mutation": "split",
        "description": "Splits the micro-node into clarify → choose → commit for higher-friction opportunities.",
        "stages": [
            PATH_PROGRESSION_STAGES[0],
            PATH_PROGRESSION_STAGES[2],
            {
                "id": "clarify_constraints",
                "label": "Clarify",
                "user_role": "Confirm budget, location, timing, energy, and social context.",
                "aura_role": "Ask only the missing questions and hide system scaffolding.",
            },
            {
                "id": "choose_concrete_option",
                "label": "Choose option",
                "user_role": "Pick the exact page, provider, event, product, or service.",
                "aura_role": "Rank concrete options and keep the best links close.",
            },
            PATH_PROGRESSION_STAGES[4],
            PATH_PROGRESSION_STAGES[5],
            PATH_PROGRESSION_STAGES[6],
        ],
    },
    "grown_social": {
        "label": "Shared path",
        "mutation": "grow",
        "description": "Adds invite/share/follow-up nodes when belonging or contribution is part of the action.",
        "stages": [
            *PATH_PROGRESSION_STAGES[:5],
            {
                "id": "invite_or_share",
                "label": "Invite / share",
                "user_role": "Invite someone, share the opportunity, or make it communal.",
                "aura_role": "Suggest the right person, message, or group context without being pushy.",
            },
            PATH_PROGRESSION_STAGES[5],
            {
                "id": "follow_up",
                "label": "Follow up",
                "user_role": "Close the loop with a message, reflection, review, or next step.",
                "aura_role": "Capture outcomes and suggest the next relational/contribution node.",
            },
            PATH_PROGRESSION_STAGES[6],
        ],
    },
}


def _select_path_progression_variant(*, kind: str, domain: Optional[str], current_stage: str, source_node_id: Optional[str] = None) -> str:
    """Deterministically A/B test graph morphologies without needing client state."""
    normalized_domain = "iVive" if domain == "Rest" else (domain or "open")
    seed = f"{kind}:{normalized_domain}:{current_stage}:{source_node_id or ''}"
    bucket = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) % 100
    if normalized_domain == "Eviva" and bucket < 45:
        return "grown_social"
    if kind in {"real_world_action", "user_created_opportunity"} and bucket < 45:
        return "trimmed_decision"
    if bucket < 72:
        return "split_micro_node"
    return "baseline"


def _progression_stages_for_variant(variant: str) -> list[dict[str, str]]:
    return list(PATH_PROGRESSION_VARIANTS.get(variant, PATH_PROGRESSION_VARIANTS["baseline"])["stages"])


def _path_progression_metadata(
    *,
    kind: str,
    domain: Optional[str],
    current_stage: str = "confirm_micro_node",
    source_node_id: Optional[str] = None,
    variant: Optional[str] = None,
) -> dict[str, Any]:
    """Describe how this feed card participates in a morphable Path Feed graph.

    The graph can grow, split, or trim into testable variants of the same core
    pathway pattern. User responses can later promote winning variants per
    domain, card kind, city, user state, or opportunity source.
    """
    selected_variant = variant or _select_path_progression_variant(
        kind=kind, domain=domain, current_stage=current_stage, source_node_id=source_node_id
    )
    variant_def = PATH_PROGRESSION_VARIANTS.get(selected_variant, PATH_PROGRESSION_VARIANTS["baseline"])
    variant_stages = _progression_stages_for_variant(selected_variant)
    stage_ids = [stage["id"] for stage in variant_stages]
    if current_stage not in stage_ids:
        # Map canonical micro-node state to the closest equivalent inside the
        # currently tested morphology. This keeps cards comparable while letting
        # graph shapes diverge.
        if selected_variant == "split_micro_node":
            current_stage = "choose_concrete_option"
        elif selected_variant == "trimmed_decision":
            current_stage = "confirm_micro_node"
        else:
            current_stage = stage_ids[min(3, len(stage_ids) - 1)]
    current_index = stage_ids.index(current_stage) if current_stage in stage_ids else min(3, len(stage_ids) - 1)
    stages: list[dict[str, Any]] = []
    for index, stage in enumerate(variant_stages):
        status = "complete" if index < current_index else "active" if index == current_index else "upcoming"
        stages.append({**stage, "status": status})
    next_stage = variant_stages[min(current_index + 1, len(variant_stages) - 1)]["id"]
    return {
        "graph": "path_feed_progression_v2_morphable",
        "base_graph": "path_feed_progression_v1",
        "ab_test_id": "path_progression_morphology_v1",
        "variant": selected_variant,
        "variant_label": variant_def["label"],
        "mutation": variant_def["mutation"],
        "variant_description": variant_def["description"],
        "kind": kind,
        "domain": "iVive" if domain == "Rest" else domain,
        "current_stage": current_stage,
        "next_stage": next_stage,
        "source_node_id": source_node_id,
        "stages": stages,
        "principle": "The feed is a morphable neural graph: pathways can grow, split, trim, and be A/B tested while preserving the same fulfilment loop.",
        "promote_if": ["higher do-now rate", "more saved nodes become completed", "better post-action rating", "lower skip rate"],
        "prune_if": ["users skip before commit", "clarification adds friction", "links do not convert to action"],
    }


def _path_progression_component(*, kind: str, domain: Optional[str], current_stage: str = "confirm_micro_node", source_node_id: Optional[str] = None) -> dict[str, Any]:
    progression = _path_progression_metadata(kind=kind, domain=domain, current_stage=current_stage, source_node_id=source_node_id)
    return {
        "type": "path_progression",
        "text": progression["variant_label"],
        "current_stage": progression["current_stage"],
        "next_stage": progression["next_stage"],
        "variant": progression["variant"],
        "mutation": progression["mutation"],
        "ab_test_id": progression["ab_test_id"],
        "items": progression["stages"],
    }

# ---------------------------------------------------------------------------
# Real-world/actionable feed cards
# ---------------------------------------------------------------------------

_CURATED_REAL_ACTIONS: list[dict[str, Any]] = [
    {
        "key": "victoria_events_calendar",
        "domain": "Aventi",
        "city": "Victoria, BC",
        "tag": "victoria_live_events",
        "kind": "Victoria event source",
        "title": "Find a real Victoria event this week",
        "body": "Open Destination Greater Victoria’s live events calendar, choose something with a real page, and turn it into a Path node.",
        "image": "https://images.unsplash.com/photo-1492684223066-81342ee5ff30?w=1200&auto=format&fit=crop",
        "url": "https://www.tourismvictoria.com/events-calendar",
        "source_url": "https://www.tourismvictoria.com/events-calendar",
        "booking_url": "https://www.tourismvictoria.com/events-calendar",
        "map_url": "https://www.google.com/maps/search/?api=1&query=events+Victoria+BC",
        "button": "Browse Victoria events →",
        "needs": ["date window", "transport plan", "ticket/free RSVP"],
        "steps": [
            "Open the Victoria events calendar.",
            "Pick one event with a page, date, location, and cost you can verify.",
            "Book/RSVP, invite someone, or ask Aura to save it as a new opportunity node.",
        ],
        "why": "This is the Victoria-first opportunity layer: real local events with pages, not generic activity ideas.",
    },
    {
        "key": "victoria_recreation_programs",
        "domain": "iVive",
        "city": "Victoria, BC",
        "tag": "victoria_recreation",
        "kind": "Local class / booking",
        "title": "Book a Victoria recreation class or drop-in",
        "body": "City recreation pages expose classes, pool/fitness options, drop-ins, registration windows, prices, and facility details.",
        "image": "https://images.unsplash.com/photo-1517836357463-d25dfeac3438?w=1200&auto=format&fit=crop",
        "url": "https://www.victoria.ca/parks-recreation/recreation",
        "source_url": "https://www.victoria.ca/parks-recreation/recreation",
        "booking_url": "https://www.victoria.ca/parks-recreation/recreation",
        "provider_url": "https://www.victoria.ca/parks-recreation/recreation/crystal-pool-fitness-centre",
        "map_url": "https://www.google.com/maps/search/?api=1&query=Crystal+Pool+Fitness+Centre+Victoria+BC",
        "button": "Open recreation options →",
        "needs": ["schedule match", "registration/account", "class or drop-in fee"],
        "steps": [
            "Open the recreation page and check current registration/drop-in schedules.",
            "Choose one class, swim, workout, court booking, or community program.",
            "Register, add it to your calendar, or save it for Aura to resurface.",
        ],
        "why": "Aura should translate vitality goals into local bookable actions with real provider links.",
    },
    {
        "key": "victoria_volunteer_opportunities",
        "domain": "Eviva",
        "city": "Victoria, BC",
        "tag": "victoria_volunteer",
        "kind": "Community service",
        "title": "Find a meaningful Victoria volunteer role",
        "body": "Volunteer Victoria has real listings and advising so users can create contribution paths from actual local opportunities.",
        "image": "https://images.unsplash.com/photo-1559027615-cd4628902d4a?w=1200&auto=format&fit=crop",
        "url": "https://victoria.volunteerattract.com/Listings.php?ListType=Volunteer_Positions&MenuItemID=20",
        "source_url": "https://volunteervictoria.bc.ca",
        "booking_url": "https://victoria.volunteerattract.com/Listings.php?ListType=Volunteer_Positions&MenuItemID=20",
        "provider_url": "https://volunteervictoria.bc.ca",
        "button": "Browse volunteer roles →",
        "needs": ["cause fit", "time commitment", "application/contact step"],
        "steps": [
            "Open the listings and filter by cause, schedule, or organization.",
            "Pick one role that genuinely fits your life right now.",
            "Apply/contact them, or create a goal for Aura to help you follow through.",
        ],
        "why": "Eviva should route people into contribution, belonging, and purpose through real community openings.",
    },
    {
        "key": "victoria_makers_services",
        "domain": "Aventi",
        "city": "Victoria, BC",
        "tag": "victoria_local_makers",
        "kind": "Products / services / workshops",
        "title": "Discover Victoria makers, workshops, and local products",
        "body": "Use local maker and market pages as opportunity sources for gifts, workshops, services, and creative experiences.",
        "image": "https://images.unsplash.com/photo-1511988617509-a57c8a288659?w=1200&auto=format&fit=crop",
        "url": "https://www.tourismvictoria.com/blog/makers-markets",
        "source_url": "https://www.tourismvictoria.com/blog/makers-markets",
        "provider_url": "https://www.tourismvictoria.com/things-to-do/shopping/public-markets/victoria-market-collective",
        "map_url": "https://www.google.com/maps/search/?api=1&query=Victoria+BC+makers+markets+workshops",
        "button": "Open maker guide →",
        "needs": ["interest", "budget", "shop/workshop availability"],
        "steps": [
            "Open the maker guide and choose one shop, workshop, service, or product category.",
            "Follow through to the provider page when available.",
            "Ask Aura to turn the specific product/service/workshop into a saved opportunity node.",
        ],
        "why": "Opportunity indexing includes products and services when they unlock real experiences, not only events.",
    },
    {
        "key": "victoria_city_unlock",
        "domain": "Aventi",
        "city": "Victoria, BC",
        "tag": "city_unlock_pricing",
        "kind": "City unlock",
        "title": "Unlock the Victoria opportunity graph",
        "body": "Aura can start Victoria around a $500/month operating budget by indexing opportunities, not the whole web: live sources, targeted search, cached refreshes, and user-created nodes.",
        "image": "https://images.unsplash.com/photo-1514924013411-cbf25faa35bb?w=1200&auto=format&fit=crop",
        "url": "ido://city-unlock/victoria-bc",
        "button": "See city unlock economics →",
        "needs": ["Victoria coverage", "$500/month budget cap", "local members sharing cost"],
        "steps": [
            "Start with Victoria, BC and a hard monthly spend ceiling.",
            "Show users the estimated city cost and current member count.",
            "Reduce the per-user price as more locals join and the fixed city graph cost is shared.",
        ],
        "why": "The product should be honest: local intelligence has real API/search/refresh costs, but shared city economics can make it cheaper over time.",
    },
    {
        "key": "vancouver_city_unlock",
        "domain": "Aventi",
        "city": "Vancouver, BC",
        "tag": "city_unlock_pricing",
        "kind": "City unlock",
        "title": "Unlock the Vancouver opportunity graph",
        "body": "Aura can extend the local graph into Vancouver with a shared Victoria+Vancouver operating budget around $1,000/month: better event/source refreshes, more local developer channels, and richer real-world cards.",
        "image": "https://images.unsplash.com/photo-1560814304-4f05b62af116?w=1200&auto=format&fit=crop",
        "url": "ido://city-unlock/vancouver-bc",
        "button": "See Vancouver unlock economics →",
        "needs": ["Vancouver coverage", "$1,000/month two-city budget", "local members and builders sharing cost"],
        "steps": [
            "Run Victoria and Vancouver as the first BC corridor.",
            "Spend more on local opportunity refreshes and developer/community acquisition.",
            "Reduce per-user city pricing as more locals join the shared graph.",
        ],
        "why": "Vancouver adds density: more events, services, developers, AI builders, and early technical contributors for the BC pilot.",
    },
    {
        "key": "victoria_developer_contributor_path",
        "domain": "Eviva",
        "city": "Victoria, BC",
        "tag": "developer_contributor_recruiting",
        "kind": "Developer community",
        "title": "Help build Aura with Victoria developers",
        "body": "Victoria has real software and AI community channels. Use them to invite programmers into the local opportunity graph, CP issues, and AI life-OS build.",
        "image": "https://images.unsplash.com/photo-1517048676732-d65bc937f952?w=1200&auto=format&fit=crop",
        "url": "https://members.viatec.ca/tech-events",
        "source_url": "https://members.viatec.ca/tech-events",
        "provider_url": "https://www.meetup.com/openhack-victoria/",
        "booking_url": "https://www.meetup.com/openhack-victoria/",
        "map_url": "https://www.google.com/maps/search/?api=1&query=Victoria+BC+software+developer+meetup",
        "button": "Open Victoria tech channels →",
        "needs": ["developer-friendly pitch", "CP-ready issues", "local demo path"],
        "steps": [
            "Open VIATEC/OpenHack and identify a relevant event or organizer channel.",
            "Invite builders to test Aura locally and contribute real Victoria nodes/integrations.",
            "Point them to clear GitHub issues where successful PRs earn CP.",
        ],
        "why": "The best early users in a city may also become contributors, curators, and evangelists.",
    },
    {
        "key": "vancouver_developer_contributor_path",
        "domain": "Eviva",
        "city": "Vancouver, BC",
        "tag": "developer_contributor_recruiting",
        "kind": "Developer community",
        "title": "Recruit Vancouver AI builders into the Aura pilot",
        "body": "Vancouver has dense AI, LLM, startup, and developer communities. Market the pilot to builders who can use Aura and improve the graph.",
        "image": "https://images.unsplash.com/photo-1552664730-d307ca884978?w=1200&auto=format&fit=crop",
        "url": "https://www.meetup.com/vancouver-llm-ai-meetup/",
        "source_url": "https://vancouvercommunity.org/tech-startup/",
        "provider_url": "https://infervan.com",
        "booking_url": "https://www.eventbrite.ca/d/canada--vancouver/tech-meetup/",
        "map_url": "https://www.google.com/maps/search/?api=1&query=Vancouver+BC+AI+developer+meetup",
        "button": "Open Vancouver builder channels →",
        "needs": ["AI-builder positioning", "demo link", "contributor issue board"],
        "steps": [
            "Start with LLM/AI, TechVancouver, Infer, and Eventbrite tech meetups.",
            "Pitch Aura as the local AI life OS and opportunity graph for BC.",
            "Convert interested builders into users, node curators, and CP contributors.",
        ],
        "why": "Vancouver can supply the density of technical contributors needed to make the two-city pilot compound faster.",
    },
    {
        "key": "vancouver_live_events",
        "domain": "Aventi",
        "city": "Vancouver, BC",
        "tag": "live_events",
        "kind": "Live events",
        "title": "Find a live event in Vancouver this week",
        "body": "Browse real upcoming Eventbrite listings for Vancouver and choose something you can actually attend.",
        "image": "https://images.unsplash.com/photo-1501281668745-f7f57925c3b4?w=1200&auto=format&fit=crop",
        "url": "https://www.eventbrite.ca/d/canada--vancouver/events/",
        "button": "Browse live events →",
        "needs": ["calendar check", "transport plan", "ticket/free RSVP"],
        "steps": [
            "Open the live listings.",
            "Filter by date, price, and distance.",
            "Pick one real event and RSVP/book or save it for later.",
        ],
        "why": "The feed should expose the real world: gatherings, classes, culture, and local possibilities.",
    },
    {
        "key": "victoria_royal_bc_museum_visit",
        "domain": "Aventi",
        "city": "Victoria, BC",
        "tag": "museum_culture",
        "kind": "Culture / museum",
        "title": "Visit the Royal BC Museum",
        "body": "Turn a vague desire for culture into a concrete Victoria outing with live hours, exhibits, tickets, and a real place to go.",
        "image": "https://images.unsplash.com/photo-1564399579883-451a5d44ec08?w=1200&auto=format&fit=crop",
        "url": "https://royalbcmuseum.bc.ca/",
        "source_url": "https://royalbcmuseum.bc.ca/",
        "booking_url": "https://royalbcmuseum.bc.ca/visit",
        "provider_url": "https://royalbcmuseum.bc.ca/",
        "map_url": "https://www.google.com/maps/search/?api=1&query=Royal+BC+Museum+Victoria+BC",
        "button": "Check museum hours →",
        "needs": ["open hours", "ticket price", "downtown transport"],
        "steps": [
            "Open the museum site and check today’s hours/exhibits.",
            "Decide whether this is a solo reset, date, family outing, or creative inspiration trip.",
            "Go, book, or save the node for a specific day.",
        ],
        "why": "Dense opportunity graphs need actual institutions, exhibits, costs, and places — not generic ‘do something cultural’ cards.",
    },
    {
        "key": "victoria_crd_parks_walk",
        "domain": "iVive",
        "city": "Victoria, BC",
        "tag": "nature_walk",
        "kind": "Nature / vitality",
        "title": "Choose a CRD regional park walk",
        "body": "Use the CRD parks map to pick a real trail or beach walk that fits your energy, transport, and available time.",
        "image": "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee?w=1200&auto=format&fit=crop",
        "url": "https://www.crd.bc.ca/parks-recreation-culture/parks-trails/find-park-trail",
        "source_url": "https://www.crd.bc.ca/parks-recreation-culture/parks-trails/find-park-trail",
        "map_url": "https://www.google.com/maps/search/?api=1&query=CRD+regional+parks+Victoria+BC",
        "button": "Open CRD park finder →",
        "needs": ["weather", "transport", "30–120 minutes"],
        "steps": [
            "Open the CRD park finder.",
            "Choose one nearby trail/beach that matches your energy.",
            "Go for the walk and tell Aura whether it improved your state.",
        ],
        "why": "Aura should route vitality into low-friction real environments users can actually enter.",
    },
    {
        "key": "victoria_public_library_learning_session",
        "domain": "iVive",
        "city": "Victoria, BC",
        "tag": "learning_library",
        "kind": "Learning / public resource",
        "title": "Use the Greater Victoria Public Library for a focused learning block",
        "body": "Pick a branch, event, or digital resource and turn curiosity into a concrete 30–60 minute learning session.",
        "image": "https://images.unsplash.com/photo-1521587760476-6c12a4b040da?w=1200&auto=format&fit=crop",
        "url": "https://www.gvpl.ca/",
        "source_url": "https://www.gvpl.ca/",
        "provider_url": "https://www.gvpl.ca/locations/",
        "booking_url": "https://www.gvpl.ca/events/",
        "map_url": "https://www.google.com/maps/search/?api=1&query=Greater+Victoria+Public+Library",
        "button": "Open library resources →",
        "needs": ["topic", "branch or online access", "30–60 minutes"],
        "steps": [
            "Open GVPL and choose a branch, event, or digital resource.",
            "Pick one topic you want to learn today.",
            "Complete one focused block and record what you learned.",
        ],
        "why": "Skill and curiosity nodes should connect to real public infrastructure, not only paid courses.",
    },
    {
        "key": "victoria_startup_viatec_event",
        "domain": "Eviva",
        "city": "Victoria, BC",
        "tag": "startup_networking",
        "kind": "Builder / networking",
        "title": "Attend or track a VIATEC tech event",
        "body": "Use VIATEC’s event calendar to find local founder, software, startup, or tech-community entry points.",
        "image": "https://images.unsplash.com/photo-1515187029135-18ee286d815b?w=1200&auto=format&fit=crop",
        "url": "https://members.viatec.ca/tech-events",
        "source_url": "https://members.viatec.ca/tech-events",
        "booking_url": "https://members.viatec.ca/tech-events",
        "provider_url": "https://www.viatec.ca/",
        "map_url": "https://www.google.com/maps/search/?api=1&query=VIATEC+Victoria+BC",
        "button": "Open VIATEC events →",
        "needs": ["event fit", "intro message", "calendar slot"],
        "steps": [
            "Open the VIATEC tech events page.",
            "Pick one event or group that fits the Aura/Connectome mission.",
            "Attend, message an organizer, or save it as a contributor-recruiting path.",
        ],
        "why": "Eviva grows when contribution, opportunity, income, and community become real local pathways.",
    },
    {
        "key": "vancouver_science_world_visit",
        "domain": "Aventi",
        "city": "Vancouver, BC",
        "tag": "science_culture",
        "kind": "Culture / science",
        "title": "Visit Science World in Vancouver",
        "body": "A concrete science/culture outing with live exhibits, tickets, and a memorable place for curiosity and play.",
        "image": "https://images.unsplash.com/photo-1500534314209-a25ddb2bd429?w=1200&auto=format&fit=crop",
        "url": "https://www.scienceworld.ca/",
        "source_url": "https://www.scienceworld.ca/",
        "booking_url": "https://www.scienceworld.ca/visit-us/",
        "provider_url": "https://www.scienceworld.ca/",
        "map_url": "https://www.google.com/maps/search/?api=1&query=Science+World+Vancouver+BC",
        "button": "Check Science World →",
        "needs": ["ticket price", "transit", "2–3 hours"],
        "steps": [
            "Open the Science World site and check current exhibits/hours.",
            "Decide whether it is a solo curiosity trip, date, or friend/family outing.",
            "Book/go or save it for the Later feed.",
        ],
        "why": "The BC corridor graph needs real high-quality experiences across culture, learning, and aliveness.",
    },
    {
        "key": "vancouver_stanley_park_route",
        "domain": "iVive",
        "city": "Vancouver, BC",
        "tag": "nature_walk",
        "kind": "Nature / movement",
        "title": "Do a Stanley Park seawall walk or bike route",
        "body": "Use the park as a real vitality node: movement, ocean, forest, views, and a route you can complete.",
        "image": "https://images.unsplash.com/photo-1541336032412-2048a678540d?w=1200&auto=format&fit=crop",
        "url": "https://vancouver.ca/parks-recreation-culture/stanley-park.aspx",
        "source_url": "https://vancouver.ca/parks-recreation-culture/stanley-park.aspx",
        "map_url": "https://www.google.com/maps/search/?api=1&query=Stanley+Park+Seawall+Vancouver",
        "button": "Open Stanley Park info →",
        "needs": ["weather", "transit/parking", "45–180 minutes"],
        "steps": [
            "Open the city page/map and pick a route length.",
            "Walk or bike at a sustainable pace.",
            "Record whether it boosted energy, mood, or clarity.",
        ],
        "why": "Physical environment is part of the graph: parks and routes can be state-transition nodes.",
    },
    {
        "key": "meditation_beginner_youtube",
        "domain": "iVive",
        "tag": "guided_meditation",
        "kind": "Learnable video",
        "title": "Do a real 10-minute guided meditation",
        "body": "A verified beginner-friendly YouTube meditation you can follow immediately — not an abstract wellness prompt.",
        "image": "https://images.unsplash.com/photo-1506126613408-eca07ce68773?w=1200&auto=format&fit=crop",
        "url": "https://www.youtube.com/watch?v=S-W1GFBJbt0",
        "button": "Start the meditation →",
        "needs": ["10 minutes", "headphones optional", "somewhere you can sit"],
        "steps": [
            "Open the video and sit comfortably.",
            "Follow the breath/body cues without trying to be perfect.",
            "Afterward, rate whether this made you calmer or clearer.",
        ],
        "why": "Aura should help you regulate in reality. This gives you an existing practice to follow now.",
    },
    {
        "key": "yoga_beginner_youtube",
        "domain": "iVive",
        "tag": "yoga_video",
        "kind": "Learnable video",
        "title": "Follow a real beginner yoga class on YouTube",
        "body": "Yoga With Adriene’s beginner class is a real, existing video you can follow from home today.",
        "image": "https://images.unsplash.com/photo-1544367567-0f2fcb009e0b?w=1200&auto=format&fit=crop",
        "url": "https://www.youtube.com/watch?v=v7AYKMP6rOE",
        "button": "Open the yoga video →",
        "needs": ["20–25 minutes", "floor space", "mat optional"],
        "steps": [
            "Open the video and clear a small patch of floor.",
            "Follow at 70% intensity — the goal is momentum, not performance.",
            "Tell Aura if this was too easy, too hard, or just right.",
        ],
        "why": "A good feed should contain followable practices, not only conceptual opportunities.",
    },
    {
        "key": "vancouver_yoga_class",
        "domain": "Aventi",
        "city": "Vancouver, BC",
        "tag": "local_yoga_class",
        "kind": "Local class",
        "title": "Attend a Vancouver yoga class this week",
        "body": "YYOGA Downtown Flow has in-person classes and online booking. Check the live schedule and reserve a spot if the timing fits.",
        "image": "https://images.unsplash.com/photo-1575052814086-f385e2e2ad1b?w=1200&auto=format&fit=crop",
        "url": "https://yyoga.ca/locations/downtown-flow/",
        "button": "Check class schedule →",
        "needs": ["Vancouver access", "booking window", "class fee"],
        "steps": [
            "Open the live schedule.",
            "Pick one class within the next 7 days.",
            "Reserve it or save it for Aura to resurface.",
        ],
        "why": "Connectome should bridge inner vitality into real-world attendance and commitment.",
    },
    {
        "key": "vancouver_skydiving",
        "domain": "Aventi",
        "city": "Vancouver, BC",
        "tag": "adventure_skydiving",
        "kind": "Adventure booking",
        "title": "Book a real skydiving adventure near Vancouver",
        "body": "Skydive Vancouver operates near Abbotsford. It’s weather-dependent, so check availability before committing.",
        "image": "https://images.unsplash.com/photo-1521673461164-de300ebcfb17?w=1200&auto=format&fit=crop",
        "url": "https://www.vancouver-skydiving.bc.ca/book-now/",
        "button": "Check skydiving availability →",
        "needs": ["transport", "weather window", "booking budget"],
        "steps": [
            "Open the booking page and check current availability.",
            "Confirm weather, transport, and safety requirements.",
            "Book it, invite someone, or save it as a high-aliveness goal.",
        ],
        "why": "Aventi should surface peak-aliveness options, not just generic inspiration.",
    },
]


def _serialise_dt(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _has_cost_signal(item: dict[str, Any]) -> bool:
    cost_words = ("$", "price", "cost", "fee", "ticket", "budget", "paid", "booking")
    values = [item.get("price"), item.get("cost"), item.get("body"), item.get("description"), item.get("why"), *(item.get("needs") or [])]
    return any(any(word in str(value).lower() for word in cost_words) for value in values if value)


def _review_rating_component(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not _has_cost_signal(item):
        return None
    rating = item.get("review_rating") or item.get("rating")
    review_count = item.get("review_count") or item.get("reviews_count")
    review_url = item.get("review_url") or item.get("provider_url") or item.get("url")
    return {
        "type": "review_rating",
        "rating": float(rating) if rating is not None else None,
        "review_count": review_count,
        "source_url": review_url,
        "label": "Review rating",
        "note": "Cost-bearing opportunities should be checked against real reviews before booking or buying.",
    }


def _real_action_spec(item: dict[str, Any], *, source: str = "curated_real_action") -> dict:
    """Build a server-driven card around a real URL/action the user can take."""
    url = item.get("url") or ""
    title = item.get("title") or "Real-world action"
    domain = item.get("domain") or "Aventi"
    components = [
        {"type": "hero_image", "source": item.get("image") or item.get("image_url") or "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee?w=1200&auto=format&fit=crop", "alt": title},
        {"type": "category_badge", "text": f"{domain.upper()} · {item.get('kind', 'Real action')}", "color": "#f59e0b" if domain == "Aventi" else "#10b981"},
        {"type": "headline", "text": title},
        {"type": "body", "text": item.get("body") or item.get("description") or "A real option you can open, verify, and act on."},
        _path_progression_component(kind="real_world_action", domain=domain, current_stage="confirm_micro_node"),
        {"type": "context_strip", "items": [
            {"label": "Reality", "value": "Verified URL"},
            {"label": "Mode", "value": item.get("kind", "Action")},
            {"label": "Domain", "value": domain},
            *([{"label": "City", "value": item.get("city")}] if item.get("city") else []),
        ]},
    ]
    review_component = _review_rating_component(item)
    if review_component:
        components.append(review_component)
    if item.get("venue") or item.get("starts_at") or item.get("price"):
        components.append({
            "type": "constraint_panel",
            "items": [
                {"label": f"When: {_serialise_dt(item.get('starts_at'))}"} if item.get("starts_at") else None,
                {"label": f"Where: {item.get('venue')}"} if item.get("venue") else None,
                {"label": f"Price: {item.get('price')}"} if item.get("price") else None,
            ],
        })
        components[-1]["items"] = [x for x in components[-1]["items"] if x]
    links = [
        {"label": "Main page", "url": item.get("url"), "kind": "page"},
        {"label": "Source", "url": item.get("source_url"), "kind": "source"},
        {"label": "Book / register", "url": item.get("booking_url"), "kind": "booking"},
        {"label": "Provider", "url": item.get("provider_url"), "kind": "provider"},
        {"label": "Map", "url": item.get("map_url"), "kind": "map"},
        {"label": "Reviews", "url": item.get("review_url"), "kind": "reviews"},
    ]
    links = [dict(t) for t in {link["url"]: link for link in links if link.get("url")}.values()]
    if links:
        components.append({
            "type": "opportunity_links",
            "title": "Useful links",
            "items": links[:5],
            "note": "Aura keeps the page links close so users can verify, book, buy, register, navigate, or review before acting.",
        })
    components += [
        {"type": "section_header", "text": "Why this belongs here"},
        {"type": "body_text", "text": item.get("why") or "Aura is turning the feed into a path of concrete possibilities, not repeated generic cards."},
        {"type": "section_header", "text": "Do it like this"},
        {"type": "timeline_steps", "items": [{"title": step, "body": ""} for step in (item.get("steps") or [])[:4]]},
        {"type": "action_button", "label": item.get("button") or "Open option →", "action": {"type": "open_url", "url": url, "payload": {"source": source, "tag": item.get("tag"), "verified_url": bool(url)}}},
    ]
    return {
        "screen_id": str(_uuid_mod.uuid4()),
        "type": "activity",
        "layout": "real_world_action",
        "components": components,
        "feedback_overlay": {"type": "star_rating", "position": "bottom_right", "always_visible": True},
        "metadata": {
            "agent": "RealWorldActionAgent",
            "source": source,
            "domain": domain,
            "city": item.get("city"),
            "location_city": item.get("city"),
            "location_signal": f"city:{item.get('city')}" if item.get("city") else None,
            "opportunity_kind": item.get("kind"),
            "path_progression": _path_progression_metadata(kind="user_created_opportunity" if source == "user_created_opportunity" else "real_world_action", domain=domain, current_stage="confirm_micro_node"),
            "tags": [item.get("tag") or "real_action", "diversity_seed"],
            "url": url,
            "links": links,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "card_data": {
            "title": title,
            "body": item.get("body") or item.get("description"),
            "image_url": item.get("image") or item.get("image_url"),
            "url": url,
            "deep_dive": {
                "time_to_start": (item.get("needs") or ["open the link"])[0],
                "difficulty": "easy" if domain == "iVive" else "medium",
                "why_it_matters": item.get("why"),
                "steps": item.get("steps") or [],
                "resources": links or ([{"label": item.get("button") or "Open", "url": url}] if url else []),
                "stat": "Real URL/action card; user can verify and act.",
            },
        },
    }


class OpportunityCreateRequest(BaseModel):
    title: str
    body: Optional[str] = None
    kind: Optional[str] = "User-created opportunity"
    domain: Optional[DomainType] = "Aventi"
    city: Optional[str] = "Victoria, BC"
    url: Optional[str] = None
    source_url: Optional[str] = None
    booking_url: Optional[str] = None
    provider_url: Optional[str] = None
    map_url: Optional[str] = None
    price: Optional[str] = None
    review_rating: Optional[float] = None
    review_count: Optional[int] = None
    review_url: Optional[str] = None
    venue: Optional[str] = None


@router.post("/opportunities", response_model=ScreenResponse)
async def create_user_opportunity(
    body: OpportunityCreateRequest,
    user_id: str = Depends(get_current_user_id),
) -> ScreenResponse:
    """Create a user/Aura co-created opportunity card and persist it as an IOO screen spec.

    This is intentionally lightweight for the Victoria-first MVP: users can add
    events, products, services, bookings, providers, or local path nodes with
    the links needed to verify and act. Later this can graduate into a dedicated
    opportunity_nodes table with moderation and refresh jobs.
    """
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="title is required")
    item = body.dict()
    item.update({
        "key": f"user_opportunity_{_uuid_mod.uuid4().hex[:10]}",
        "tag": "user_created_opportunity",
        "button": "Open opportunity →" if (body.url or body.booking_url or body.provider_url) else "Save opportunity →",
        "needs": ["verify details", "choose timing", "commit or save"],
        "steps": [
            "Verify the linked page, provider, date/time, cost, and location where applicable.",
            "Decide whether this is worth doing, buying, booking, attending, or sharing.",
            "Save it to your path or act from the strongest link.",
        ],
        "why": "This was created with a user, so Aura can learn new local opportunities instead of only consuming indexed sources.",
        "image": "https://images.unsplash.com/photo-1518005020951-eccb494ad742?w=1200&auto=format&fit=crop",
    })
    tier = "free"
    daily_limit = 10
    spec = _real_action_spec(item, source="user_created_opportunity")
    spec["metadata"]["created_by_user_id"] = str(user_id)
    spec["metadata"]["validation_status"] = "user_submitted"
    spec["metadata"]["budget_model"] = "victoria_first_500_month_mvp"
    return await _build_screen_response_from_spec(user_id, tier, daily_limit, spec)


CITY_UNLOCK_MODELS: dict[str, dict[str, Any]] = {
    "victoria": {
        "city": "Victoria, BC",
        "estimated_monthly_cost": 425,
        "budget_cap": 500,
        "focus": ["events", "recreation/classes", "volunteering", "makers/services", "local developer channels"],
    },
    "vancouver": {
        "city": "Vancouver, BC",
        "estimated_monthly_cost": 575,
        "budget_cap": 650,
        "focus": ["events", "AI/developer meetups", "startup/community channels", "services/products", "bookings"],
    },
}


@router.get("/city-unlock")
async def get_city_unlock(city: str = "Victoria + Vancouver, BC") -> dict[str, Any]:
    """Transparent city-unlock economics for the two-city local opportunity graph MVP."""
    city_key = city.lower()
    if "vancouver" in city_key and "victoria" not in city_key:
        selected = [CITY_UNLOCK_MODELS["vancouver"]]
    elif "victoria" in city_key and "vancouver" not in city_key:
        selected = [CITY_UNLOCK_MODELS["victoria"]]
    else:
        selected = [CITY_UNLOCK_MODELS["victoria"], CITY_UNLOCK_MODELS["vancouver"]]
    estimated_monthly_cost = sum(int(c["estimated_monthly_cost"]) for c in selected)
    target_margin = 1.15
    example_members = [50, 100, 250, 500]
    return {
        "city": " + ".join(c["city"] for c in selected),
        "currency": "CAD",
        "estimated_monthly_cost": estimated_monthly_cost,
        "budget_cap": max(1000, sum(int(c["budget_cap"]) for c in selected)) if len(selected) > 1 else selected[0]["budget_cap"],
        "cities": selected,
        "coverage": ["events", "classes", "services", "products", "volunteering", "bookings", "developer/community channels", "user-created nodes"],
        "operating_model": [
            "index opportunities, not the whole web",
            "prefer official/provider/event/booking/community pages",
            "cache and refresh selectively inside a hard monthly budget",
            "allocate part of the budget to developer/programmer acquisition in Victoria and Vancouver",
            "let users and Aura create missing opportunity nodes together",
            "reward verified local nodes and shipped code with CP",
        ],
        "budget_split": {
            "opportunity_refresh_and_search": 600,
            "developer_programmer_marketing": 250,
            "local_community_experiments": 100,
            "buffer_monitoring_overages": 50,
        } if len(selected) > 1 else None,
        "shared_price_examples": [
            {"local_members": n, "estimated_price_per_user": round((estimated_monthly_cost * target_margin) / n, 2)}
            for n in example_members
        ],
        "message": "Unlocking a city corridor has real source/search/refresh and community-acquisition costs. The per-user price can go down as more Victoria and Vancouver locals share the graph cost.",
    }


async def _build_screen_response_from_spec(user_id: str, tier: str, daily_limit: int, spec_dict: dict) -> ScreenResponse:
    db_id = await _store_ioo_screen_spec(spec_dict)
    screens_today = await increment_daily_screen_count(user_id)
    return ScreenResponse(
        screen=ScreenSpec(
            screen_id=spec_dict["screen_id"],
            type=spec_dict.get("type", "activity"),
            layout=spec_dict.get("layout", "card_stack"),
            components=spec_dict.get("components", []),
            feedback_overlay=FeedbackOverlay(**spec_dict.get("feedback_overlay", {"type": "star_rating", "position": "bottom_right", "always_visible": True})),
            metadata=ScreenMetadata(**spec_dict.get("metadata", {"agent": "RealWorldActionAgent"})),
        ),
        screen_spec_db_id=db_id,
        screens_today=screens_today,
        daily_limit=daily_limit,
        is_limited=(tier == "free"),
    )


async def _try_live_event_action(user_id: str, tier: str, daily_limit: int) -> Optional[ScreenResponse]:
    """Turn a stored live event into a feed card when the user's city has events.

    If the city pipeline is empty, do one best-effort sync inline so real future
    events can appear directly in the feed instead of waiting for a separate
    cron/admin sync. This stays read-only toward event platforms and only stores
    public event metadata.
    """
    try:
        from ora.agents.event_agent import EventAgent

        agent = EventAgent()
        city = await _user_city(user_id)
        events = await agent.get_recommended_events(user_id=str(user_id), days_ahead=14, limit=8)
        events = [ev for ev in events if ev.get("url")]
        if not events and city:
            try:
                await agent.sync_city(city, force=False)
            except Exception as sync_err:
                logger.debug("Live event feed sync skipped for %s/%s: %s", str(user_id)[:8], city, sync_err)
            events = await agent.get_events_for_city(city=city, days_ahead=14, limit=8)
            events = [ev for ev in events if ev.get("url")]
        if not events:
            return None
        event = events[0] if len(events) <= 2 else random.choice(events[: min(5, len(events))])
        item = {
            "domain": "Aventi",
            "tag": "live_event",
            "kind": "Live event",
            "title": event.get("title") or "Local event",
            "body": event.get("description") or "A local live event Aura found from the events pipeline.",
            "image_url": event.get("image_url"),
            "url": event.get("url"),
            "button": "Open event details →",
            "venue": event.get("venue_name") or event.get("address") or event.get("city"),
            "starts_at": event.get("starts_at"),
            "price": event.get("price_range"),
            "steps": ["Open the event page.", "Check date, location, cost, and transport.", "Book, invite someone, or save it for later."],
            "why": "This is the live Aventi layer: events you can actually attend, not conceptual prompts.",
        }
        return await _build_screen_response_from_spec(user_id, tier, daily_limit, _real_action_spec(item, source="live_event"))
    except Exception as err:
        logger.debug("Live event action card skipped for user %s: %s", str(user_id)[:8], err)
        return None


async def _user_city(user_id: str) -> Optional[str]:
    """Return the user's explicit/live city when available. IP geo is only fallback elsewhere."""
    try:
        row = await fetchrow(
            """
            SELECT COALESCE(NULLIF(u.city, ''), NULLIF(s.location_city, '')) AS city,
                   NULLIF(s.location_country, '') AS country
            FROM users u
            LEFT JOIN ioo_user_state s ON s.user_id = u.id
            WHERE u.id = $1
            """,
            str(user_id),
        )
        if not row or not row.get("city"):
            return None
        city = str(row["city"]).strip()
        country = str(row.get("country") or "").strip()
        if country and country.lower() not in city.lower():
            return f"{city}, {country}"
        return city
    except Exception:
        return None


def _city_token(value: Optional[str]) -> str:
    """Extract a robust city token from values like 'Victoria, Canada'."""
    if not value:
        return ""
    token = str(value).lower().split(",")[0]
    token = re.sub(r"(city of|near)", "", token)
    token = re.sub(r"[^a-z0-9]+", " ", token).strip()
    return token


def _item_location_text(item: dict[str, Any]) -> str:
    return " ".join(
        str(item.get(key) or "")
        for key in ("city", "title", "body", "url", "source_url", "provider_url", "booking_url", "map_url")
    ).lower()


def _city_rank(item: dict[str, Any], preferred_city: Optional[str]) -> int:
    if not preferred_city:
        return 0
    preferred = _city_token(preferred_city)
    if not preferred:
        return 0
    item_city = _city_token(item.get("city"))
    if item_city:
        if preferred == item_city or preferred in item_city or item_city in preferred:
            return 0
        # Wrong-city physical opportunities should be ineligible for a user
        # with a known location. This is a hard filter upstream, not just a
        # ranking hint.
        return 4

    # Defensive fallback for older curated cards that forgot to set `city` but
    # clearly mention a city in title/body/URLs (e.g. Vancouver yoga). Known user
    # location must still win over those legacy cards.
    text = _item_location_text(item)
    known_local_markers = {
        "victoria": ("victoria", "greater victoria", "gvpl", "viatec"),
        "vancouver": ("vancouver", "stanley park", "science world", "yyoga", "abbotsford"),
    }
    for city_name, markers in known_local_markers.items():
        if any(marker in text for marker in markers):
            return 0 if city_name == preferred else 4
    return 1


def _filter_location_eligible(candidates: list[dict[str, Any]], preferred_city: Optional[str]) -> list[dict[str, Any]]:
    """Hard-filter wrong-city local opportunities when user location is known."""
    if not preferred_city:
        return candidates
    eligible = [item for item in candidates if _city_rank(item, preferred_city) <= 1]
    return eligible or [item for item in candidates if not item.get("city")]


async def _try_real_world_card(user_id: str, tier: str, daily_limit: int, slot_index: int = 0, domain_filter: Optional[str] = None, preferred_city: Optional[str] = None) -> Optional[ScreenResponse]:
    """Diversify the main feed with concrete learn/attend/book actions."""
    # Prefer true live event data for the first Aventi slot, then fall back to
    # curated verified URLs so the feed is never only generic Eviva cards.
    if slot_index % 5 == 0 and (domain_filter in (None, '', 'Aventi')):
        live = await _try_live_event_action(user_id, tier, daily_limit)
        if live is not None:
            return live

    current_count = await get_daily_screen_count(user_id)
    rotation_bucket = datetime.now(timezone.utc).strftime("%Y-%m-%d:%H")
    digest = hashlib.sha256(f"{user_id}:{rotation_bucket}:{current_count}".encode("utf-8")).hexdigest()
    candidates = [item for item in _CURATED_REAL_ACTIONS if not domain_filter or item.get('domain') == domain_filter or (domain_filter == 'iVive' and item.get('domain') == 'Rest')]
    if not candidates:
        candidates = _CURATED_REAL_ACTIONS
    if preferred_city:
        candidates = _filter_location_eligible(candidates, preferred_city)
        candidates = sorted(candidates, key=lambda item: (_city_rank(item, preferred_city), item.get("key", "")))
    start = int(digest[:8], 16) % len(candidates)
    idx = (start + slot_index + current_count) % len(candidates)
    item = dict(candidates[idx])
    if domain_filter == 'iVive' and item.get('domain') == 'Rest':
        item['domain'] = 'iVive'
        item['tag'] = item.get('tag') or 'recovery'
    return await _build_screen_response_from_spec(user_id, tier, daily_limit, _real_action_spec(item))


_STATIC_FALLBACK_CARDS = [
    {
        "title": "Take a 10-minute Aventi walk",
        "image": "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee?w=1200&auto=format&fit=crop",
        "body": "A tiny real-world exploration: step outside, choose one direction, and let curiosity pick the next turn. Notice one place, person, event poster, cafe, path, or view you could return to later.",
        "domain": "Aventi",
        "tag": "exploration",
        "button": "Start the walk →",
        "why": "Aventi grows aliveness through lived experience. This is deliberately small so it works even when motivation is low.",
        "steps": [
            "Check your energy and weather; keep it easy.",
            "Walk for 10 minutes with no productivity goal.",
            "Save one thing you noticed as a possible future experience.",
        ],
        "needs": ["10 minutes", "safe place to walk", "curiosity"],
        "result": "Ora learns what kinds of places and experiences make you feel more alive.",
    },
    {
        "title": "Reset your system with iVive",
        "image": "https://images.unsplash.com/photo-1506126613408-eca07ce68773?w=1200&auto=format&fit=crop",
        "body": "A nervous-system reset for when your body is carrying friction. Drink water, loosen your jaw, drop your shoulders, and take five slow breaths before choosing the next task.",
        "domain": "iVive",
        "tag": "vitality",
        "button": "Do the reset →",
        "why": "iVive is about maintaining and growing the self. A regulated body makes better decisions and lowers the cost of starting.",
        "steps": [
            "Drink water or take one sip if that is all you can do.",
            "Relax jaw, shoulders, hands, and belly.",
            "Take five slow breaths, then ask: what is the smallest next step?",
        ],
        "needs": ["1–3 minutes", "water if available", "somewhere to pause"],
        "result": "Ora learns whether you need restoration before action.",
    },
    {
        "title": "Send one Eviva spark",
        "image": "https://images.unsplash.com/photo-1529156069898-49953e39b3ac?w=1200&auto=format&fit=crop",
        "body": "A contribution-through-connection action: send one person a real appreciation, useful intro, helpful link, or warm check-in. Keep it one sentence and make it genuine.",
        "domain": "Eviva",
        "tag": "connection",
        "button": "Send appreciation →",
        "why": "Eviva grows contribution, relationships, and service. Small signals of care can reopen human pathways without needing a big project.",
        "steps": [
            "Choose one person who would genuinely benefit from warmth or recognition.",
            "Write one specific sentence — no performance, no ask.",
            "Send it, or save it if now is not socially appropriate.",
        ],
        "needs": ["one person", "one sentence", "phone or messaging app"],
        "result": "Ora learns which relationships and contribution channels are alive for you.",
    },
    {
        "title": "Choose Rest on purpose",
        "image": "https://images.unsplash.com/photo-1495195134817-aeb325a55b65?w=1200&auto=format&fit=crop",
        "body": "A deliberate recovery micro-step: put your phone down for three minutes and let your nervous system learn that nothing needs to be chased right now.",
        "domain": "Rest",
        "tag": "recovery",
        "button": "Begin rest →",
        "why": "Rest is the substrate under iVive, Eviva, and Aventi. Sometimes the best next action is reducing load before adding direction.",
        "steps": [
            "Put the phone face down or away from your hand.",
            "Let your eyes rest on one still object.",
            "After three minutes, choose whether to continue resting or return to action.",
        ],
        "needs": ["3 minutes", "quiet enough space", "permission to pause"],
        "result": "Ora learns when your next best step is recovery, not more input.",
    },
]


async def _static_fallback_card(
    user_id: str,
    tier: str,
    daily_limit: int,
    reason: str,
) -> ScreenResponse:
    """Build a deterministic local card so the main feed never appears empty."""
    # Deterministic across process restarts so repeated failures do not create
    # a chaotic feed. These are seed cards, not the long-term feed model: IOO
    # graph generation should remain the primary path and learn from responses.
    digest = hashlib.sha256(f"{user_id}:{datetime.now(timezone.utc).date()}".encode("utf-8")).hexdigest()
    idx = int(digest[:8], 16) % len(_STATIC_FALLBACK_CARDS)
    item = _STATIC_FALLBACK_CARDS[idx]
    screen_id = str(_uuid_mod.uuid4())
    spec_dict = {
        "screen_id": screen_id,
        "type": "activity",
        "layout": "card_stack",
        "components": [
            {"type": "hero_image", "source": item["image"], "alt": item["title"]},
            {"type": "category_badge", "text": item["domain"].upper(), "color": "#00d4aa"},
            {"type": "headline", "text": item["title"]},
            {"type": "body", "text": item["body"]},
            _path_progression_component(kind="fallback_action", domain=item["domain"], current_stage="confirm_micro_node"),
            {"type": "section_header", "text": "What this is"},
            {"type": "body_text", "text": item["why"]},
            {"type": "section_header", "text": "Needs"},
            {"type": "body_text", "text": ", ".join(item["needs"])},
            {
                "type": "action_button",
                "label": item["button"],
                "action": {
                    "type": "open_url",
                    "url": f"ido://fallback/{item['tag']}",
                    "payload": {"tag": item["tag"], "source": "static_fallback"},
                },
            },
        ],
        "feedback_overlay": {"type": "star_rating", "position": "bottom_right", "always_visible": True},
            "metadata": {
            "agent": "StaticFallbackAgent",
            "source": "static_fallback",
            "domain": "iVive" if item["domain"] == "Rest" else item["domain"],
            "path_progression": _path_progression_metadata(kind="fallback_action", domain=item["domain"], current_stage="confirm_micro_node"),
            "tags": [item["tag"], "mvp_stability"],
            "fallback_reason": reason,
            "ioo_execution_status": "pending_user_response",
            "ioo_learning_event": "fallback_card_shown",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "card_data": {
            "title": item["title"],
            "body": item["body"],
            "image_url": item["image"],
            "deep_dive": {
                "time_to_start": item["needs"][0],
                "difficulty": "easy",
                "why_it_matters": item["why"],
                "steps": item["steps"],
                "resources": [],
                "stat": item["result"],
            },
        },
        "deep_dive": {
            "time_to_start": item["needs"][0],
            "difficulty": "easy",
            "why_it_matters": item["why"],
            "steps": item["steps"],
            "resources": [],
            "stat": item["result"],
        },
    }

    # TODO(IOO): when the user chooses "do now", trigger the IOO Execution
    # Protocol for this fallback action. "do later" should schedule/resurface,
    # and "not interested" should become a graph-learning signal/refinement.
    # TODO(ScreenGraph): once fallback cards are mapped to IOO candidate nodes,
    # create screen_graph_edges here too so Do Now / Do Later / Not Interested
    # can adjust user-specific pathway weights instead of only aggregate ratings.

    try:
        db_id = await _store_ioo_screen_spec(spec_dict)
    except Exception as err:
        db_id = screen_id
        logger.error("Static fallback card persistence failed for user %s: %s", user_id[:8], err, exc_info=True)

    try:
        screens_today = await increment_daily_screen_count(user_id)
    except Exception as err:
        screens_today = await get_daily_screen_count(user_id)
        logger.warning("Static fallback count increment failed for user %s: %s", user_id[:8], err)

    logger.warning("Using static fallback screen for user %s: %s", user_id[:8], reason)

    return ScreenResponse(
        screen=ScreenSpec(
            screen_id=spec_dict["screen_id"],
            type=spec_dict["type"],
            layout=spec_dict["layout"],
            components=spec_dict["components"],
            feedback_overlay=FeedbackOverlay(**spec_dict["feedback_overlay"]),
            metadata=ScreenMetadata(**spec_dict["metadata"]),
        ),
        screen_spec_db_id=db_id,
        screens_today=screens_today,
        daily_limit=daily_limit,
        is_limited=False,
    )


async def _try_ioo_card(
    user_id: str,
    goal_id: Optional[str],
    tier: str,
    daily_limit: int,
) -> Optional[ScreenResponse]:
    """
    Attempt to build one IOO graph-sourced card.
    Returns a ScreenResponse on success, None if the graph has nothing to offer.
    """
    try:
        from ora.agents.ioo_graph_agent import get_graph_agent as _get_ioo
        _ioo = _get_ioo()
        if goal_id:
            # Goal-specific: recommend nodes aligned to that goal
            nodes = await _ioo.recommend_next_nodes(
                user_id=str(user_id),
                goal_id=goal_id,
                limit=8,
            )
        else:
            # Discovery mode: surface diverse nodes from across all domains
            # Mix: 60% personalised (vector recommend) + 40% random exploration
            if random.random() < 0.6:
                try:
                    nodes = await _ioo.vector_recommend(
                        user_id=str(user_id),
                        goal_context="life improvement discovery exploration",
                        limit=10,
                        preference="mixed",
                    )
                except Exception:
                    nodes = await _ioo.recommend_next_nodes(user_id=str(user_id), goal_id=None, limit=8)
            else:
                # Pure exploration: pick from any domain the user hasn't seen recently
                nodes = await _ioo.recommend_next_nodes(user_id=str(user_id), goal_id=None, limit=8)

        if not nodes:
            return None

        # Semi-random selection with slight weighting toward higher-difficulty nodes
        # (easier nodes are boring; discovery should stretch people slightly)
        weights = [1 + (n.get('difficulty_level', 5) * 0.1) for n in nodes]
        total = sum(weights)
        r = random.random() * total
        cumulative = 0
        node = nodes[-1]
        for n, w in zip(nodes, weights):
            cumulative += w
            if r <= cumulative:
                node = n
                break
        spec_dict = _ioo_node_to_screen_dict(node)
        db_id = await _store_ioo_screen_spec(spec_dict)
        screens_today = await increment_daily_screen_count(user_id)

        screen = ScreenSpec(
            screen_id=spec_dict["screen_id"],
            type=spec_dict["type"],
            layout=spec_dict["layout"],
            components=spec_dict["components"],
            feedback_overlay=FeedbackOverlay(**spec_dict["feedback_overlay"]),
            metadata=ScreenMetadata(**spec_dict["metadata"]),
        )
        return ScreenResponse(
            screen=screen,
            screen_spec_db_id=db_id,
            screens_today=screens_today,
            daily_limit=daily_limit,
            is_limited=(tier == "free"),
        )
    except Exception as _err:
        logger.warning(f"IOO card build failed, falling back to brain: {_err}")
        return None


def _client_ip(request: Request) -> str:
    """Extract real client IP, respecting X-Forwarded-For from reverse proxy."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.post("/next", response_model=ScreenResponse)
async def get_next_screen(
    body: ScreenRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    """
    Request the next screen from Ora.
    MVP/stability: preserves auth but does not hard-block the main feed on
    daily limits. Paywall enforcement can return after feed stability is proven.
    """
    # Check subscription tier via PricingAgent-backed tier guard
    from api.tier_guard import check_tier_limit, get_user_tier, build_upgrade_card, get_current_usage
    from ora.agents.pricing_agent import get_pricing_agent

    tier = await get_user_tier(user_id)
    pricing_agent = get_pricing_agent()
    limits = await pricing_agent.get_tier_limits(tier)
    configured_daily_limit = limits.get("daily_screens", 10)
    daily_limit = configured_daily_limit if configured_daily_limit == -1 else max(configured_daily_limit, 999)

    # Get current count BEFORE incrementing (brain will increment)
    current_count = await get_daily_screen_count(user_id)

    if False and daily_limit != -1 and current_count >= daily_limit:
        # Return Ora's warm upgrade card instead of a cold 402
        upgrade_card = await build_upgrade_card("daily_screens", daily_limit, tier)
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "daily_limit_reached",
                "screens_today": current_count,
                "daily_limit": daily_limit,
                "tier": tier,
                "upgrade_card": upgrade_card,
                "upgrade_url": "/api/payments/checkout",
            },
            headers={"X-Upgrade-URL": "/api/payments/checkout"},
        )

    brain = get_brain()

    # Geo context — resolve IP asynchronously, inject into brain call
    geo_hints = {}
    try:
        ip = _client_ip(request)
        geo = await get_location_for_ip(ip)
        geo_hints = geo_to_context_hints(geo)
    except Exception:
        pass

    preferred_city = await _user_city(user_id)

    # ── Smart feed routing ─────────────────────────────────────────────────
    # Product principle: the feed must feel like a living path. Always give a
    # meaningful share of cards to real actions (live events, videos, classes,
    # adventures) so users are not trapped in generic Eviva/IOO opportunity loops.
    if random.random() < 0.45:
        real_action = await _try_real_world_card(user_id, tier, daily_limit, current_count, body.domain, preferred_city)
        if real_action is not None:
            return real_action

    # IOO remains the intelligence layer, but not the whole feed.
    if not body.domain and random.random() < 0.55:
        ioo_response = await _try_ioo_card(user_id, body.goal_id, tier, daily_limit)
        if ioo_response is not None:
            return ioo_response
    # ──────────────────────────────────────────────────────────────────────

    try:
        spec_dict, db_id, screens_today = await brain.get_screen(
            user_id=user_id,
            context=body.context or "",
            goal_id=body.goal_id,
            domain=body.domain,
            geo_hints=geo_hints,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Brain error for user {user_id[:8]}, using static fallback: {e}", exc_info=True)
        return await _static_fallback_card(user_id, tier, daily_limit, f"brain_exception:{type(e).__name__}")

    # Parse spec into Pydantic model (validates structure)
    try:
        screen = ScreenSpec(
            screen_id=spec_dict["screen_id"],
            type=spec_dict.get("type", "unknown"),
            layout=spec_dict.get("layout", "scroll"),
            components=spec_dict.get("components", []),
            feedback_overlay=FeedbackOverlay(
                **spec_dict.get(
                    "feedback_overlay",
                    {"type": "star_rating", "position": "bottom_right", "always_visible": True},
                )
            ),
            metadata=ScreenMetadata(**spec_dict.get("metadata", {"agent": "OraBrain"})),
        )
    except Exception as e:
        logger.error(f"Spec parse error, using static fallback: {e}", exc_info=True)
        return await _static_fallback_card(user_id, tier, daily_limit, f"spec_parse_error:{type(e).__name__}")

    return ScreenResponse(
        screen=screen,
        screen_spec_db_id=db_id,
        screens_today=screens_today,
        daily_limit=daily_limit,
        is_limited=(tier == "free"),
    )


# ---------------------------------------------------------------------------
# Batch endpoint — prefetch multiple screens at once
# ---------------------------------------------------------------------------

class BatchScreenRequest(BaseModel):
    count: int = 3
    goal_id: Optional[str] = None
    domain: Optional[DomainType] = None
    context: Optional[str] = None


@router.post("/batch", response_model=List[ScreenResponse])
async def get_screen_batch(
    body: BatchScreenRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    """
    Fetch up to 5 screens at once for TikTok-style prefetching.
    Each screen goes through the same logic as /next.
    Returns a list of ScreenResponse objects.
    """
    count = max(1, min(body.count, 5))  # cap at 5

    # Check subscription tier once
    user_row = await fetchrow(
        "SELECT subscription_tier FROM users WHERE id = $1", str(user_id)
    )
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    tier = user_row["subscription_tier"]
    is_free = tier == "free"
    daily_limit = max(settings.FREE_TIER_DAILY_SCREENS, 999)

    if is_free:
        # MVP/stability: keep authentication, but do not hard-block the primary
        # feed on daily limits. A broken empty feed is worse than over-serving
        # during pre-product-stability.
        current_count = await get_daily_screen_count(user_id)
        if current_count >= settings.FREE_TIER_DAILY_SCREENS:
            logger.info(
                "Free user %s exceeded configured daily screen limit (%s/%s); serving feed for MVP stability",
                user_id[:8],
                current_count,
                settings.FREE_TIER_DAILY_SCREENS,
            )

    brain = get_brain()
    results: List[ScreenResponse] = []

    # Geo context for batch (resolve once, reuse)
    batch_geo_hints = {}
    try:
        ip = _client_ip(request)
        geo = await get_location_for_ip(ip)
        batch_geo_hints = geo_to_context_hints(geo)
    except Exception:
        pass

    preferred_city = await _user_city(user_id)

    # Check if user has active goals (once, shared across batch)
    # Fetch screens sequentially to respect rate limits and user model updates
    for _ in range(count):
        try:
            slot_index = len(results)

            # Force diversity in every prefetched batch. Slots 0/2/4 are real
            # actions where possible; slots 1/3 can use IOO/brain. This directly
            # prevents repeated "Eviva Opportunities" cards from occupying the
            # whole swipe queue.
            if slot_index in (0, 2, 4):
                real_action = await _try_real_world_card(user_id, tier, daily_limit, slot_index, body.domain, preferred_city)
                if real_action is not None:
                    results.append(real_action)
                    continue

            # No goals gate: anyone can discover IOO nodes regardless of whether they have active goals.
            if not body.domain and random.random() < 0.50:
                ioo_resp = await _try_ioo_card(user_id, body.goal_id, tier, daily_limit)
                if ioo_resp is not None:
                    results.append(ioo_resp)
                    continue

            if random.random() < 0.35:
                real_action = await _try_real_world_card(user_id, tier, daily_limit, slot_index, body.domain, preferred_city)
                if real_action is not None:
                    results.append(real_action)
                    continue

            spec_dict, db_id, screens_today = await brain.get_screen(
                user_id=user_id,
                context=body.context or "",
                goal_id=body.goal_id,
                domain=body.domain,
                geo_hints=batch_geo_hints,
            )
            screen = ScreenSpec(
                screen_id=spec_dict["screen_id"],
                type=spec_dict.get("type", "unknown"),
                layout=spec_dict.get("layout", "scroll"),
                components=spec_dict.get("components", []),
                feedback_overlay=FeedbackOverlay(
                    **spec_dict.get(
                        "feedback_overlay",
                        {"type": "star_rating", "position": "bottom_right", "always_visible": True},
                    )
                ),
                metadata=ScreenMetadata(**spec_dict.get("metadata", {"agent": "OraBrain"})),
            )
            results.append(
                ScreenResponse(
                    screen=screen,
                    screen_spec_db_id=db_id,
                    screens_today=screens_today,
                    daily_limit=daily_limit,
                    is_limited=is_free,
                )
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Batch screen error for user {user_id[:8]}, using fallback if needed: {e}", exc_info=True)
            if not results:
                results.append(await _static_fallback_card(user_id, tier, daily_limit, f"batch_exception:{type(e).__name__}"))
            break

    if not results:
        results.append(await _static_fallback_card(user_id, tier, daily_limit, "batch_empty"))

    return results


# ---------------------------------------------------------------------------
# Save-for-later endpoint
# ---------------------------------------------------------------------------

class SaveScreenRequest(BaseModel):
    screen_spec_id: str


@router.post("/save")
async def save_screen_for_later(
    body: SaveScreenRequest,
    user_id: str = Depends(get_current_user_id),
):
    """
    Bookmark a screen so Ora resurfaces it in ~24 hours.
    Upserts an interaction row with saved=true.
    """
    # TODO(ScreenGraph): treat this as the explicit "Do Later" graph-learning
    # signal. Increase the relevant user-specific screen_graph_edges weight,
    # connect this screen to its resurfacing/reminder screen, and avoid treating
    # a save as either completion or rejection.
    try:
        screen_uuid = UUID(body.screen_spec_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid screen_spec_id")

    try:
        await execute(
            """
            INSERT INTO interactions (user_id, screen_spec_id, saved, created_at)
            VALUES ($1, $2, TRUE, NOW())
            ON CONFLICT (user_id, screen_spec_id)
            DO UPDATE SET saved = TRUE
            """,
            str(user_id),
            screen_uuid,
        )
    except Exception as e:
        logger.error(f"Save screen error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to save screen")

    return {"ok": True, "message": "Saved! Ora will resurface this in 24h."}


# ---------------------------------------------------------------------------
# Get by ID
# ---------------------------------------------------------------------------

@router.get("/{screen_id}")
async def get_screen_by_id(
    screen_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Retrieve a previously generated screen by its DB ID."""
    try:
        row = await fetchrow(
            "SELECT spec, agent_type, global_rating FROM screen_specs WHERE id = $1",
            UUID(screen_id),
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid screen ID")

    if not row:
        raise HTTPException(status_code=404, detail="Screen not found")

    return {
        "spec": dict(row["spec"]),
        "agent_type": row["agent_type"],
        "global_rating": row["global_rating"],
    }
