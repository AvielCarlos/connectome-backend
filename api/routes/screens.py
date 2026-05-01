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
from core.database import fetchrow, execute
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


def _path_progression_metadata(
    *,
    kind: str,
    domain: Optional[str],
    current_stage: str = "confirm_micro_node",
    source_node_id: Optional[str] = None,
) -> dict[str, Any]:
    """Describe how this feed card participates in the Path Feed progression graph."""
    stage_ids = [stage["id"] for stage in PATH_PROGRESSION_STAGES]
    current_index = stage_ids.index(current_stage) if current_stage in stage_ids else 3
    stages: list[dict[str, Any]] = []
    for index, stage in enumerate(PATH_PROGRESSION_STAGES):
        status = "complete" if index < current_index else "active" if index == current_index else "upcoming"
        stages.append({**stage, "status": status})
    next_stage = PATH_PROGRESSION_STAGES[min(current_index + 1, len(PATH_PROGRESSION_STAGES) - 1)]["id"]
    return {
        "graph": "path_feed_progression_v1",
        "kind": kind,
        "domain": "iVive" if domain == "Rest" else domain,
        "current_stage": current_stage,
        "next_stage": next_stage,
        "source_node_id": source_node_id,
        "stages": stages,
        "principle": "The feed is a neural graph for building and progressing life paths, not a passive content stream.",
    }


def _path_progression_component(*, kind: str, domain: Optional[str], current_stage: str = "confirm_micro_node") -> dict[str, Any]:
    progression = _path_progression_metadata(kind=kind, domain=domain, current_stage=current_stage)
    return {
        "type": "path_progression",
        "text": "Path progression",
        "current_stage": progression["current_stage"],
        "next_stage": progression["next_stage"],
        "items": progression["stages"],
    }

# ---------------------------------------------------------------------------
# Real-world/actionable feed cards
# ---------------------------------------------------------------------------

_CURATED_REAL_ACTIONS: list[dict[str, Any]] = [
    {
        "key": "vancouver_live_events",
        "domain": "Aventi",
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
        {"type": "context_strip", "items": [{"label": "Reality", "value": "Verified URL"}, {"label": "Mode", "value": item.get("kind", "Action")}, {"label": "Domain", "value": domain}]},
    ]
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
            "path_progression": _path_progression_metadata(kind="real_world_action", domain=domain, current_stage="confirm_micro_node"),
            "tags": [item.get("tag") or "real_action", "diversity_seed"],
            "url": url,
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
                "resources": [{"label": item.get("button") or "Open", "url": url}] if url else [],
                "stat": "Real URL/action card; user can verify and act.",
            },
        },
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
    """Turn a stored live event into a feed card when the user's city has events."""
    try:
        from ora.agents.event_agent import EventAgent

        events = await EventAgent().get_recommended_events(user_id=str(user_id), days_ahead=14, limit=8)
        events = [ev for ev in events if ev.get("url")]
        if not events:
            return None
        event = random.choice(events[: min(5, len(events))])
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


async def _try_real_world_card(user_id: str, tier: str, daily_limit: int, slot_index: int = 0, domain_filter: Optional[str] = None) -> Optional[ScreenResponse]:
    """Diversify the main feed with concrete learn/attend/book actions."""
    # Prefer true live event data for the first Aventi slot, then fall back to
    # curated verified URLs so the feed is never only generic Eviva cards.
    if slot_index % 5 == 0 and (domain_filter in (None, '', 'Aventi')):
        live = await _try_live_event_action(user_id, tier, daily_limit)
        if live is not None:
            return live

    digest = hashlib.sha256(f"{user_id}:{datetime.now(timezone.utc).date()}".encode("utf-8")).hexdigest()
    candidates = [item for item in _CURATED_REAL_ACTIONS if not domain_filter or item.get('domain') == domain_filter or (domain_filter == 'iVive' and item.get('domain') == 'Rest')]
    if not candidates:
        candidates = _CURATED_REAL_ACTIONS
    start = int(digest[:8], 16) % len(candidates)
    idx = (start + slot_index) % len(candidates)
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

    # ── Smart feed routing ─────────────────────────────────────────────────
    # Product principle: the feed must feel like a living path. Always give a
    # meaningful share of cards to real actions (live events, videos, classes,
    # adventures) so users are not trapped in generic Eviva/IOO opportunity loops.
    if random.random() < 0.45:
        real_action = await _try_real_world_card(user_id, tier, daily_limit, current_count, body.domain)
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
                real_action = await _try_real_world_card(user_id, tier, daily_limit, slot_index, body.domain)
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
                real_action = await _try_real_world_card(user_id, tier, daily_limit, slot_index, body.domain)
                if real_action is not None:
                    results.append(real_action)
                    continue

            spec_dict, db_id, screens_today = await brain.get_screen(
                user_id=user_id,
                context=None,
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
