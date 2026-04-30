"""
UX Selection Agent — deterministic option ranking for IOO execution.

The SearchAgent discovers reversible candidate actions. UXSelectionAgent decides
which option should be shown or recommended first, using a typed, side-effect
free interface that can later be replaced by richer models without changing the
execution protocol shape.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Iterable


@dataclass(frozen=True)
class RankedUXOption:
    """A candidate action ranked for user fit and execution likelihood."""

    rank: int
    id: str
    title: str
    score: float
    rationale: str
    tradeoffs: list[str]
    next_action: dict[str, Any]
    candidate: dict[str, Any] = field(default_factory=dict)
    score_breakdown: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class UXSelectionResult:
    """Deterministic UXSelectionAgent output payload."""

    role: str
    status: str
    objective: str
    intent: str
    summary: str
    ranked_options: list[RankedUXOption]
    recommended_next_action: dict[str, Any] | None
    scoring: dict[str, Any]
    safety: dict[str, Any]


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


def _text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    return str(value).strip() or fallback


def _text_list(value: Any) -> list[str]:
    return [_text(item) for item in _as_list(value) if _text(item)]


def _number(value: Any, fallback: float = 0.0) -> float:
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except Exception:
        return fallback


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _normalise_node(node: Any) -> dict[str, Any]:
    data = _as_dict(node)
    requirements = _as_dict(data.get("requirements"))
    tags = _text_list(data.get("tags"))
    required_skills = _text_list(data.get("requires_skills")) + _text_list(requirements.get("required_skills"))
    return {
        "id": _text(data.get("id") or data.get("node_id")),
        "title": _text(data.get("title"), "Untitled IOO node"),
        "description": _text(data.get("description")),
        "type": _text(data.get("type") or data.get("node_type"), "activity"),
        "domain": _text(data.get("domain") or data.get("goal_category"), "IOO"),
        "step_type": _text(data.get("step_type"), "hybrid"),
        "tags": list(dict.fromkeys(tags)),
        "requires_location": data.get("requires_location"),
        "requires_finances": data.get("requires_finances"),
        "requires_time_hours": data.get("requires_time_hours"),
        "requires_skills": list(dict.fromkeys(required_skills)),
        "best_time": data.get("best_time"),
        "difficulty_level": int(data.get("difficulty_level") or 5),
    }


def _normalise_context(user_context: Any) -> dict[str, Any]:
    context = _as_dict(user_context)
    profile = _as_dict(context.get("profile"))
    state_json = _as_dict(context.get("state_json"))
    merged = {**profile, **state_json, **context}
    merged["known_skills"] = _text_list(merged.get("known_skills"))
    return merged


def _normalise_candidate(candidate: Any, index: int) -> dict[str, Any]:
    data = _as_dict(candidate)
    source = _as_dict(data.get("source"))
    next_action = _as_dict(data.get("next_action"))
    metadata = _as_dict(data.get("metadata"))
    return {
        **data,
        "id": _text(data.get("id"), f"candidate-{index + 1}"),
        "title": _text(data.get("title"), "Untitled option"),
        "candidate_type": _text(data.get("candidate_type"), "option"),
        "source": source,
        "confidence": _clamp(_number(data.get("confidence"), 0.0)),
        "rationale": _text(data.get("rationale")),
        "next_action": next_action,
        "metadata": metadata,
    }


def _context_location(context: dict[str, Any]) -> str | None:
    city = context.get("location_city") or context.get("city")
    country = context.get("location_country") or context.get("country")
    if city and country:
        return f"{city}, {country}"
    return city or country


def _skills_missing(required: Iterable[str], known: Iterable[str]) -> list[str]:
    known_lower = {str(skill).lower() for skill in known}
    return [skill for skill in required if str(skill).lower() not in known_lower]


def _contains_any(haystack: str, needles: Iterable[str]) -> bool:
    lower = haystack.lower()
    return any(str(needle).lower() in lower for needle in needles if str(needle).strip())


def _fit_score(candidate: dict[str, Any], node: dict[str, Any], objective: str) -> float:
    text = " ".join(
        [
            candidate["title"],
            candidate["candidate_type"],
            candidate.get("rationale", ""),
            _text(candidate.get("metadata", {}).get("query")),
            _text(candidate.get("source", {}).get("name")),
        ]
    ).lower()
    tags = [tag.lower() for tag in node["tags"]]
    score = 0.45
    if _contains_any(text, tags):
        score += 0.2
    if node["domain"].lower() in text or node["type"].lower() in text:
        score += 0.12
    if objective and _contains_any(text, objective.split()[:8]):
        score += 0.1
    if candidate.get("candidate_type") in {"fallback_clarification", "learning_or_prep_path"}:
        score += 0.05
    return _clamp(score)


def _constraint_score(candidate: dict[str, Any], node: dict[str, Any], context: dict[str, Any]) -> tuple[float, list[str]]:
    score = 0.72
    tradeoffs: list[str] = []
    location = _context_location(context)
    metadata = candidate.get("metadata", {})

    if node["step_type"] in {"physical", "hybrid"}:
        if metadata.get("location_used") or location or node.get("requires_location"):
            score += 0.12
        else:
            score -= 0.18
            tradeoffs.append("Needs a location before this can become truly concrete.")

    if node.get("requires_finances") and not (
        context.get("finances_monthly_budget_usd") or context.get("budget_usd") or context.get("finances_level") not in {None, "unknown"}
    ):
        score -= 0.1
        tradeoffs.append("Budget comfort is unknown, so price-sensitive options need confirmation.")

    missing_skills = _skills_missing(node.get("requires_skills", []), context.get("known_skills", []))
    if missing_skills and candidate.get("candidate_type") != "learning_or_prep_path":
        score -= min(0.16, 0.04 * len(missing_skills))
        tradeoffs.append("May require prep first: " + ", ".join(missing_skills[:3]) + ".")
    elif missing_skills:
        score += 0.08

    if node.get("requires_time_hours") and not (context.get("free_time_weekday_hours") or context.get("free_time_weekend_hours")):
        score -= 0.06
        tradeoffs.append("Time availability is unknown; scheduling still needs user confirmation.")

    return _clamp(score), tradeoffs


def _friction_score(candidate: dict[str, Any], intent: str) -> tuple[float, list[str]]:
    source = candidate.get("source", {})
    next_action = candidate.get("next_action", {})
    tradeoffs: list[str] = []
    score = 0.5

    if source.get("url") or next_action.get("url"):
        score += 0.22
    else:
        tradeoffs.append("No direct link is available yet.")
    if next_action.get("requires_confirmation"):
        score -= 0.1
        tradeoffs.append("Requires explicit confirmation before proceeding.")
    else:
        score += 0.1
    if next_action.get("action_type") in {"open_link", "review", "shortlist"}:
        score += 0.08
    if candidate.get("candidate_type") == "fallback_clarification":
        score -= 0.16
    if intent == "do_now" and candidate.get("candidate_type") in {"local_discovery", "learning_or_prep_path", "repository_search"}:
        score += 0.04

    return _clamp(score), tradeoffs


def _fulfilment_score(candidate: dict[str, Any], node: dict[str, Any]) -> float:
    text = " ".join([candidate["title"], candidate.get("rationale", ""), candidate.get("candidate_type", "")]).lower()
    score = 0.58
    high_signal_words = {"community", "mission", "impact", "volunteer", "local", "practical", "guide", "event", "experience"}
    if high_signal_words & set(text.replace("-", " ").split()):
        score += 0.12
    if node["domain"].lower() in {"ivive", "eviva", "aventi"}:
        score += 0.05
    if candidate.get("confidence", 0) >= 0.65:
        score += 0.06
    return _clamp(score)


def _rationale(candidate: dict[str, Any], breakdown: dict[str, float]) -> str:
    strongest = max(breakdown, key=breakdown.get)
    reason = candidate.get("rationale") or "This option is a plausible path toward the objective."
    labels = {
        "confidence": "source confidence",
        "fit": "objective fit",
        "constraints": "constraint fit",
        "friction": "low-friction next action",
        "fulfilment": "fulfilment potential",
    }
    return f"{reason} Ranked highly for {labels.get(strongest, strongest)}."


def _default_next_action(candidate: dict[str, Any]) -> dict[str, Any]:
    action = dict(candidate.get("next_action") or {})
    action.setdefault("label", "Review this option and confirm whether to continue")
    action.setdefault("action_type", "review")
    action.setdefault("requires_confirmation", False)
    if not action.get("url"):
        source_url = candidate.get("source", {}).get("url")
        if source_url:
            action["url"] = source_url
    action.setdefault("candidate_id", candidate["id"])
    return action


def build_ux_selection_payload(
    *,
    objective: str,
    node: Any,
    user_context: Any,
    candidate_actions: Iterable[Any],
    intent: str = "do_now",
) -> dict[str, Any]:
    """
    Rank candidate actions and return the recommended next action.

    Inputs are plain dict/dataclass shaped values so the agent can consume
    SearchAgent output, hand-authored candidates, or future live-provider data.
    The output is deterministic: same inputs produce the same ranking.
    """

    n = _normalise_node(node)
    context = _normalise_context(user_context)
    clean_intent = intent if intent in {"do_now", "do_later"} else "do_now"
    candidates = [_normalise_candidate(candidate, index) for index, candidate in enumerate(candidate_actions or [])]

    ranked: list[RankedUXOption] = []
    for candidate in candidates:
        confidence = candidate["confidence"]
        fit = _fit_score(candidate, n, objective)
        constraints, constraint_tradeoffs = _constraint_score(candidate, n, context)
        friction, friction_tradeoffs = _friction_score(candidate, clean_intent)
        fulfilment = _fulfilment_score(candidate, n)
        breakdown = {
            "confidence": round(confidence, 3),
            "fit": round(fit, 3),
            "constraints": round(constraints, 3),
            "friction": round(friction, 3),
            "fulfilment": round(fulfilment, 3),
        }
        weighted = (
            confidence * 0.25
            + fit * 0.2
            + constraints * 0.2
            + friction * 0.2
            + fulfilment * 0.15
        )
        tradeoffs = list(dict.fromkeys(constraint_tradeoffs + friction_tradeoffs))
        if not tradeoffs:
            tradeoffs = ["Still needs user confirmation before any irreversible action."]

        ranked.append(
            RankedUXOption(
                rank=0,
                id=candidate["id"],
                title=candidate["title"],
                score=round(weighted * 100, 1),
                rationale=_rationale(candidate, breakdown),
                tradeoffs=tradeoffs[:3],
                next_action=_default_next_action(candidate),
                candidate=candidate,
                score_breakdown=breakdown,
            )
        )

    ranked = sorted(
        ranked,
        key=lambda option: (-option.score, option.id),
    )[:5]
    ranked = [
        RankedUXOption(
            rank=index + 1,
            id=option.id,
            title=option.title,
            score=option.score,
            rationale=option.rationale,
            tradeoffs=option.tradeoffs,
            next_action=option.next_action,
            candidate=option.candidate,
            score_breakdown=option.score_breakdown,
        )
        for index, option in enumerate(ranked)
    ]

    recommended = dict(ranked[0].next_action) if ranked else None
    if recommended:
        recommended["label"] = f"Recommended: {recommended.get('label', 'review this option')}"
        recommended["ranked_option_id"] = ranked[0].id
        recommended["score"] = ranked[0].score

    status = "ranked" if ranked else "needs_candidates"
    result = UXSelectionResult(
        role="UXSelectionAgent",
        status=status,
        objective=objective or f"Choose the best path for {n['title']}",
        intent=clean_intent,
        summary=(
            f"Ranked {len(ranked)} option(s) for fit, constraints, friction, fulfilment, and source confidence."
            if ranked
            else "No candidate actions were provided; SearchAgent or a human should supply options first."
        ),
        ranked_options=ranked,
        recommended_next_action=recommended,
        scoring={
            "method": "deterministic_weighted_ranking_v1",
            "weights": {
                "confidence": 0.25,
                "fit": 0.2,
                "constraints": 0.2,
                "friction": 0.2,
                "fulfilment": 0.15,
            },
            "tie_breaker": "stable candidate id ascending",
        },
        safety={
            "side_effect_free": True,
            "external_actions_require_confirmation": True,
            "irreversible_actions_ranked_but_not_performed": True,
        },
    )
    return asdict(result)


__all__ = [
    "RankedUXOption",
    "UXSelectionResult",
    "build_ux_selection_payload",
]
