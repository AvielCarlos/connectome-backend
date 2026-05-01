"""Evolution Engine v1 for the living IOO graph.

This module is intentionally conservative: it rewards real-world evidence with a
small anti-gaming CP amount, reinforces/prunes graph pathways, and spawns a few
adjacent test nodes only after actual completion evidence.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.database import execute, fetch, fetchrow, fetchval
from ora.agents.ioo_graph_agent import get_graph_agent

logger = logging.getLogger(__name__)

EVIDENCE_CP_AMOUNT = 2
EVIDENCE_CP_DAILY_CAP = 10


@dataclass
class EvolutionResult:
    cp_awarded: int = 0
    cp_message: str = ""
    spawned_nodes: list[dict[str, Any]] | None = None
    pruned_nodes: int = 0
    edge_weights_updated: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "cp_awarded": self.cp_awarded,
            "cp_message": self.cp_message,
            "spawned_nodes": self.spawned_nodes or [],
            "pruned_nodes": self.pruned_nodes,
            "edge_weights_updated": self.edge_weights_updated,
            "principle": "Real action evidence teaches Aura which nodes, edges, and variants deserve to live, branch, or fade.",
        }


def _plain(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    return value


def _evidence_strength(evidence: dict[str, Any] | None, note: str | None = None) -> float:
    """Small heuristic: reward proof, not spammy taps."""
    evidence = evidence or {}
    text_bits = [str(note or "")]
    for key in ("text", "note", "reflection", "proof", "outcome"):
        if evidence.get(key):
            text_bits.append(str(evidence[key]))
    text_len = len(" ".join(text_bits).strip())
    has_attachment = bool(evidence.get("url") or evidence.get("photo_url") or evidence.get("attachment_url"))
    rating = evidence.get("rating") or evidence.get("fulfilment_rating") or evidence.get("value_rating")
    strength = 0.65
    if text_len >= 24:
        strength += 0.2
    if has_attachment:
        strength += 0.15
    try:
        if float(rating) >= 4:
            strength += 0.15
    except Exception:
        pass
    return max(0.25, min(1.25, strength))


async def _award_evidence_cp(user_id: str, run_id: str) -> tuple[int, str]:
    """Award small CP once per execution run, capped daily to reduce gaming."""
    duplicate = await fetchval(
        """
        SELECT 1 FROM cp_transactions
        WHERE user_id = $1::uuid AND reason = 'action_evidence' AND reference_id = $2
        LIMIT 1
        """,
        str(user_id),
        str(run_id),
    )
    if duplicate:
        return 0, "Evidence already counted for this action."

    awarded_today = await fetchval(
        """
        SELECT COALESCE(SUM(amount), 0)
        FROM cp_transactions
        WHERE user_id = $1::uuid
          AND reason = 'action_evidence'
          AND amount > 0
          AND created_at >= date_trunc('day', NOW())
        """,
        str(user_id),
    ) or 0
    remaining = max(0, EVIDENCE_CP_DAILY_CAP - int(awarded_today))
    amount = min(EVIDENCE_CP_AMOUNT, remaining)
    if amount <= 0:
        return 0, "Evidence received. Daily evidence CP cap reached."

    await execute(
        """
        INSERT INTO user_cp_balance (user_id, cp_balance, total_cp_earned, last_updated)
        VALUES ($1::uuid, $2, $2, NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            cp_balance = user_cp_balance.cp_balance + $2,
            total_cp_earned = user_cp_balance.total_cp_earned + $2,
            last_updated = NOW()
        """,
        str(user_id),
        int(amount),
    )
    await execute(
        """
        INSERT INTO cp_transactions (user_id, amount, reason, reference_id, created_at)
        VALUES ($1::uuid, $2, 'action_evidence', $3, NOW())
        """,
        str(user_id),
        int(amount),
        str(run_id),
    )
    return int(amount), f"Evidence received +{amount} CP. Small on purpose: CP rewards proof without making spam profitable."


async def _reinforce_previous_edge(user_id: str, node_id: str) -> None:
    previous = await fetchrow(
        """
        SELECT node_id FROM ioo_execution_runs
        WHERE user_id = $1::uuid
          AND status = 'completed'
          AND node_id IS NOT NULL
          AND node_id <> $2::uuid
        ORDER BY completed_at DESC NULLS LAST, updated_at DESC
        LIMIT 1
        """,
        str(user_id),
        str(node_id),
    )
    if not previous:
        return
    await execute(
        """
        INSERT INTO ioo_edges (from_node_id, to_node_id, relation_type, traversal_count, success_count, weight, confidence, rationale, last_reinforced_at)
        VALUES ($1::uuid, $2::uuid, 'next_completed_after', 1, 1, 0.72, 0.65, 'User completed these nodes in sequence.', NOW())
        ON CONFLICT (from_node_id, to_node_id) DO UPDATE SET
            traversal_count = ioo_edges.traversal_count + 1,
            success_count = ioo_edges.success_count + 1,
            relation_type = CASE WHEN ioo_edges.relation_type = 'leads_to' THEN 'next_completed_after' ELSE ioo_edges.relation_type END,
            confidence = LEAST(1.0, COALESCE(ioo_edges.confidence, 0.5) + 0.04),
            last_reinforced_at = NOW(),
            updated_at = NOW()
        """,
        str(previous["node_id"]),
        str(node_id),
    )


async def record_action_evidence(
    *,
    user_id: str,
    node_id: str,
    run_id: str,
    evidence: dict[str, Any] | None,
    completion_note: str | None = None,
) -> dict[str, Any]:
    """Main v1 loop: action -> evidence -> reward -> reinforce -> grow/trim."""
    graph = get_graph_agent()
    strength = _evidence_strength(evidence, completion_note)
    result = EvolutionResult(spawned_nodes=[])

    await execute(
        """
        INSERT INTO ioo_graph_events (event_type, user_id, node_id, payload)
        VALUES ('action_evidence', $1::uuid, $2::uuid, $3::jsonb)
        """,
        str(user_id),
        str(node_id),
        json.dumps({
            "run_id": str(run_id),
            "evidence": evidence or {},
            "completion_note": completion_note,
            "strength": strength,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }),
    )

    cp_awarded, cp_message = await _award_evidence_cp(user_id, run_id)
    result.cp_awarded = cp_awarded
    result.cp_message = cp_message

    await graph.record_node_outcome(str(user_id), str(node_id), success=True)
    await graph.reinforce_node_signal(str(node_id), "fulfilment_high", user_id=str(user_id), strength=strength)
    await _reinforce_previous_edge(str(user_id), str(node_id))

    # Branch only after evidence so autonomous growth follows reality, not just imagination.
    try:
        spawned = await graph.grow_node_from_angles(
            str(node_id),
            angles=["easiest", "social", "lowest_cost", "growth_edge"],
            max_new=3,
        )
        result.spawned_nodes = [_plain(dict(row)) for row in spawned]
    except Exception as err:
        logger.debug("Evolution node growth skipped: %s", err)

    try:
        prune = await graph.prune_underperforming_nodes(min_attempts=10, max_nodes=10)
        result.pruned_nodes = int(prune.get("pruned", 0))
    except Exception as err:
        logger.debug("Evolution pruning skipped: %s", err)

    try:
        result.edge_weights_updated = int(await graph.update_edge_weights())
    except Exception as err:
        logger.debug("Evolution edge-weight update skipped: %s", err)

    try:
        await graph.build_user_ioo_vector(str(user_id))
    except Exception as err:
        logger.debug("Evolution user-vector rebuild skipped: %s", err)

    await execute(
        """
        INSERT INTO ioo_graph_events (event_type, user_id, node_id, payload)
        VALUES ('evolution_cycle_v1', $1::uuid, $2::uuid, $3::jsonb)
        """,
        str(user_id),
        str(node_id),
        json.dumps(result.as_dict()),
    )
    return result.as_dict()


async def run_background_evolution(max_nodes: int = 25) -> dict[str, Any]:
    """Manual/scheduled v1 maintenance: trim weak branches and refresh weights."""
    graph = get_graph_agent()
    prune = await graph.prune_underperforming_nodes(min_attempts=10, max_nodes=max_nodes)
    edge_updates = await graph.update_edge_weights()
    await execute(
        """
        INSERT INTO ioo_graph_events (event_type, payload)
        VALUES ('background_evolution_v1', $1::jsonb)
        """,
        json.dumps({"pruned": prune.get("pruned", 0), "edge_weights_updated": edge_updates}),
    )
    return {"pruned": prune.get("pruned", 0), "edge_weights_updated": edge_updates, "nodes": prune.get("nodes", [])}
