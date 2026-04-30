"""
IOO Execution Agent — turns IOO graph possibility nodes into grounded plans.

The IOO Graph is the possibility map. The IOO Execution Protocol is the bridge
from "this could matter" to "here is how the user can make it real".

This foundation is intentionally deterministic and side-effect free: execution
agents return directives/plans only. Live searches, bookings, purchases,
messages, and other irreversible/external actions require explicit user
confirmation before they are performed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable
from uuid import UUID

from ora.agents.ioo_search_agent import build_search_agent_payload


class ExecutionAgentRole:
    """Specialist roles that can participate in an IOO execution protocol."""

    SEARCH = "SearchAgent"
    UX_SELECTION = "UXSelectionAgent"
    SCHEDULING = "SchedulingAgent"
    RESOURCE = "ResourceAgent"
    ACCOUNTABILITY = "AccountabilityAgent"

    ALL = [SEARCH, UX_SELECTION, SCHEDULING, RESOURCE, ACCOUNTABILITY]


@dataclass(frozen=True)
class _NodeView:
    id: str
    title: str
    description: str
    node_type: str
    domain: str
    step_type: str
    tags: list[str]
    requires_finances: float | None
    requires_fitness_level: int | None
    requires_skills: list[str]
    requires_location: str | None
    requires_time_hours: float | None
    physical_context: str | None
    best_time: str | None
    requirements: dict[str, Any]
    difficulty_level: int


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        return dict(value)
    except Exception:
        return {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    if isinstance(value, str):
        return [value]
    return []


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except Exception:
        return None


def _text_list(value: Any) -> list[str]:
    return [str(item).strip() for item in _as_list(value) if str(item).strip()]


def _normalise_node(node: Any) -> _NodeView:
    data = _as_dict(node)
    requirements = _as_dict(data.get("requirements"))
    requires_skills = _text_list(data.get("requires_skills"))
    required_skills_from_json = _text_list(requirements.get("required_skills"))
    merged_skills = list(dict.fromkeys(requires_skills + required_skills_from_json))

    return _NodeView(
        id=str(data.get("id") or data.get("node_id") or ""),
        title=str(data.get("title") or "Untitled IOO node"),
        description=str(data.get("description") or ""),
        node_type=str(data.get("type") or data.get("node_type") or "activity"),
        domain=str(data.get("domain") or data.get("goal_category") or "IOO"),
        step_type=str(data.get("step_type") or "hybrid"),
        tags=_text_list(data.get("tags")),
        requires_finances=_number(data.get("requires_finances")),
        requires_fitness_level=int(data.get("requires_fitness_level") or 0),
        requires_skills=merged_skills,
        requires_location=data.get("requires_location"),
        requires_time_hours=_number(data.get("requires_time_hours")),
        physical_context=data.get("physical_context"),
        best_time=data.get("best_time"),
        requirements=requirements,
        difficulty_level=int(data.get("difficulty_level") or 5),
    )


def _normalise_context(user_context: Any) -> dict[str, Any]:
    context = _as_dict(user_context)
    profile = _as_dict(context.get("profile"))
    state_json = _as_dict(context.get("state_json"))
    merged = {**profile, **state_json, **context}
    merged["known_skills"] = _text_list(merged.get("known_skills"))
    return merged


def _context_location(context: dict[str, Any]) -> str | None:
    city = context.get("location_city") or context.get("city")
    country = context.get("location_country") or context.get("country")
    if city and country:
        return f"{city}, {country}"
    return city or country


def _skills_missing(required: Iterable[str], known: Iterable[str]) -> list[str]:
    known_lower = {str(skill).lower() for skill in known}
    return [skill for skill in required if str(skill).lower() not in known_lower]


def clarify_execution_requirements(node: Any, user_context: Any) -> list[str]:
    """Return clarifying questions needed before a grounded plan can proceed."""
    n = _normalise_node(node)
    context = _normalise_context(user_context)
    questions: list[str] = []

    if n.step_type in {"physical", "hybrid"} and not _context_location(context) and not n.requires_location:
        questions.append("Where should Ora optimise this for — your current city/neighbourhood or somewhere else?")

    if n.requires_finances and not context.get("finances_monthly_budget_usd") and context.get("finances_level", "unknown") == "unknown":
        questions.append("What budget range feels safe for this right now?")

    missing_skills = _skills_missing(n.requires_skills, context.get("known_skills", []))
    if missing_skills:
        questions.append(f"Do you already have these prerequisites: {', '.join(missing_skills)} — or should Ora include a prep path?")

    if n.requires_time_hours and not (context.get("free_time_weekday_hours") or context.get("free_time_weekend_hours")):
        questions.append("How much time do you want to commit to this in the next week?")

    return questions


def _agent_directives(n: _NodeView, context: dict[str, Any], intent: str) -> list[dict[str, str]]:
    location = _context_location(context) or n.requires_location or "the user's preferred location"
    values = ", ".join(n.tags[:4]) or n.domain
    timing = "soon" if intent == "do_now" else "for a future window"

    directives = [
        {
            "role": ExecutionAgentRole.SEARCH,
            "directive": (
                f"Find real, current options for '{n.title}' near {location}, including trustworthy links, providers, maps, prices, and availability signals. "
                "TODO: later call live web search / Google Places / Aventi / Eviva services; do not book or purchase without confirmation."
            ),
            "status": "planned",
        },
        {
            "role": ExecutionAgentRole.UX_SELECTION,
            "directive": f"Rank the available paths by fit with the user's values and constraints: {values}. Prefer low-friction, high-fulfilment options.",
            "status": "planned",
        },
        {
            "role": ExecutionAgentRole.SCHEDULING,
            "directive": f"Prepare timing suggestions {timing}, using best_time='{n.best_time or 'flexible'}' and the user's known free-time constraints.",
            "status": "planned",
        },
        {
            "role": ExecutionAgentRole.RESOURCE,
            "directive": "Gather tools, tutorials, booking/ticket links, maps, prep checklists, and any safety or access requirements. Present links only; ask before external actions.",
            "status": "planned",
        },
        {
            "role": ExecutionAgentRole.ACCOUNTABILITY,
            "directive": "Define completion criteria, XP/CP reward hooks, and follow-up reminder suggestions so the node can be tracked through completion.",
            "status": "planned",
        },
    ]
    return directives


def _estimated_minutes(n: _NodeView, fallback: int) -> int:
    if n.requires_time_hours:
        return max(5, int(n.requires_time_hours * 60))
    if n.node_type == "goal":
        return max(fallback, 45)
    return fallback


def _xp_reward(n: _NodeView) -> int:
    base = {1: 25, 2: 40, 3: 60, 4: 80, 5: 100, 6: 150, 7: 200, 8: 300, 9: 400, 10: 500}.get(n.difficulty_level, 100)
    if n.step_type == "physical":
        base = int(base * 1.5)
    return base


def spawn_execution_plan(node: Any, user_context: Any, intent: str = "do_now") -> dict[str, Any]:
    """Create the deterministic execution plan body for an IOO node."""
    n = _normalise_node(node)
    context = _normalise_context(user_context)
    location = _context_location(context) or n.requires_location
    minutes = _estimated_minutes(n, 30 if intent == "do_now" else 20)

    first_step_description = "Confirm the exact version of this node you want to execute."
    if location:
        first_step_description += f" Optimise around {location}."
    if n.description:
        first_step_description += f" Node context: {n.description}"

    steps = [
        {
            "title": "Set execution constraints",
            "description": first_step_description,
            "step_type": "digital",
            "estimated_minutes": 5,
        },
        {
            "title": "Find concrete options",
            "description": "Search for real options, providers, tutorials, communities, routes, or links that can make this node actionable. No external booking/purchase/message is performed yet.",
            "step_type": "digital" if n.step_type == "digital" else "hybrid",
            "estimated_minutes": 15,
        },
        {
            "title": "Choose the best path",
            "description": "Compare options by fulfilment fit, friction, cost, distance, timing, safety, and likelihood of completion.",
            "step_type": "digital",
            "estimated_minutes": 10,
        },
        {
            "title": "Commit and do the first action",
            "description": "Take the smallest confirmed next action: block time, open the tutorial, prepare gear, start outreach draft, or confirm a booking flow for user approval.",
            "step_type": n.step_type if n.step_type in {"digital", "physical", "hybrid"} else "hybrid",
            "estimated_minutes": minutes,
        },
    ]

    resources = ["user availability", "budget comfort", "completion evidence"]
    if n.step_type in {"physical", "hybrid"}:
        resources.extend(["location", "transport plan"])
    if n.requires_skills:
        resources.append("prerequisite skill check")

    links_to_find = [
        "official provider or booking pages",
        "maps/directions",
        "reviews or trust signals",
        "beginner guide or tutorial",
    ]
    if "event" in n.tags or n.node_type == "experience":
        links_to_find.append("tickets or event listings")
    if "career" in n.tags or "job" in n.tags:
        links_to_find.append("role listings and application pages")

    calendar_suggestion = None
    if intent == "do_now":
        calendar_suggestion = {
            "timing": n.best_time or "next available 30-60 minute window",
            "duration_minutes": minutes,
            "confirmation_required": True,
        }
    elif intent == "do_later":
        calendar_suggestion = {
            "timing": n.best_time or "within the next 7 days",
            "duration_minutes": minutes,
            "confirmation_required": True,
        }

    return {
        "summary": f"Turn '{n.title}' from an IOO possibility node into a confirmed real-world action path.",
        "steps": steps,
        "resources_needed": resources,
        "links_to_find": links_to_find,
        "calendar_suggestion": calendar_suggestion,
        "completion_criteria": [
            "User confirms a chosen path or option",
            "First concrete action is completed or scheduled",
            f"Evidence captured for IOO node completion/reward ({_xp_reward(n)} XP placeholder)",
        ],
    }


def build_execution_protocol(node: Any, user_context: Any, intent: str = "do_now") -> dict[str, Any]:
    """Build the full IOO Execution Protocol response payload."""
    if intent not in {"do_now", "do_later"}:
        intent = "do_now"

    n = _normalise_node(node)
    context = _normalise_context(user_context)
    questions = clarify_execution_requirements(n.__dict__, context)

    search_agent = build_search_agent_payload(n.__dict__, context, intent)
    execution_agents = _agent_directives(n, context, intent)
    for agent in execution_agents:
        if agent.get("role") == ExecutionAgentRole.SEARCH:
            agent["status"] = search_agent["status"]
            agent["candidate_count"] = len(search_agent.get("candidates", []))
            agent["fallback_used"] = bool(search_agent.get("fallback", {}).get("used"))
            break

    return {
        "node_id": n.id,
        "intent": intent,
        "status": "needs_clarification" if questions else "plan_ready",
        "clarifying_questions": questions,
        "execution_agents": execution_agents,
        "search_agent": search_agent,
        "execution_plan": spawn_execution_plan(n.__dict__, context, intent),
        "safety": {
            "external_actions_require_confirmation": True,
            "note": "Execution agents may prepare searches, links, schedules, and drafts; irreversible actions require explicit user confirmation.",
        },
    }


__all__ = [
    "ExecutionAgentRole",
    "build_execution_protocol",
    "clarify_execution_requirements",
    "spawn_execution_plan",
]
