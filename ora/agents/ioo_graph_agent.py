"""
IOO Graph Agent — IRL Experience Achievement Map

Traverses the IOO graph to recommend real-world nodes (activities, experiences,
sub-goals, goals) filtered by each user's capability profile.

The graph learns from cross-user traversal data: edges gain weight when users
successfully complete nodes after taking them, making popular successful paths
rise to the top over time.
"""

import logging
import json
from typing import Optional
from uuid import UUID

from core.database import fetch, fetchrow, fetchval, execute

logger = logging.getLogger(__name__)


class IOOGraphAgent:
    """Traverses and learns the IOO achievement graph."""

    # ------------------------------------------------------------------
    # Recommendation
    # ------------------------------------------------------------------

    async def recommend_next_nodes(
        self,
        user_id: str,
        goal_id: Optional[str] = None,
        limit: int = 5,
    ) -> list:
        """
        Return the best next nodes for a user toward their goal.

        Strategy:
        1. Find nodes the user hasn't completed/abandoned
        2. Filter by user capability (finances, fitness, location, skills)
        3. Sort by edge weight (if coming from a completed node) then by
           success_rate = success_count / max(attempt_count, 1)
        4. Limit to `limit` results
        """
        # Fetch user state
        user_state = await fetchrow(
            "SELECT * FROM ioo_user_state WHERE user_id = $1",
            str(user_id),
        )

        # Build filter clause from user capabilities
        finance_filter = ""
        fitness_filter = ""
        location_filter = ""

        params: list = [str(user_id)]
        idx = 2  # next param index

        if user_state:
            # Finance: skip nodes that cost more than user can afford
            budget = user_state["finances_monthly_budget_usd"]
            if budget is not None:
                finance_filter = f"AND (n.requires_finances IS NULL OR n.requires_finances <= ${idx})"
                params.append(float(budget))
                idx += 1

            # Fitness
            fitness = user_state["fitness_level"]
            if fitness is not None:
                fitness_filter = f"AND (n.requires_fitness_level IS NULL OR n.requires_fitness_level <= ${idx})"
                params.append(int(fitness))
                idx += 1

            # Location
            city = user_state["location_city"]
            country = user_state["location_country"]
            if city or country:
                location_filter = (
                    f"AND (n.requires_location IS NULL "
                    f"OR n.requires_location ILIKE ${idx} "
                    f"OR n.requires_location ILIKE ${idx+1})"
                )
                params.append(f"%{city}%" if city else "%")
                params.append(f"%{country}%" if country else "%")
                idx += 2

        # Find completed node IDs for this user (to use edge weights)
        completed_ids = await fetch(
            """
            SELECT node_id FROM ioo_user_progress
            WHERE user_id = $1 AND status = 'completed'
            """,
            str(user_id),
        )
        completed_node_ids = [str(r["node_id"]) for r in completed_ids]

        # Exclude already-completed or abandoned nodes
        exclude_sql = f"""
            SELECT node_id FROM ioo_user_progress
            WHERE user_id = $1 AND status IN ('completed', 'abandoned')
        """

        # Main recommendation query — join with edges for weight boosting
        query = f"""
            SELECT DISTINCT ON (n.id)
                n.*,
                COALESCE(e.weight, 0.5) AS edge_weight,
                CASE WHEN n.attempt_count > 0
                     THEN n.success_count::float / n.attempt_count
                     ELSE 0.5
                END AS success_rate
            FROM ioo_nodes n
            LEFT JOIN ioo_edges e ON e.to_node_id = n.id
                AND e.from_node_id = ANY(${{any_idx}}::uuid[])
            WHERE n.is_active = TRUE
              AND n.id NOT IN ({exclude_sql})
              {finance_filter}
              {fitness_filter}
              {location_filter}
            ORDER BY n.id, (COALESCE(e.weight, 0.5) * 0.6 + 
                            CASE WHEN n.attempt_count > 0
                                 THEN n.success_count::float / n.attempt_count
                                 ELSE 0.5 END * 0.4) DESC
            LIMIT ${idx}
        """

        # Replace placeholder — asyncpg doesn't support array literals cleanly,
        # so we use a simpler approach: build the query without the array join
        # when there are no completed nodes, and with it when there are.
        final_params: list
        if completed_node_ids:
            simple_query = f"""
                SELECT DISTINCT ON (n.id)
                    n.id, n.type, n.title, n.description, n.tags, n.domain,
                    n.requires_finances, n.requires_fitness_level, n.requires_skills,
                    n.requires_location, n.requires_time_hours,
                    n.attempt_count, n.success_count, n.avg_completion_hours,
                    COALESCE(
                        (SELECT MAX(e.weight) FROM ioo_edges e
                         WHERE e.to_node_id = n.id
                           AND e.from_node_id::text = ANY($2::text[])),
                        0.5
                    ) AS edge_weight,
                    CASE WHEN n.attempt_count > 0
                         THEN n.success_count::float / n.attempt_count
                         ELSE 0.5
                    END AS success_rate
                FROM ioo_nodes n
                WHERE n.is_active = TRUE
                  AND n.id NOT IN (
                      SELECT node_id FROM ioo_user_progress
                      WHERE user_id = $1 AND status IN ('completed', 'abandoned')
                  )
                  {finance_filter}
                  {fitness_filter}
                  {location_filter}
                ORDER BY n.id,
                         (COALESCE(
                            (SELECT MAX(e.weight) FROM ioo_edges e
                             WHERE e.to_node_id = n.id
                               AND e.from_node_id::text = ANY($2::text[])),
                            0.5) * 0.6 +
                          CASE WHEN n.attempt_count > 0
                               THEN n.success_count::float / n.attempt_count
                               ELSE 0.5 END * 0.4) DESC
                LIMIT ${idx}
            """
            final_params = [str(user_id), completed_node_ids] + params[1:-1] + [limit]
        else:
            simple_query = f"""
                SELECT
                    n.id, n.type, n.title, n.description, n.tags, n.domain,
                    n.requires_finances, n.requires_fitness_level, n.requires_skills,
                    n.requires_location, n.requires_time_hours,
                    n.attempt_count, n.success_count, n.avg_completion_hours,
                    0.5 AS edge_weight,
                    CASE WHEN n.attempt_count > 0
                         THEN n.success_count::float / n.attempt_count
                         ELSE 0.5
                    END AS success_rate
                FROM ioo_nodes n
                WHERE n.is_active = TRUE
                  AND n.id NOT IN (
                      SELECT node_id FROM ioo_user_progress
                      WHERE user_id = $1 AND status IN ('completed', 'abandoned')
                  )
                  {finance_filter}
                  {fitness_filter}
                  {location_filter}
                ORDER BY success_rate DESC
                LIMIT ${idx}
            """
            final_params = [str(user_id)] + params[1:-1] + [limit]

        rows = await fetch(simple_query, *final_params)
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Traversal recording
    # ------------------------------------------------------------------

    async def record_traversal(
        self,
        user_id: str,
        from_node_id: str,
        to_node_id: str,
    ) -> None:
        """Log that a user took an edge. Creates the edge if it doesn't exist."""
        await execute(
            """
            INSERT INTO ioo_edges (from_node_id, to_node_id, traversal_count)
            VALUES ($1, $2, 1)
            ON CONFLICT (from_node_id, to_node_id)
            DO UPDATE SET
                traversal_count = ioo_edges.traversal_count + 1,
                updated_at = NOW()
            """,
            str(from_node_id),
            str(to_node_id),
        )

    # ------------------------------------------------------------------
    # Outcome recording
    # ------------------------------------------------------------------

    async def record_node_outcome(
        self,
        user_id: str,
        node_id: str,
        success: bool,
        hours_taken: float = 0.0,
    ) -> None:
        """Update node aggregate stats after a user finishes (or fails) a node."""
        if success:
            await execute(
                """
                UPDATE ioo_nodes SET
                    attempt_count = attempt_count + 1,
                    success_count = success_count + 1,
                    avg_completion_hours = CASE
                        WHEN avg_completion_hours IS NULL THEN $2
                        ELSE (avg_completion_hours * success_count + $2) / (success_count + 1)
                    END,
                    updated_at = NOW()
                WHERE id = $1
                """,
                str(node_id),
                float(hours_taken),
            )
        else:
            await execute(
                """
                UPDATE ioo_nodes SET
                    attempt_count = attempt_count + 1,
                    updated_at = NOW()
                WHERE id = $1
                """,
                str(node_id),
            )

        # Also update edges that lead to this node
        await self._update_edges_for_node(node_id, success)

    async def _update_edges_for_node(self, node_id: str, success: bool) -> None:
        """Increment success_count on edges pointing to this node if successful."""
        if success:
            await execute(
                """
                UPDATE ioo_edges SET
                    success_count = success_count + 1,
                    updated_at = NOW()
                WHERE to_node_id = $1
                """,
                str(node_id),
            )

    # ------------------------------------------------------------------
    # Edge weight recalculation
    # ------------------------------------------------------------------

    async def update_edge_weights(self) -> int:
        """
        Recalculate all edge weights based on success_count / traversal_count.
        Weight = 0.5 + 0.5 * success_rate (so minimum is 0.5 for untested edges).
        Returns number of edges updated.
        """
        result = await execute(
            """
            UPDATE ioo_edges SET
                weight = CASE
                    WHEN traversal_count > 0
                    THEN LEAST(1.0, 0.3 + 0.7 * (success_count::float / traversal_count))
                    ELSE 0.5
                END,
                updated_at = NOW()
            WHERE traversal_count > 0
            """
        )
        # parse "UPDATE N"
        try:
            count = int(str(result).split()[-1])
        except Exception:
            count = 0
        return count

    # ------------------------------------------------------------------
    # Seed initial nodes
    # ------------------------------------------------------------------

    async def seed_initial_nodes(self) -> dict:
        """
        Seed 20 diverse nodes across all types and domains.
        Idempotent — skips nodes that already exist by title.
        """
        existing = await fetchval("SELECT COUNT(*) FROM ioo_nodes")
        if existing and existing >= 10:
            return {"seeded": 0, "existing": existing}

        nodes = [
            # ── GOALS ──────────────────────────────────────────────────
            {
                "type": "goal", "title": "Run a 5K",
                "description": "Complete a 5-kilometer run without stopping.",
                "tags": ["fitness", "running", "endurance"],
                "domain": "Animus",
                "requires_fitness_level": 2, "requires_time_hours": 8.0,
                "requires_finances": 30.0,
            },
            {
                "type": "goal", "title": "Travel solo to a new country",
                "description": "Plan and complete a solo trip abroad.",
                "tags": ["travel", "adventure", "independence"],
                "domain": "Animus",
                "requires_finances": 1500.0, "requires_time_hours": 80.0,
            },
            {
                "type": "goal", "title": "Build a side project and launch it",
                "description": "Ship a working product or creative project publicly.",
                "tags": ["creativity", "tech", "entrepreneurship"],
                "domain": "iVive",
                "requires_time_hours": 120.0, "requires_skills": ["coding", "design"],
            },
            {
                "type": "goal", "title": "Volunteer for a cause you care about",
                "description": "Contribute regular time to a meaningful organization.",
                "tags": ["community", "meaning", "contribution"],
                "domain": "Eviva",
                "requires_time_hours": 20.0,
            },
            {
                "type": "goal", "title": "Learn a musical instrument",
                "description": "Play a song you love from start to finish.",
                "tags": ["music", "creativity", "learning"],
                "domain": "iVive",
                "requires_finances": 200.0, "requires_time_hours": 60.0,
            },
            # ── SUB-GOALS ─────────────────────────────────────────────
            {
                "type": "sub_goal", "title": "Run 3 times per week for a month",
                "description": "Build the running habit before race day.",
                "tags": ["fitness", "habit", "running"],
                "domain": "Animus",
                "requires_fitness_level": 1, "requires_time_hours": 12.0,
            },
            {
                "type": "sub_goal", "title": "Get a valid passport",
                "description": "Apply for or renew your passport.",
                "tags": ["travel", "admin"],
                "domain": "Animus",
                "requires_finances": 150.0, "requires_time_hours": 2.0,
            },
            {
                "type": "sub_goal", "title": "Define your MVP scope",
                "description": "Write a one-page spec for the smallest shippable version.",
                "tags": ["product", "planning", "tech"],
                "domain": "iVive",
                "requires_time_hours": 3.0,
            },
            {
                "type": "sub_goal", "title": "Find 3 volunteer opportunities near you",
                "description": "Research and shortlist organizations that match your values.",
                "tags": ["community", "research"],
                "domain": "Eviva",
                "requires_time_hours": 1.0,
            },
            {
                "type": "sub_goal", "title": "Buy a beginner instrument",
                "description": "Purchase or borrow a ukulele, keyboard, or guitar.",
                "tags": ["music", "gear"],
                "domain": "iVive",
                "requires_finances": 100.0, "requires_time_hours": 1.0,
            },
            # ── EXPERIENCES ───────────────────────────────────────────
            {
                "type": "experience", "title": "Enter a local 5K race",
                "description": "Sign up and show up on race day.",
                "tags": ["fitness", "running", "social"],
                "domain": "Animus",
                "requires_fitness_level": 3, "requires_finances": 35.0,
                "requires_time_hours": 3.0,
            },
            {
                "type": "experience", "title": "Book a one-way flight",
                "description": "Commit to the trip by booking without a return ticket.",
                "tags": ["travel", "adventure", "commitment"],
                "domain": "Animus",
                "requires_finances": 400.0, "requires_time_hours": 1.0,
            },
            {
                "type": "experience", "title": "Show your project to 5 strangers",
                "description": "Get real feedback from people who don't know you.",
                "tags": ["product", "vulnerability", "feedback"],
                "domain": "Eviva",
                "requires_time_hours": 3.0,
            },
            {
                "type": "experience", "title": "Attend a community volunteer day",
                "description": "Show up for a single organized volunteer event.",
                "tags": ["community", "social"],
                "domain": "Eviva",
                "requires_time_hours": 5.0,
            },
            {
                "type": "experience", "title": "Play your first song for someone",
                "description": "Perform one song — however rough — for another person.",
                "tags": ["music", "vulnerability", "social"],
                "domain": "iVive",
                "requires_time_hours": 0.5,
            },
            # ── ACTIVITIES ────────────────────────────────────────────
            {
                "type": "activity", "title": "Go for a 20-minute run",
                "description": "Lace up and run for 20 minutes at a comfortable pace.",
                "tags": ["fitness", "running", "daily"],
                "domain": "Animus",
                "requires_fitness_level": 0, "requires_time_hours": 0.5,
            },
            {
                "type": "activity", "title": "Pack a backpack for one week",
                "description": "Practice minimalist packing: fit everything in carry-on.",
                "tags": ["travel", "preparation"],
                "domain": "Animus",
                "requires_time_hours": 1.0,
            },
            {
                "type": "activity", "title": "Spend 2 hours coding your MVP",
                "description": "Dedicated deep-work session on your project.",
                "tags": ["tech", "focus", "creation"],
                "domain": "iVive",
                "requires_time_hours": 2.0, "requires_skills": ["coding"],
            },
            {
                "type": "activity", "title": "Write a kindness letter",
                "description": "Write a heartfelt letter to someone who needs encouragement.",
                "tags": ["community", "writing", "kindness"],
                "domain": "Eviva",
                "requires_time_hours": 0.5,
            },
            {
                "type": "activity", "title": "Practice 15 minutes of scales",
                "description": "Daily instrument practice session.",
                "tags": ["music", "practice", "daily"],
                "domain": "iVive",
                "requires_time_hours": 0.25,
            },
        ]

        seeded = 0
        # Seed edges: activities → sub_goals → experiences → goals (logical flow)
        node_ids: dict = {}

        for node in nodes:
            existing_id = await fetchval(
                "SELECT id FROM ioo_nodes WHERE title = $1",
                node["title"],
            )
            if existing_id:
                node_ids[node["title"]] = str(existing_id)
                continue

            tags = node.get("tags", [])
            skills = node.get("requires_skills", [])
            row = await fetchrow(
                """
                INSERT INTO ioo_nodes
                    (type, title, description, tags, domain,
                     requires_finances, requires_fitness_level, requires_skills,
                     requires_location, requires_time_hours)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                RETURNING id
                """,
                node["type"],
                node["title"],
                node.get("description"),
                tags,
                node.get("domain"),
                node.get("requires_finances"),
                node.get("requires_fitness_level", 0),
                skills,
                node.get("requires_location"),
                node.get("requires_time_hours"),
            )
            if row:
                node_ids[node["title"]] = str(row["id"])
                seeded += 1

        # Seed canonical edges (activity → sub_goal → experience → goal)
        edge_pairs = [
            ("Go for a 20-minute run", "Run 3 times per week for a month"),
            ("Run 3 times per week for a month", "Enter a local 5K race"),
            ("Enter a local 5K race", "Run a 5K"),
            ("Pack a backpack for one week", "Book a one-way flight"),
            ("Get a valid passport", "Book a one-way flight"),
            ("Book a one-way flight", "Travel solo to a new country"),
            ("Spend 2 hours coding your MVP", "Define your MVP scope"),
            ("Define your MVP scope", "Show your project to 5 strangers"),
            ("Show your project to 5 strangers", "Build a side project and launch it"),
            ("Write a kindness letter", "Find 3 volunteer opportunities near you"),
            ("Find 3 volunteer opportunities near you", "Attend a community volunteer day"),
            ("Attend a community volunteer day", "Volunteer for a cause you care about"),
            ("Practice 15 minutes of scales", "Buy a beginner instrument"),
            ("Buy a beginner instrument", "Play your first song for someone"),
            ("Play your first song for someone", "Learn a musical instrument"),
        ]
        edges_created = 0
        for from_title, to_title in edge_pairs:
            from_id = node_ids.get(from_title)
            to_id = node_ids.get(to_title)
            if from_id and to_id:
                try:
                    await execute(
                        """
                        INSERT INTO ioo_edges (from_node_id, to_node_id)
                        VALUES ($1, $2)
                        ON CONFLICT (from_node_id, to_node_id) DO NOTHING
                        """,
                        from_id,
                        to_id,
                    )
                    edges_created += 1
                except Exception as e:
                    logger.warning(f"Edge seed failed ({from_title} → {to_title}): {e}")

        logger.info(f"IOO seed: {seeded} nodes, {edges_created} edges created")
        return {"seeded": seeded, "edges_created": edges_created, "existing": existing}


# Module-level singleton
_graph_agent: Optional[IOOGraphAgent] = None


def get_graph_agent() -> IOOGraphAgent:
    global _graph_agent
    if _graph_agent is None:
        _graph_agent = IOOGraphAgent()
    return _graph_agent
