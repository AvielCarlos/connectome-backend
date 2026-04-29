"""
Surface Lifecycle Manager — evaluates IOO surfaces and kills/changes/keeps them.

Usage:
    mgr = SurfaceLifecycleManager()
    result = await mgr.evaluate_surface(surface_id)
    summary = await mgr.run_lifecycle_sweep()
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from core.database import fetch, fetchrow, execute
from ora.agents.surface_generator import SurfaceGenerator

logger = logging.getLogger(__name__)

# Mechanism rotation order for variants
MECHANISM_ROTATION = ["button", "conversation", "proactive"]


class SurfaceLifecycleManager:
    """Evaluate surface performance and apply lifecycle decisions."""

    def __init__(self) -> None:
        self._gen = SurfaceGenerator()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def evaluate_surface(self, surface_id: str) -> Dict[str, Any]:
        """
        Evaluate a single surface and return a decision dict.

        Decision logic:
          - view_count < 20            → "too_early"
          - engagement == 0 and views >= kill_at_views → "kill"
          - engagement < 0.05 and views >= 50          → "change"
          - engagement >= 0.1 or success >= 0.3         → "keep"
          - else                                         → "watch"
        """
        row = await fetchrow(
            "SELECT * FROM ioo_surfaces WHERE id = $1::uuid",
            str(surface_id),
        )
        if not row:
            return {
                "surface_id": str(surface_id),
                "decision": "not_found",
                "engagement_rate": 0.0,
                "success_rate": 0.0,
                "reasoning": "Surface not found in database.",
            }

        surface = dict(row)
        view_count = surface.get("view_count", 0) or 0
        interaction_count = surface.get("interaction_count", 0) or 0
        completion_count = surface.get("completion_count", 0) or 0
        goal_success_count = surface.get("goal_success_count", 0) or 0
        kill_at_views = surface.get("kill_at_views", 100) or 100

        engagement_rate = interaction_count / max(view_count, 1)
        success_rate = goal_success_count / max(completion_count, 1)

        if view_count < 20:
            decision = "too_early"
            reasoning = f"Only {view_count} views — need at least 20 before deciding."

        elif engagement_rate == 0 and view_count >= kill_at_views:
            decision = "kill"
            reasoning = (
                f"Zero engagement after {view_count} views (kill_at_views={kill_at_views}). "
                "Surface is invisible to users."
            )

        elif engagement_rate < 0.05 and view_count >= 50:
            decision = "change"
            reasoning = (
                f"Engagement rate {engagement_rate:.1%} is below 5% threshold "
                f"after {view_count} views. Needs redesign."
            )

        elif engagement_rate >= 0.1 or success_rate >= 0.3:
            decision = "keep"
            reasoning = (
                f"Performing well — engagement={engagement_rate:.1%}, "
                f"success={success_rate:.1%}."
            )

        else:
            decision = "watch"
            reasoning = (
                f"Engagement={engagement_rate:.1%}, success={success_rate:.1%}. "
                "Not enough signal yet; keep monitoring."
            )

        return {
            "surface_id": str(surface_id),
            "surface_type": surface.get("surface_type", ""),
            "decision": decision,
            "engagement_rate": round(engagement_rate, 4),
            "success_rate": round(success_rate, 4),
            "view_count": view_count,
            "interaction_count": interaction_count,
            "reasoning": reasoning,
        }

    async def run_lifecycle_sweep(self) -> Dict[str, Any]:
        """
        Evaluate all non-killed surfaces and apply decisions.

        Returns a summary dict with counts for each decision type.
        """
        rows = await fetch(
            "SELECT id FROM ioo_surfaces WHERE status != 'killed' ORDER BY created_at",
        )

        summary: Dict[str, List[str]] = {
            "killed": [],
            "changed": [],
            "kept": [],
            "watched": [],
            "too_early": [],
            "errors": [],
        }

        for row in rows:
            sid = str(row["id"])
            try:
                result = await self.evaluate_surface(sid)
                decision = result["decision"]

                if decision == "kill":
                    await execute(
                        "UPDATE ioo_surfaces SET status = 'killed', updated_at = NOW() WHERE id = $1::uuid",
                        sid,
                    )
                    summary["killed"].append(sid)

                elif decision == "change":
                    await execute(
                        "UPDATE ioo_surfaces SET status = 'changing', updated_at = NOW() WHERE id = $1::uuid",
                        sid,
                    )
                    try:
                        await self.generate_variant(dict(await fetchrow(
                            "SELECT * FROM ioo_surfaces WHERE id = $1::uuid", sid,
                        ) or {}))
                    except Exception as ve:
                        logger.warning("generate_variant failed for %s: %s", sid, ve)
                    summary["changed"].append(sid)

                elif decision == "keep":
                    await execute(
                        "UPDATE ioo_surfaces SET status = 'active', updated_at = NOW() WHERE id = $1::uuid",
                        sid,
                    )
                    summary["kept"].append(sid)

                elif decision == "watch":
                    summary["watched"].append(sid)

                else:  # too_early / not_found
                    summary["too_early"].append(sid)

            except Exception as e:
                logger.error("Lifecycle sweep error for surface %s: %s", sid, e, exc_info=True)
                summary["errors"].append(sid)

        return {
            "swept": len(rows),
            "killed": len(summary["killed"]),
            "changed": len(summary["changed"]),
            "kept": len(summary["kept"]),
            "watched": len(summary["watched"]),
            "too_early": len(summary["too_early"]),
            "errors": len(summary["errors"]),
            "detail": summary,
        }

    async def generate_variant(self, surface: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate a new surface variant for an underperforming surface.

        Rotates the open_mechanism (button → conversation → proactive) and
        regenerates the spec, saving as a fresh row with a parent reference.
        """
        if not surface:
            raise ValueError("Cannot generate variant: empty surface dict")

        current_mechanism = surface.get("open_mechanism", "button")
        try:
            next_idx = (MECHANISM_ROTATION.index(current_mechanism) + 1) % len(MECHANISM_ROTATION)
        except ValueError:
            next_idx = 0
        next_mechanism = MECHANISM_ROTATION[next_idx]

        # Fetch the originating node
        node_id = surface.get("node_id")
        if not node_id:
            raise ValueError("Surface has no node_id")

        node_row = await fetchrow(
            "SELECT * FROM ioo_nodes WHERE id = $1::uuid",
            str(node_id),
        )
        if not node_row:
            raise ValueError(f"Node {node_id} not found")

        node = dict(node_row)

        # Generate a fresh spec with the new mechanism
        new_spec = self._gen.generate_spec(node, open_mechanism=next_mechanism)

        # Embed parent reference in spec
        new_spec["parent_surface_id"] = str(surface.get("id", ""))
        new_spec["variant_reason"] = "underperforming_mechanism_rotation"

        # Insert new variant surface
        new_row = await fetchrow(
            """
            INSERT INTO ioo_surfaces
                (node_id, surface_type, title, spec, open_mechanism, status)
            VALUES ($1::uuid, $2, $3, $4, $5, 'testing')
            RETURNING id, node_id, surface_type, title, spec, status, open_mechanism, created_at
            """,
            str(node_id),
            new_spec.get("template", "info_card"),
            new_spec.get("title", ""),
            new_spec,
            next_mechanism,
        )

        logger.info(
            "Generated variant surface %s for parent %s (mechanism: %s → %s)",
            new_row["id"] if new_row else "?",
            surface.get("id", "?"),
            current_mechanism,
            next_mechanism,
        )

        return dict(new_row) if new_row else {}
