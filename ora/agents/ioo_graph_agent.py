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
import math
import re
from datetime import datetime, timezone
from typing import Optional, List
from uuid import UUID

from core.config import settings
from core.database import fetch, fetchrow, fetchval, execute
from ora.agents.recommendation_engine import (
    _embedding_to_pgvector,
    _hash_text_to_embedding,
)

logger = logging.getLogger(__name__)


def _parse_pgvector(raw) -> List[float]:
    """Convert asyncpg/pgvector/string values into a Python vector."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [float(v) for v in raw]
    if isinstance(raw, tuple):
        return [float(v) for v in raw]
    text = str(raw).strip()
    if not text:
        return []
    try:
        return [float(v) for v in text.strip("[]").split(",") if v.strip()]
    except Exception:
        try:
            return [float(v) for v in json.loads(text)]
        except Exception:
            return []


def _normalize_vector(values: List[float]) -> List[float]:
    norm = math.sqrt(sum(v * v for v in values))
    if norm <= 0:
        return values
    return [v / norm for v in values]


def _node_embedding_text(node: dict) -> str:
    tags = node.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = [tags]
    return " | ".join(
        str(part)
        for part in [
            f"title: {node.get('title') or ''}",
            f"description: {node.get('description') or ''}",
            f"domain: {node.get('domain') or ''}",
            f"tags: {', '.join(tags)}",
            f"node_type: {node.get('type') or node.get('node_type') or ''}",
            f"step_type: {node.get('step_type') or ''}",
            f"physical_context: {node.get('physical_context') or ''}",
            f"best_time: {node.get('best_time') or ''}",
            f"goal_category: {node.get('goal_category') or ''}",
        ]
        if part
    )

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
    # Vector embeddings / recommendations
    # ------------------------------------------------------------------

    async def _ensure_vector_schema(self) -> None:
        """Idempotently add IOO vector columns/indexes."""
        await execute("ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS embedding vector(1536)")
        await execute("ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS goal_category TEXT")
        await execute("ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS step_type TEXT DEFAULT 'hybrid'")
        await execute("ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS physical_context TEXT")
        await execute("ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS best_time TEXT")
        await execute("ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS requirements JSONB DEFAULT '{}'")
        await execute("ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS prerequisite_nodes UUID[] DEFAULT '{}'")
        await execute("ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS estimated_duration_days INTEGER")
        await execute("ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS difficulty_level INTEGER DEFAULT 5")
        await execute(
            """
            CREATE TABLE IF NOT EXISTS ioo_node_proposals (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                title TEXT NOT NULL,
                description TEXT,
                goal_category TEXT,
                step_type TEXT DEFAULT 'hybrid',
                domain TEXT,
                tags TEXT[] DEFAULT '{}',
                source_url TEXT,
                confidence FLOAT DEFAULT 0.5,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )
        await execute("ALTER TABLE ioo_user_state ADD COLUMN IF NOT EXISTS embedding vector(1536)")
        await execute("ALTER TABLE ioo_user_state ADD COLUMN IF NOT EXISTS embedding_updated_at TIMESTAMPTZ")
        try:
            await execute(
                """
                CREATE INDEX IF NOT EXISTS idx_ioo_nodes_embedding
                ON ioo_nodes USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 20)
                """
            )
        except Exception as e:
            logger.debug(f"IOO vector index creation skipped: {e}")

    async def _embed_text(self, text: str) -> List[float]:
        """Embed text with OpenAI when configured; otherwise deterministic hash fallback."""
        api_key = getattr(settings, "OPENAI_API_KEY", "")
        if api_key:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=15.0) as client:
                    r = await client.post(
                        "https://api.openai.com/v1/embeddings",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={"model": "text-embedding-3-small", "input": text[:8000]},
                    )
                    r.raise_for_status()
                    return r.json()["data"][0]["embedding"]
            except Exception as e:
                logger.warning(f"IOO OpenAI embedding failed: {e} — using hash fallback")
        return _hash_text_to_embedding(text)

    async def embed_all_nodes(self) -> int:
        """Generate and store embeddings for all IOO nodes that don't have one yet.
        Uses OpenAI text-embedding-3-small (1536 dims) when key is available.
        Returns number of nodes embedded."""
        await self._ensure_vector_schema()
        rows = await fetch(
            """
            SELECT id, type, title, description, tags, domain, goal_category, step_type, physical_context, best_time
            FROM ioo_nodes
            WHERE is_active = TRUE AND embedding IS NULL
            ORDER BY created_at ASC
            """
        )
        embedded = 0
        for row in rows:
            node = dict(row)
            emb = await self._embed_text(_node_embedding_text(node))
            if not emb:
                continue
            await execute(
                "UPDATE ioo_nodes SET embedding = $2::vector, updated_at = NOW() WHERE id = $1",
                str(node["id"]),
                _embedding_to_pgvector(emb),
            )
            embedded += 1
        return embedded

    async def _capability_filter_sql(self, user_id: str, start_idx: int = 3) -> tuple[str, list]:
        user_state = await fetchrow("SELECT * FROM ioo_user_state WHERE user_id = $1", str(user_id))
        clauses: list[str] = []
        params: list = []
        idx = start_idx
        if user_state:
            budget = user_state["finances_monthly_budget_usd"]
            if budget is not None:
                clauses.append(f"AND (n.requires_finances IS NULL OR n.requires_finances <= ${idx})")
                params.append(float(budget)); idx += 1
            fitness = user_state["fitness_level"]
            if fitness is not None:
                clauses.append(f"AND (n.requires_fitness_level IS NULL OR n.requires_fitness_level <= ${idx})")
                params.append(int(fitness)); idx += 1
            city = user_state["location_city"]
            country = user_state["location_country"]
            if city or country:
                clauses.append(f"AND (n.requires_location IS NULL OR n.requires_location ILIKE ${idx} OR n.requires_location ILIKE ${idx+1})")
                params.extend([f"%{city}%" if city else "%", f"%{country}%" if country else "%"])
                idx += 2

            # Physical feasibility: only suggest physical/hybrid steps that fit
            # known available time and basic transport constraints.
            weekday = user_state["free_time_weekday_hours"]
            weekend = user_state["free_time_weekend_hours"]
            available_time = max(
                float(weekday or 0),
                float(weekend or 0),
            )
            if available_time > 0:
                clauses.append(
                    f"AND (n.step_type = 'digital' OR n.requires_time_hours IS NULL OR n.requires_time_hours <= ${idx})"
                )
                params.append(available_time)
                idx += 1

            has_car = user_state["has_car"]
            if has_car is False:
                clauses.append(
                    "AND (n.physical_context IS NULL OR n.physical_context NOT ILIKE '%car%')"
                )
        return "\n".join(clauses), params

    async def vector_recommend(
        self,
        user_id: str,
        goal_context: str,
        limit: int = 10,
        preference: str = "mixed",
    ) -> list:
        """
        Vector similarity recommendation using the user's goal context.
        1. Embed the user's current goal/context string
        2. Find IOO nodes with cosine similarity (pgvector <=> operator)
        3. Blend with capability filters and edge weight scoring
        4. Return ranked list of recommended nodes
        """
        await self._ensure_vector_schema()
        if await fetchval("SELECT COUNT(*) FROM ioo_nodes WHERE is_active = TRUE AND embedding IS NULL"):
            await self.embed_all_nodes()

        goal_emb = await self._embed_text(goal_context or "personal growth real-world experience")
        query_vec = _embedding_to_pgvector(goal_emb)

        completed_rows = await fetch(
            "SELECT node_id FROM ioo_user_progress WHERE user_id = $1 AND status = 'completed'",
            str(user_id),
        )
        completed_ids = [str(r["node_id"]) for r in completed_rows]
        capability_sql, capability_params = await self._capability_filter_sql(str(user_id), start_idx=4)
        preference = preference if preference in ("prefer_digital", "prefer_physical", "mixed") else "mixed"
        preference_score_sql = ""
        if preference == "prefer_digital":
            preference_score_sql = " + CASE WHEN n.step_type = 'digital' THEN 0.10 WHEN n.step_type = 'hybrid' THEN 0.05 ELSE 0 END"
        elif preference == "prefer_physical":
            preference_score_sql = " + CASE WHEN n.step_type = 'physical' THEN 0.10 WHEN n.step_type = 'hybrid' THEN 0.05 ELSE 0 END"

        rows = await fetch(
            f"""
            SELECT
                n.id, n.type, n.title, n.description, n.tags, n.domain, n.goal_category,
                n.step_type, n.physical_context, n.best_time,
                n.requires_finances, n.requires_fitness_level, n.requires_skills,
                n.requires_location, n.requires_time_hours,
                n.attempt_count, n.success_count, n.avg_completion_hours,
                1 - (n.embedding <=> $2::vector) AS vector_similarity,
                COALESCE((
                    SELECT MAX(e.weight) FROM ioo_edges e
                    WHERE e.to_node_id = n.id
                      AND e.from_node_id::text = ANY($3::text[])
                ), 0.5) AS edge_weight,
                CASE WHEN n.attempt_count > 0
                     THEN n.success_count::float / n.attempt_count
                     ELSE 0.5
                END AS success_rate
            FROM ioo_nodes n
            WHERE n.is_active = TRUE
              AND n.embedding IS NOT NULL
              AND n.id NOT IN (
                  SELECT node_id FROM ioo_user_progress
                  WHERE user_id = $1 AND status IN ('completed', 'abandoned')
              )
              {capability_sql}
            ORDER BY (
                (1 - (n.embedding <=> $2::vector)) * 0.60 +
                COALESCE((
                    SELECT MAX(e.weight) FROM ioo_edges e
                    WHERE e.to_node_id = n.id
                      AND e.from_node_id::text = ANY($3::text[])
                ), 0.5) * 0.25 +
                (CASE WHEN n.attempt_count > 0 THEN n.success_count::float / n.attempt_count ELSE 0.5 END) * 0.15
                {preference_score_sql}
            ) DESC
            LIMIT ${4 + len(capability_params)}
            """,
            str(user_id),
            query_vec,
            completed_ids,
            *capability_params,
            limit,
        )
        return [dict(r) for r in rows]


    async def find_path(
        self,
        user_id: str,
        goal_node_id: str,
        max_steps: int = 10,
        preference: str = "mixed",
    ) -> list[dict]:
        """
        Compute the optimal step-by-step path from user's current position to a goal node.
        Like Google Maps turn-by-turn directions.

        Returns ordered list of nodes: [step1, step2, step3, ... goal]
        Each step has: node_id, title, description, estimated_time, why_this_step
        """
        await self._ensure_vector_schema()

        goal = await fetchrow(
            """
            SELECT id, type, title, description, tags, domain, requires_time_hours, embedding,
                   step_type, physical_context, best_time, goal_category
            FROM ioo_nodes
            WHERE id = $1::uuid AND is_active = TRUE
            """,
            str(goal_node_id),
        )
        if not goal:
            return []

        completed_rows = await fetch(
            """
            SELECT node_id FROM ioo_user_progress
            WHERE user_id = $1 AND status = 'completed'
            """,
            str(user_id),
        )
        completed_ids = {str(r["node_id"]) for r in completed_rows}
        if str(goal["id"]) in completed_ids:
            return []

        capability_sql, capability_params = await self._capability_filter_sql(str(user_id), start_idx=3)
        cap_param_count = len(capability_params)
        shifted_capability_sql = re.sub(
            r"\$(\d+)",
            lambda m: f"${int(m.group(1)) + cap_param_count}",
            capability_sql,
        )
        goal_param_idx = 3 + cap_param_count * 2
        preference = preference if preference in ("prefer_digital", "prefer_physical", "mixed") else "mixed"
        path_preference_sql = ""
        if preference == "prefer_digital":
            path_preference_sql = "CASE WHEN n.step_type = 'digital' THEN 1.10 WHEN n.step_type = 'hybrid' THEN 1.05 ELSE 0.95 END"
        elif preference == "prefer_physical":
            path_preference_sql = "CASE WHEN n.step_type = 'physical' THEN 1.10 WHEN n.step_type = 'hybrid' THEN 1.05 ELSE 0.95 END"
        else:
            path_preference_sql = "1.0"

        # First try real graph pathfinding: reverse BFS/DFS from current completed
        # nodes (or root activities when user has no completed nodes) toward the goal.
        rows = await fetch(
            f"""
            WITH RECURSIVE paths AS (
                -- Starting point: completed nodes if present, otherwise low-friction roots.
                SELECT
                    n.id AS node_id,
                    ARRAY[n.id] AS path_ids,
                    0 AS depth,
                    1.0::float AS path_quality,
                    COALESCE(n.requires_time_hours, 1)::float AS total_time
                FROM ioo_nodes n
                WHERE n.is_active = TRUE
                  AND (
                    (cardinality($1::uuid[]) > 0 AND n.id = ANY($1::uuid[]))
                    OR
                    (cardinality($1::uuid[]) = 0 AND n.id NOT IN (SELECT to_node_id FROM ioo_edges))
                  )
                  {capability_sql}

                UNION ALL

                SELECT
                    e.to_node_id AS node_id,
                    p.path_ids || e.to_node_id,
                    p.depth + 1,
                    p.path_quality * COALESCE(e.weight, 0.5)::float *
                        (CASE WHEN n.attempt_count > 0 THEN n.success_count::float / n.attempt_count ELSE 0.5 END) *
                        ({path_preference_sql}),
                    p.total_time + COALESCE(n.requires_time_hours, 1)::float
                FROM paths p
                JOIN ioo_edges e ON e.from_node_id = p.node_id
                JOIN ioo_nodes n ON n.id = e.to_node_id
                WHERE p.depth < $2
                  AND n.is_active = TRUE
                  AND NOT e.to_node_id = ANY(p.path_ids)
                  AND NOT e.to_node_id = ANY($1::uuid[])
                  {shifted_capability_sql}
            )
            SELECT path_ids, depth, path_quality, total_time
            FROM paths
            WHERE node_id = ${goal_param_idx}::uuid
            ORDER BY
                depth ASC,
                path_quality DESC,
                total_time ASC
            LIMIT 1
            """,
            list(completed_ids),
            max_steps,
            *capability_params,
            *capability_params,
            str(goal_node_id),
        )

        path_ids: list[str] = []
        if rows:
            raw_ids = rows[0]["path_ids"]
            path_ids = [str(pid) for pid in raw_ids if str(pid) not in completed_ids]

        # Fallback: if graph edges are sparse, use vector recommendations as
        # semantic stepping stones and end at the goal. This keeps the UX useful
        # while the IOO graph is still learning edges.
        if not path_ids:
            goal_context = _node_embedding_text(dict(goal))
            semantic_steps = await self.vector_recommend(
                user_id=str(user_id),
                goal_context=goal_context,
                limit=max(1, max_steps - 1),
                preference=preference,
            )
            seen = set(completed_ids)
            for step in semantic_steps:
                sid = str(step["id"])
                if sid == str(goal_node_id) or sid in seen:
                    continue
                path_ids.append(sid)
                seen.add(sid)
            path_ids.append(str(goal_node_id))

        if str(goal_node_id) not in path_ids:
            path_ids.append(str(goal_node_id))
        path_ids = path_ids[:max_steps]

        node_rows = await fetch(
            """
            SELECT id, type, title, description, tags, domain, requires_time_hours,
                   step_type, physical_context, best_time,
                   requires_finances, requires_fitness_level, attempt_count, success_count
            FROM ioo_nodes
            WHERE id::text = ANY($1::text[])
            """,
            path_ids,
        )
        by_id = {str(r["id"]): dict(r) for r in node_rows}
        ordered: list[dict] = []
        total = len(path_ids)
        for index, node_id in enumerate(path_ids, start=1):
            node = by_id.get(str(node_id))
            if not node:
                continue
            is_goal = str(node_id) == str(goal_node_id)
            if is_goal:
                why = "This is your destination — the outcome Ora is mapping you toward."
            elif index == 1:
                why = "This is the next reachable step from where you are now."
            else:
                why = "This builds on the previous step and moves you closer to the destination."
            if node.get("attempt_count"):
                success_rate = (node.get("success_count") or 0) / max(float(node.get("attempt_count") or 1), 1.0)
                why += f" Similar users complete it about {round(success_rate * 100)}% of the time."
            ordered.append({
                "step": index,
                "node_id": str(node["id"]),
                "title": node.get("title"),
                "description": node.get("description"),
                "type": node.get("type"),
                "domain": node.get("domain"),
                "estimated_time": float(node["requires_time_hours"]) if node.get("requires_time_hours") is not None else None,
                "step_type": node.get("step_type") or "hybrid",
                "physical_context": node.get("physical_context"),
                "best_time": node.get("best_time"),
                "why_this_step": why,
                "is_goal": is_goal,
                "remaining_steps": max(total - index, 0),
            })
        return ordered


    async def check_node_eligibility(self, user_id: str, node_id: str) -> dict:
        """Check if a user is eligible to attempt a node and identify gaps."""
        await self._ensure_vector_schema()
        node = await fetchrow(
            """
            SELECT id, title, requirements, prerequisite_nodes, requires_finances, requires_fitness_level,
                   requires_skills, requires_location, requires_time_hours, step_type,
                   physical_context
            FROM ioo_nodes WHERE id = $1::uuid AND is_active = TRUE
            """,
            str(node_id),
        )
        if not node:
            return {"eligible": False, "gaps": [{"type": "missing_node", "message": "Node not found"}], "bridge_nodes": []}

        state = await fetchrow("SELECT * FROM ioo_user_state WHERE user_id = $1", str(user_id))
        completed = await fetch(
            "SELECT node_id::text FROM ioo_user_progress WHERE user_id = $1 AND status = 'completed'",
            str(user_id),
        )
        completed_ids = {r["node_id"] for r in completed}
        req = node["requirements"] or {}
        if isinstance(req, str):
            try:
                req = json.loads(req)
            except Exception:
                req = {}

        gaps: list[dict] = []
        def gap(kind: str, message: str, **extra):
            gaps.append({"type": kind, "message": message, **extra})

        budget_req = req.get("min_budget_usd") or node["requires_finances"]
        if budget_req is not None and state and state["finances_monthly_budget_usd"] is not None:
            if float(state["finances_monthly_budget_usd"]) < float(budget_req):
                gap("budget", f"Needs about ${float(budget_req):.0f} available budget", required=float(budget_req), current=float(state["finances_monthly_budget_usd"]))

        fitness_req = req.get("min_fitness_level") or node["requires_fitness_level"]
        current_fitness = state["fitness_level"] if state else None
        if fitness_req is not None and current_fitness is not None and int(current_fitness) < int(fitness_req):
            gap("fitness", f"Needs fitness level {fitness_req}; current estimate is {current_fitness}", required=int(fitness_req), current=int(current_fitness))

        needed_skills = set(req.get("required_skills") or node["requires_skills"] or [])
        known_skills = set(state["known_skills"] or []) if state else set()
        missing_skills = sorted(needed_skills - known_skills)
        if missing_skills:
            gap("skills", "Missing prerequisite skills", missing=missing_skills)

        prereqs = [str(x) for x in (req.get("prerequisite_node_ids") or node["prerequisite_nodes"] or [])]
        missing_prereqs = [pid for pid in prereqs if pid not in completed_ids]
        if missing_prereqs:
            gap("prior_nodes", "Needs prerequisite nodes completed first", missing=missing_prereqs)

        if node["step_type"] in ("physical", "hybrid") and state:
            available_time = max(float(state["free_time_weekday_hours"] or 0), float(state["free_time_weekend_hours"] or 0))
            required_time = req.get("time_per_week_hours") or node["requires_time_hours"]
            if required_time and available_time and available_time < float(required_time):
                gap("time", f"Needs {float(required_time):.1f}h available; current estimate is {available_time:.1f}h", required=float(required_time), current=available_time)
            if (req.get("requires_location") or node["requires_location"]) and not (state["location_city"] or state["location_country"]):
                gap("location", "Needs a location context to find a feasible physical option")
            equipment = req.get("required_equipment") or []
            if any(str(e).lower() in ("gym_membership", "car", "transport") for e in equipment):
                if "car" in [str(e).lower() for e in equipment] and state["has_car"] is False:
                    gap("transport", "Needs transport access or a closer alternative")

        bridge_nodes = []
        if gaps:
            ids = await self.spawn_prerequisite_nodes(user_id, node_id, gaps)
            if ids:
                rows = await fetch("SELECT id, title, description, step_type FROM ioo_nodes WHERE id::text = ANY($1::text[])", ids)
                bridge_nodes = [dict(r) for r in rows]
        return {"eligible": not gaps, "gaps": gaps, "bridge_nodes": bridge_nodes}

    async def spawn_prerequisite_nodes(self, user_id: str, node_id: str, gaps: list) -> list[str]:
        """Find/create bridge nodes for unmet requirements."""
        await self._ensure_vector_schema()
        created_or_found: list[str] = []
        target = await fetchrow("SELECT title, goal_category, domain FROM ioo_nodes WHERE id = $1::uuid", str(node_id))
        target_title = target["title"] if target else "this goal"
        category = target["goal_category"] if target else None
        domain = target["domain"] if target else "iVive"

        templates = {
            "budget": ("Create a simple budget for {target}", "Work out the minimum cost and choose the lowest-friction way to fund this step.", "digital", ["finance", "planning"]),
            "fitness": ("Complete a 2-week foundation program for {target}", "Build baseline strength/endurance before attempting the next physical step.", "physical", ["fitness", "foundation"]),
            "skills": ("Learn the basic skills needed for {target}", "Complete a short primer/practice block for the missing prerequisite skills.", "digital", ["learning", "skills"]),
            "prior_nodes": ("Complete prerequisite step for {target}", "Finish the required prior node before moving forward.", "hybrid", ["prerequisite"]),
            "time": ("Schedule time blocks for {target}", "Create protected calendar time so this goal has room in the week.", "digital", ["planning", "time"]),
            "location": ("Find a nearby place for {target}", "Locate a feasible nearby option based on your city and transport constraints.", "digital", ["local", "planning"]),
            "transport": ("Find a walkable or transit-accessible option for {target}", "Avoid car-dependency by finding a closer route or transit option.", "digital", ["transport", "local"]),
        }

        for gap in gaps:
            tpl = templates.get(gap.get("type"), ("Prepare for {target}", "Close the prerequisite gap before attempting this step.", "hybrid", ["preparation"]))
            title = tpl[0].format(target=target_title)
            existing = await fetchrow(
                """
                SELECT id FROM ioo_nodes
                WHERE lower(title) = lower($1) OR title ILIKE $2
                LIMIT 1
                """,
                title,
                f"%{title[:40]}%",
            )
            if existing:
                created_or_found.append(str(existing["id"]))
                continue
            proposal = await fetchrow(
                """
                INSERT INTO ioo_node_proposals
                    (title, description, goal_category, step_type, domain, tags, confidence, status)
                VALUES ($1,$2,$3,$4,$5,$6,0.86,'approved')
                RETURNING id
                """,
                title, tpl[1], category, tpl[2], domain, tpl[3],
            )
            row = await fetchrow(
                """
                INSERT INTO ioo_nodes
                    (type, title, description, tags, domain, step_type, goal_category,
                     requirements, difficulty_level)
                VALUES ('activity',$1,$2,$3,$4,$5,$6,$7::jsonb,3)
                RETURNING id
                """,
                title, tpl[1], tpl[3], domain, tpl[2], category,
                json.dumps({"bridge_for_node_id": str(node_id), "gap_type": gap.get("type")}),
            )
            if row:
                created_or_found.append(str(row["id"]))
                await execute(
                    "INSERT INTO ioo_edges (from_node_id, to_node_id) VALUES ($1::uuid,$2::uuid) ON CONFLICT DO NOTHING",
                    str(row["id"]), str(node_id),
                )
        return created_or_found

    async def build_personalised_path(
        self,
        user_id: str,
        goal_node_id: str,
        max_steps: int = 10,
        preference: str = "mixed",
    ) -> list[dict]:
        """Full GPS-style route with dynamically spawned prerequisite bridge nodes."""
        base_path = await self.find_path(user_id, goal_node_id, max_steps=max_steps, preference=preference)
        personalised: list[dict] = []
        seen: set[str] = set()
        for step in base_path:
            node_id = str(step["node_id"])
            eligibility = await self.check_node_eligibility(user_id, node_id)
            if not eligibility.get("eligible"):
                for bridge in eligibility.get("bridge_nodes", []):
                    bid = str(bridge["id"])
                    if bid in seen:
                        continue
                    personalised.append({
                        "step": len(personalised) + 1,
                        "node_id": bid,
                        "title": bridge.get("title"),
                        "description": bridge.get("description"),
                        "step_type": bridge.get("step_type") or "hybrid",
                        "is_prerequisite": True,
                        "unblocks_node_id": node_id,
                        "why_this_step": "Ora spawned this bridge step because a requirement gap blocks the next node.",
                    })
                    seen.add(bid)
            if node_id not in seen:
                step["step"] = len(personalised) + 1
                step["is_prerequisite"] = False
                step["eligibility"] = {"eligible": eligibility.get("eligible"), "gaps": eligibility.get("gaps", [])}
                personalised.append(step)
                seen.add(node_id)
        return personalised

    async def build_user_ioo_vector(self, user_id: str) -> List[float]:
        """Build and store the user's IOO fingerprint from completed/in-progress nodes."""
        await self._ensure_vector_schema()
        rows = await fetch(
            """
            SELECT p.status, p.started_at, p.completed_at, p.created_at,
                   n.id, n.embedding, n.title, n.description, n.tags, n.domain, n.type, n.goal_category
            FROM ioo_user_progress p
            JOIN ioo_nodes n ON n.id = p.node_id
            WHERE p.user_id = $1
              AND p.status IN ('completed', 'started', 'viewed')
            ORDER BY COALESCE(p.completed_at, p.started_at, p.created_at) DESC
            LIMIT 50
            """,
            str(user_id),
        )
        if not rows:
            return []

        vectors: list[tuple[List[float], float]] = []
        now = datetime.now(timezone.utc)
        for row in rows:
            raw = row["embedding"]
            if not raw:
                node = dict(row)
                emb = await self._embed_text(_node_embedding_text(node))
                await execute(
                    "UPDATE ioo_nodes SET embedding = $2::vector, updated_at = NOW() WHERE id = $1",
                    str(row["id"]),
                    _embedding_to_pgvector(emb),
                )
            else:
                emb = _parse_pgvector(raw)
            if not emb:
                continue
            event_at = row["completed_at"] or row["started_at"] or row["created_at"] or now
            if event_at.tzinfo is None:
                event_at = event_at.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (now - event_at).total_seconds() / 86400.0)
            recency_weight = 1.0 / (1.0 + age_days / 30.0)
            status_weight = {"completed": 1.0, "started": 0.65, "viewed": 0.35}.get(row["status"], 0.5)
            vectors.append((emb, recency_weight * status_weight))

        if not vectors:
            return []
        total_weight = sum(w for _, w in vectors) or 1.0
        dim = len(vectors[0][0])
        avg = [0.0] * dim
        for emb, weight in vectors:
            for i, val in enumerate(emb[:dim]):
                avg[i] += val * weight
        avg = _normalize_vector([v / total_weight for v in avg])
        await execute(
            """
            INSERT INTO ioo_user_state (user_id, embedding, embedding_updated_at)
            VALUES ($1::uuid, $2::vector, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET embedding = $2::vector, embedding_updated_at = NOW(), last_updated = NOW()
            """,
            str(user_id),
            _embedding_to_pgvector(avg),
        )
        return avg

    async def get_user_vector_summary(self, user_id: str) -> dict:
        """Return a safe summary of the user's IOO fingerprint, not the raw vector."""
        vector = await self.build_user_ioo_vector(user_id)
        progress_counts = await fetch(
            """
            SELECT status, COUNT(*) AS count
            FROM ioo_user_progress
            WHERE user_id = $1
            GROUP BY status
            """,
            str(user_id),
        )
        top_nodes = []
        if vector:
            try:
                rows = await fetch(
                    """
                    SELECT title, type, domain, 1 - (embedding <=> $1::vector) AS similarity
                    FROM ioo_nodes
                    WHERE embedding IS NOT NULL AND is_active = TRUE
                    ORDER BY embedding <=> $1::vector
                    LIMIT 5
                    """,
                    _embedding_to_pgvector(vector),
                )
                top_nodes = [dict(r) for r in rows]
            except Exception as e:
                logger.debug(f"IOO vector summary nearest nodes failed: {e}")
        return {
            "has_vector": bool(vector),
            "dimensions": len(vector) if vector else 0,
            "progress_counts": {r["status"]: r["count"] for r in progress_counts},
            "nearest_nodes": top_nodes,
        }

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
        await self.reinforce_node_signal(node_id, "complete" if success else "abandon", user_id=user_id)

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
    # Neural graph lifecycle: grow, reinforce, prune, split, merge
    # ------------------------------------------------------------------

    async def _log_graph_event(
        self,
        event_type: str,
        node_id: str | None = None,
        related_node_id: str | None = None,
        user_id: str | None = None,
        payload: dict | None = None,
    ) -> None:
        """Persist a lightweight memory of how the graph changed."""
        try:
            await execute(
                """
                INSERT INTO ioo_graph_events (event_type, user_id, node_id, related_node_id, payload)
                VALUES ($1, $2::uuid, $3::uuid, $4::uuid, $5::jsonb)
                """,
                event_type,
                str(user_id) if user_id else None,
                str(node_id) if node_id else None,
                str(related_node_id) if related_node_id else None,
                json.dumps(payload or {}),
            )
        except Exception as e:
            logger.debug(f"IOO graph event log skipped ({event_type}): {e}")

    async def reinforce_node_signal(
        self,
        node_id: str,
        signal: str,
        user_id: str | None = None,
        strength: float = 1.0,
    ) -> dict:
        """
        Update neural scores from explicit/implicit behaviour.

        Signals are deliberately broad so frontend surfaces, execution runs, and
        future agents can all feed the same graph: view, save, skip, start,
        complete, abandon, fulfilment_high, fulfilment_low.
        """
        engagement_delta = {
            "view": 0.01,
            "save": 0.04,
            "start": 0.08,
            "complete": 0.12,
            "skip": -0.03,
            "abandon": -0.06,
        }.get(signal, 0.0) * float(strength)
        fulfilment_delta = {
            "complete": 0.08,
            "fulfilment_high": 0.14,
            "fulfilment_low": -0.10,
            "abandon": -0.04,
        }.get(signal, 0.0) * float(strength)

        row = await fetchrow(
            """
            UPDATE ioo_nodes
            SET engagement_score = LEAST(1.0, GREATEST(0.0, COALESCE(engagement_score, 0.5) + $2)),
                fulfilment_score = LEAST(1.0, GREATEST(0.0, COALESCE(fulfilment_score, 0.5) + $3)),
                last_reinforced_at = CASE WHEN $2 > 0 OR $3 > 0 THEN NOW() ELSE last_reinforced_at END,
                updated_at = NOW()
            WHERE id = $1::uuid
            RETURNING id, engagement_score, fulfilment_score, neural_state
            """,
            str(node_id),
            engagement_delta,
            fulfilment_delta,
        )
        await self._log_graph_event(
            "reinforce" if engagement_delta >= 0 and fulfilment_delta >= 0 else "decay",
            node_id=node_id,
            user_id=user_id,
            payload={"signal": signal, "strength": strength, "engagement_delta": engagement_delta, "fulfilment_delta": fulfilment_delta},
        )
        return dict(row) if row else {}

    async def grow_node_from_angles(
        self,
        node_id: str,
        angles: list[str] | None = None,
        max_new: int = 4,
    ) -> list[dict]:
        """
        Spawn sibling/child possibilities around a node from multiple angles.

        This is the core anti-flat-card behaviour: one intention can branch into
        fastest, easiest, social, growth, low-cost, vitality, contribution, and
        novelty routes, then later be pruned/merged by outcomes.
        """
        base = await fetchrow("SELECT * FROM ioo_nodes WHERE id = $1::uuid AND is_active = TRUE", str(node_id))
        if not base:
            return []
        angles = angles or ["fastest", "easiest", "most_fulfilling", "most_social", "lowest_cost", "growth_edge"]
        created: list[dict] = []
        tags = list(base["tags"] or [])
        for angle in angles[:max_new]:
            title = f"{base['title']} — {angle.replace('_', ' ')} path"
            existing = await fetchrow(
                "SELECT id, title, growth_angle FROM ioo_nodes WHERE lower(title) = lower($1) LIMIT 1",
                title,
            )
            if existing:
                created.append(dict(existing))
                continue
            description = (
                f"A {angle.replace('_', ' ')} route for: {base['title']}. "
                f"Generated as an IOO neural-graph branch so Ora can test which pathway creates more engagement, experiences, activity, and fulfilment."
            )
            row = await fetchrow(
                """
                INSERT INTO ioo_nodes
                    (type, title, description, tags, domain, step_type, goal_category,
                     requirements, difficulty_level, generation_source, growth_angle,
                     parent_node_ids, neural_state)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,'neural_growth',$10,ARRAY[$11::uuid],'active')
                RETURNING id, title, growth_angle
                """,
                base["type"],
                title,
                description,
                list(dict.fromkeys(tags + [angle])),
                base["domain"],
                base["step_type"],
                base["goal_category"],
                json.dumps({"generated_from_node_id": str(node_id), "growth_angle": angle}),
                base["difficulty_level"],
                angle,
                str(node_id),
            )
            await execute(
                """
                INSERT INTO ioo_edges (from_node_id, to_node_id, relation_type, confidence, rationale)
                VALUES ($1::uuid, $2::uuid, 'branches_to', 0.72, $3)
                ON CONFLICT (from_node_id, to_node_id) DO UPDATE
                SET relation_type = 'branches_to', confidence = GREATEST(ioo_edges.confidence, 0.72), updated_at = NOW()
                """,
                str(node_id),
                str(row["id"]),
                f"Spawned as the {angle} branch of this IOO possibility.",
            )
            await execute("UPDATE ioo_nodes SET spawned_count = spawned_count + 1 WHERE id = $1::uuid", str(node_id))
            await self._log_graph_event("grow", node_id=node_id, related_node_id=str(row["id"]), payload={"angle": angle})
            created.append(dict(row))
        return created

    async def prune_underperforming_nodes(
        self,
        min_attempts: int = 8,
        max_nodes: int = 25,
    ) -> dict:
        """Conservatively deactivate weak branches while preserving history."""
        rows = await fetch(
            """
            UPDATE ioo_nodes
            SET is_active = FALSE,
                neural_state = 'pruned',
                pruned_at = NOW(),
                prune_reason = 'Low engagement/fulfilment after enough attempts',
                updated_at = NOW()
            WHERE id IN (
                SELECT id FROM ioo_nodes
                WHERE is_active = TRUE
                  AND neural_state <> 'pruned'
                  AND attempt_count >= $1
                  AND (COALESCE(success_count,0)::float / GREATEST(attempt_count,1)) < 0.18
                  AND COALESCE(engagement_score,0.5) < 0.35
                ORDER BY updated_at ASC
                LIMIT $2
            )
            RETURNING id, title
            """,
            int(min_attempts),
            int(max_nodes),
        )
        for row in rows:
            await self._log_graph_event("prune", node_id=str(row["id"]), payload={"reason": "underperforming"})
        return {"pruned": len(rows), "nodes": [dict(r) for r in rows]}

    async def split_node_by_tags(self, node_id: str, max_children: int = 3) -> list[dict]:
        """Split a broad/high-traffic node into sharper child nodes."""
        node = await fetchrow("SELECT * FROM ioo_nodes WHERE id = $1::uuid AND is_active = TRUE", str(node_id))
        if not node:
            return []
        tags = [t for t in list(node["tags"] or []) if t]
        if len(tags) < 2:
            return []
        children: list[dict] = []
        for tag in tags[:max_children]:
            title = f"{node['title']} — {tag} focus"
            existing = await fetchrow("SELECT id, title FROM ioo_nodes WHERE lower(title) = lower($1) LIMIT 1", title)
            if existing:
                children.append(dict(existing))
                continue
            row = await fetchrow(
                """
                INSERT INTO ioo_nodes
                    (type, title, description, tags, domain, step_type, goal_category,
                     requirements, difficulty_level, generation_source, growth_angle,
                     parent_node_ids, split_from_node_id, neural_state)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,'neural_split',$10,ARRAY[$11::uuid],$11::uuid,'active')
                RETURNING id, title
                """,
                node["type"], title, f"A sharper {tag} branch split from {node['title']}.", [tag],
                node["domain"], node["step_type"], node["goal_category"],
                json.dumps({"split_from_node_id": str(node_id), "focus_tag": tag}),
                node["difficulty_level"], tag, str(node_id),
            )
            await execute(
                "INSERT INTO ioo_edges (from_node_id, to_node_id, relation_type, confidence, rationale) VALUES ($1::uuid,$2::uuid,'splits_to',0.8,$3) ON CONFLICT DO NOTHING",
                str(node_id), str(row["id"]), f"Split broad node into focused {tag} child.",
            )
            await self._log_graph_event("split", node_id=node_id, related_node_id=str(row["id"]), payload={"tag": tag})
            children.append(dict(row))
        return children

    async def merge_duplicate_title_nodes(self, max_groups: int = 10) -> dict:
        """Merge exact duplicate titles into the strongest surviving node."""
        groups = await fetch(
            """
            SELECT lower(title) AS key, array_agg(id ORDER BY success_count DESC, attempt_count DESC, created_at ASC) AS ids
            FROM ioo_nodes
            WHERE is_active = TRUE
            GROUP BY lower(title)
            HAVING COUNT(*) > 1
            LIMIT $1
            """,
            int(max_groups),
        )
        merged = 0
        for group in groups:
            ids = [str(x) for x in group["ids"]]
            keep, duplicates = ids[0], ids[1:]
            for duplicate in duplicates:
                await execute(
                    """
                    UPDATE ioo_nodes
                    SET is_active = FALSE,
                        neural_state = 'merged',
                        merged_into_node_id = $2::uuid,
                        updated_at = NOW()
                    WHERE id = $1::uuid
                    """,
                    duplicate, keep,
                )
                await execute(
                    """
                    UPDATE ioo_edges
                    SET from_node_id = CASE WHEN from_node_id = $1::uuid THEN $2::uuid ELSE from_node_id END,
                        to_node_id = CASE WHEN to_node_id = $1::uuid THEN $2::uuid ELSE to_node_id END,
                        relation_type = COALESCE(relation_type, 'merged_path'),
                        updated_at = NOW()
                    WHERE from_node_id = $1::uuid OR to_node_id = $1::uuid
                    """,
                    duplicate, keep,
                )
                await execute(
                    "UPDATE ioo_nodes SET merged_from_node_ids = array_append(merged_from_node_ids, $2::uuid), updated_at = NOW() WHERE id = $1::uuid",
                    keep, duplicate,
                )
                await self._log_graph_event("merge", node_id=duplicate, related_node_id=keep)
                merged += 1
        return {"merged": merged, "groups": len(groups)}

    async def run_neural_lifecycle_sweep(self) -> dict:
        """One maintenance pass over the living IOO graph."""
        edge_weights_updated = await self.update_edge_weights()
        pruned = await self.prune_underperforming_nodes()
        merged = await self.merge_duplicate_title_nodes()
        candidates = await fetch(
            """
            SELECT id FROM ioo_nodes
            WHERE is_active = TRUE
              AND neural_state = 'active'
              AND attempt_count >= 10
              AND spawned_count < 3
              AND COALESCE(engagement_score,0.5) >= 0.62
            ORDER BY engagement_score DESC, success_count DESC
            LIMIT 5
            """
        )
        grown = []
        for row in candidates:
            grown.extend(await self.grow_node_from_angles(str(row["id"]), max_new=2))
        return {
            "edge_weights_updated": edge_weights_updated,
            "pruned": pruned,
            "merged": merged,
            "grown": len(grown),
            "principle": "grow from multiple angles, reinforce winners, prune weak branches, split/merge overloaded structure",
        }

    async def upsert_world_signal_node(self, signal: dict) -> dict:
        """
        Turn a live world signal/event/opportunity into an IOO node.

        World-aware Ora should continuously grow the IOO graph from live data:
        local events, opportunities, learning resources, trends, cultural moments,
        and useful places. These nodes are then reinforced/pruned by behaviour.
        """
        title = str(signal.get("title") or "Untitled world opportunity").strip()[:240]
        if not title:
            return {}
        url = str(signal.get("url") or "").strip()
        signal_type = str(signal.get("signal_type") or signal.get("type") or "opportunity").lower()
        source = str(signal.get("source") or "world")
        summary = str(signal.get("summary") or signal.get("description") or "")
        location = str(signal.get("location") or signal.get("city") or "")
        tags = signal.get("tags") or signal.get("relevance_tags") or []
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except Exception:
                tags = [tags]
        tags = [str(t).strip().lower() for t in tags if str(t).strip()]

        node_type = "experience" if signal_type in {"event", "weather", "historical"} else "activity"
        step_type = "physical" if signal_type == "event" or location not in {"", "Online", "online"} else "digital"
        domain = "Aventi" if signal_type in {"event", "weather", "historical", "inspiration"} else "Eviva"
        if any(t in tags for t in ["wellness", "fitness", "health", "mindfulness", "psychology", "wellbeing"]):
            domain = "iVive"
        requirements = {
            "world_signal": True,
            "world_signal_type": signal_type,
            "world_signal_source": source,
            "world_signal_url": url,
            "external_id": signal.get("external_id") or signal.get("id"),
            "starts_at": str(signal.get("starts_at") or ""),
            "raw_location": location,
        }

        existing = None
        if url:
            existing = await fetchrow(
                """
                SELECT id, title FROM ioo_nodes
                WHERE requirements->>'world_signal_url' = $1
                LIMIT 1
                """,
                url,
            )
        if not existing:
            existing = await fetchrow(
                """
                SELECT id, title FROM ioo_nodes
                WHERE generation_source = 'world_signal'
                  AND lower(title) = lower($1)
                  AND COALESCE(requires_location, '') = COALESCE($2, '')
                LIMIT 1
                """,
                title,
                location,
            )

        if existing:
            row = await fetchrow(
                """
                UPDATE ioo_nodes
                SET description = COALESCE(NULLIF($2, ''), description),
                    tags = $3,
                    requirements = COALESCE(requirements, '{}'::jsonb) || $4::jsonb,
                    engagement_score = LEAST(1.0, COALESCE(engagement_score, 0.5) + 0.01),
                    neural_state = 'active',
                    is_active = TRUE,
                    updated_at = NOW()
                WHERE id = $1::uuid
                RETURNING id, title, generation_source, growth_angle
                """,
                str(existing["id"]),
                summary,
                tags,
                json.dumps(requirements),
            )
            await self._log_graph_event("world_refresh", node_id=str(row["id"]), payload=requirements)
            return dict(row)

        row = await fetchrow(
            """
            INSERT INTO ioo_nodes
                (type, title, description, tags, domain, step_type, goal_category,
                 requires_location, requirements, difficulty_level, generation_source,
                 growth_angle, neural_state, engagement_score, fulfilment_score)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,'world_signal',$11,'active',$12,$13)
            RETURNING id, title, generation_source, growth_angle
            """,
            node_type,
            title,
            summary,
            tags,
            domain,
            step_type,
            signal_type,
            location or None,
            json.dumps(requirements),
            3 if signal_type == "event" else 4,
            signal_type,
            max(0.1, min(float(signal.get("relevance_score") or 0.5), 1.0)),
            0.55,
        )
        await self._log_graph_event("world_spawn", node_id=str(row["id"]), payload=requirements)
        return dict(row)

    async def ingest_world_signals(self, signals: list[dict], max_nodes: int = 25) -> dict:
        """Bulk-import live world signals into the IOO neural graph."""
        spawned_or_refreshed = []
        for signal in signals[:max_nodes]:
            try:
                node = await self.upsert_world_signal_node(signal)
                if node:
                    spawned_or_refreshed.append(node)
            except Exception as e:
                logger.warning(f"IOO world-signal ingest failed for {signal.get('title', '?')}: {e}")
        return {"ingested": len(spawned_or_refreshed), "nodes": spawned_or_refreshed}

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
                "domain": "iVive",
                "requires_fitness_level": 2, "requires_time_hours": 8.0,
                "requires_finances": 30.0,
            },
            {
                "type": "goal", "title": "Travel solo to a new country",
                "description": "Plan and complete a solo trip abroad.",
                "tags": ["travel", "adventure", "independence"],
                "domain": "Aventi",
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
                "domain": "iVive",
                "requires_fitness_level": 1, "requires_time_hours": 12.0,
            },
            {
                "type": "sub_goal", "title": "Get a valid passport",
                "description": "Apply for or renew your passport.",
                "tags": ["travel", "admin"],
                "domain": "Aventi",
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
                "domain": "iVive",
                "requires_fitness_level": 3, "requires_finances": 35.0,
                "requires_time_hours": 3.0,
            },
            {
                "type": "experience", "title": "Book a one-way flight",
                "description": "Commit to the trip by booking without a return ticket.",
                "tags": ["travel", "adventure", "commitment"],
                "domain": "Aventi",
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
                "domain": "iVive",
                "requires_fitness_level": 0, "requires_time_hours": 0.5,
            },
            {
                "type": "activity", "title": "Pack a backpack for one week",
                "description": "Practice minimalist packing: fit everything in carry-on.",
                "tags": ["travel", "preparation"],
                "domain": "Aventi",
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
            tags_l = set(node.get("tags", []))
            if "step_type" not in node:
                if tags_l & {"fitness", "running", "travel", "community", "social", "music"}:
                    node["step_type"] = "physical"
                elif tags_l & {"tech", "product", "research", "writing", "planning"}:
                    node["step_type"] = "digital"
                else:
                    node["step_type"] = "hybrid"
            if node.get("step_type") == "physical":
                node.setdefault("physical_context", "Requires real-world availability; may depend on local access.")
                node.setdefault("best_time", "flexible")

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
                     requires_location, requires_time_hours,
                     step_type, physical_context, best_time)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
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
                node.get("step_type", "hybrid"),
                node.get("physical_context"),
                node.get("best_time"),
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
