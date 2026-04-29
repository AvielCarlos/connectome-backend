"""Screen Graph agent foundation.

Screens are durable pathway/interface nodes between user state, IOO
possibilities, and execution. This module intentionally contains deterministic
helpers only: no external LLM calls, no hidden generation side effects.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Optional
from uuid import UUID

from core.database import fetchrow

SCREEN_RELATIONS = {
    "leads_to",
    "requires",
    "clarifies",
    "executes",
    "belongs_to_ioo_node",
}


def _as_uuid(value: Any) -> Optional[UUID]:
    """Return value as UUID when possible; otherwise None."""
    if value in (None, ""):
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _terms(value: Any) -> set[str]:
    """Create a small deterministic term set for lightweight matching."""
    if value is None:
        return set()
    if isinstance(value, Mapping):
        chunks = [str(k) for k in value.keys()] + [str(v) for v in value.values()]
    elif isinstance(value, (list, tuple, set)):
        chunks = [str(v) for v in value]
    else:
        chunks = str(value).replace("_", " ").split()
    return {chunk.strip().lower() for chunk in chunks if chunk and chunk.strip()}


def _overlap_score(a: Any, b: Any) -> float:
    """Return a deterministic 0..1 lexical overlap score."""
    a_terms = _terms(a)
    b_terms = _terms(b)
    if not a_terms or not b_terms:
        return 0.0
    return len(a_terms & b_terms) / max(len(a_terms | b_terms), 1)


async def create_screen_edge(
    *,
    from_screen_id: Any = None,
    to_screen_id: Any = None,
    relation_type: str,
    ioo_node_id: Any = None,
    user_id: Any = None,
    weight: float = 1.0,
    evidence: Optional[Mapping[str, Any]] = None,
) -> Optional[dict]:
    """Create or update a Screen Graph edge.

    Edges connect generated screen specs to other screen specs and/or IOO nodes.
    `from_screen_id` and `to_screen_id` are optional because some relations, such
    as `belongs_to_ioo_node`, may initially only attach a screen to an IOO node.
    Invalid relation types raise ValueError; invalid UUID-like identifiers are
    stored as NULL rather than crashing the learning path.
    """
    if relation_type not in SCREEN_RELATIONS:
        raise ValueError(f"Unsupported screen graph relation_type: {relation_type}")

    safe_weight = max(0.0, min(float(weight), 10.0))
    row = await fetchrow(
        """
        INSERT INTO screen_graph_edges (
            from_screen_id, to_screen_id, relation_type, ioo_node_id,
            user_id, weight, evidence, created_at, updated_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, NOW(), NOW())
        RETURNING id, from_screen_id, to_screen_id, relation_type, ioo_node_id,
                  user_id, weight, evidence, created_at, updated_at
        """,
        _as_uuid(from_screen_id),
        _as_uuid(to_screen_id),
        relation_type,
        _as_uuid(ioo_node_id),
        _as_uuid(user_id),
        safe_weight,
        json.dumps(dict(evidence or {})),
    )
    return dict(row) if row else None


def propose_option_spindles(
    user_state: Mapping[str, Any],
    desired_outcome: Any,
    candidate_nodes: Iterable[Mapping[str, Any]],
) -> list[dict]:
    """Propose multiple deterministic A→B route spindles.

    The output is deliberately simple: each spindle groups candidate IOO nodes
    around a route strategy. Runtime agents can later expand these into richer
    graph searches and execution plans.
    """
    nodes = [dict(node) for node in candidate_nodes]
    if not nodes:
        return []

    strategies = [
        ("fastest", "requires_time_hours", False),
        ("easiest", "difficulty_level", False),
        ("growth", "difficulty_level", True),
        ("best_fit", "semantic_fit", True),
    ]
    spindles: list[dict] = []
    for name, key, reverse in strategies:
        scored: list[tuple[float, dict]] = []
        for node in nodes:
            if key == "semantic_fit":
                score = (
                    _overlap_score(desired_outcome, node.get("title")) * 0.45
                    + _overlap_score(desired_outcome, node.get("description")) * 0.25
                    + _overlap_score(user_state.get("preferences"), node.get("tags")) * 0.20
                    + _overlap_score(user_state.get("domain"), node.get("domain")) * 0.10
                )
            else:
                raw = node.get(key)
                score = float(raw if raw is not None else (999 if key == "requires_time_hours" else 5))
            scored.append((score, node))

        ranked = [node for _, node in sorted(scored, key=lambda item: item[0], reverse=reverse)]
        selected = ranked[:5]
        if selected:
            spindles.append(
                {
                    "strategy": name,
                    "desired_outcome": desired_outcome,
                    "nodes": selected,
                    "pathway_node_ids": [str(node.get("id")) for node in selected if node.get("id")],
                    "confidence": round(0.45 + min(len(selected), 5) * 0.08, 2),
                }
            )
    return spindles


def rank_screen_pathways(
    user_context: Mapping[str, Any],
    ioo_nodes: Iterable[Mapping[str, Any]],
    screen_candidates: Iterable[Mapping[str, Any]],
) -> list[dict]:
    """Rank generated screens as pathway nodes for the current user context.

    The scorer favours screens connected to matching IOO nodes, user-preferred
    domains/tags, explicit screen roles, and prior edge weight. It is stable and
    explainable so UI/API code can use it before introducing heavier planning.
    """
    node_by_id = {str(node.get("id")): dict(node) for node in ioo_nodes if node.get("id")}
    preferred_terms = _terms(user_context.get("preferences")) | _terms(user_context.get("domain"))
    desired_terms = _terms(user_context.get("desired_outcome") or user_context.get("goal"))

    ranked: list[dict] = []
    for screen in screen_candidates:
        candidate = dict(screen)
        metadata = candidate.get("metadata") or {}
        ioo_node_id = str(candidate.get("ioo_node_id") or metadata.get("node_id") or "")
        node = node_by_id.get(ioo_node_id, {})

        score = 0.0
        score += _overlap_score(preferred_terms, candidate.get("domain") or metadata.get("domain")) * 0.20
        score += _overlap_score(preferred_terms, metadata.get("tags") or candidate.get("tags")) * 0.20
        score += _overlap_score(desired_terms, node.get("title")) * 0.20
        score += _overlap_score(desired_terms, node.get("description")) * 0.15
        score += min(float(candidate.get("edge_weight", 1.0) or 1.0), 10.0) / 10.0 * 0.15
        if candidate.get("screen_role") in {"clarify", "execute", "recommend", "reflect"}:
            score += 0.10

        ranked.append(
            {
                **candidate,
                "ioo_node": node or None,
                "pathway_score": round(score, 4),
                "ranking_evidence": {
                    "matched_ioo_node_id": ioo_node_id or None,
                    "screen_role": candidate.get("screen_role"),
                    "edge_weight": candidate.get("edge_weight", 1.0),
                },
            }
        )

    return sorted(ranked, key=lambda item: item["pathway_score"], reverse=True)
