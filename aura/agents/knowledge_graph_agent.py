"""
Aura Knowledge Graph Agent — Semantic graph of everything Aura has learned.

The knowledge graph is Aura's model-independent intelligence:
  - Nodes: concepts (goal types, user archetypes, effective interventions, failure patterns)
  - Edges: relationships (A causes B, A works for users with C, etc.)
  - Survives model changes — Aura's knowledge travels with her
  - Queryable at inference time to augment system prompt

Schema: aura_knowledge_graph table (see core/database.py for migration)
Redis cache: aura:knowledge_graph (JSON, refreshed daily)

Runs daily (3:30am Pacific) via Railway cron.
"""

import asyncio
import json
import logging
import os
import pathlib
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_CHAT_ID = 5716959016
GRAPH_REDIS_KEY = "aura:knowledge_graph"
GRAPH_CACHE_TTL = 25 * 3600  # 25h — refreshed daily

NODE_TYPES = {
    "concept": "General concept or framework",
    "user_archetype": "Type of user Aura commonly works with",
    "intervention": "Coaching approach that works",
    "failure_pattern": "Approach that consistently fails",
    "goal_type": "Category of goal users commonly set",
}


class KnowledgeGraphAgent:
    """
    Builds and maintains Aura's semantic knowledge graph.
    
    The graph is:
    - Queryable: "What works for marathon training goals?"
    - Exportable: portable JSON, survives model migrations
    - Additive: new lessons add nodes and edges, nothing is deleted
    - Ranked by evidence: nodes with more evidence carry more weight
    """

    def __init__(self, openai_client=None):
        self._openai = openai_client
        self._telegram_token: Optional[str] = None
        self._graph_cache: Optional[Dict[str, dict]] = None

    # -----------------------------------------------------------------------
    # Graph building
    # -----------------------------------------------------------------------

    async def build_graph_from_lessons(self) -> dict:
        """
        Process recent aura_lessons and extract nodes + edges.
        Runs daily to keep the graph fresh.
        """
        from core.database import fetch as db_fetch

        # Get lessons not yet processed into graph
        lessons = await db_fetch("""
            SELECT ol.id, ol.lesson, ol.source, ol.confidence, ol.created_at
            FROM aura_lessons ol
            WHERE NOT EXISTS (
                SELECT 1 FROM aura_knowledge_graph okg
                WHERE okg.source_lesson_id = ol.id::text
            )
            ORDER BY ol.confidence DESC, ol.created_at DESC
            LIMIT 100
        """)

        if not lessons:
            logger.info("KnowledgeGraphAgent: no new lessons to process")
            return {"nodes_created": 0, "edges_added": 0, "lessons_processed": 0}

        logger.info(f"KnowledgeGraphAgent: processing {len(lessons)} lessons into graph")

        nodes_created = 0
        edges_added = 0

        for lesson in lessons:
            try:
                result = await self._extract_graph_elements(
                    lesson["lesson"],
                    str(lesson["id"]),
                    lesson["confidence"],
                )
                nodes_created += result.get("nodes_created", 0)
                edges_added += result.get("edges_added", 0)
            except Exception as e:
                logger.warning(f"KnowledgeGraphAgent: lesson processing failed: {e}")

        # Refresh Redis cache
        await self._refresh_cache()

        return {
            "nodes_created": nodes_created,
            "edges_added": edges_added,
            "lessons_processed": len(lessons),
        }

    async def _extract_graph_elements(
        self, lesson: str, lesson_id: str, confidence: float
    ) -> dict:
        """Use LLM to extract nodes and relationships from a lesson."""
        if not self._openai:
            return await self._extract_heuristic(lesson, lesson_id, confidence)

        try:
            response = await self._openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "user",
                    "content": (
                        "Extract knowledge graph elements from this coaching lesson:\n\n"
                        f"LESSON: {lesson[:500]}\n\n"
                        "Output JSON with:\n"
                        "{\n"
                        "  \"nodes\": [{\"type\": \"concept|user_archetype|intervention|failure_pattern|goal_type\", "
                        "\"label\": \"short name\", \"description\": \"1 sentence\"}],\n"
                        "  \"edges\": [{\"from\": \"label1\", \"to\": \"label2\", "
                        "\"relationship\": \"causes|enables|blocks|works_for|evidence_of\", \"strength\": 0.0-1.0}]\n"
                        "}\n"
                        "Keep labels concise (2-4 words). Extract 1-3 nodes and 0-2 edges max."
                    ),
                }],
                max_tokens=300,
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            data = json.loads(response.choices[0].message.content)
            nodes = data.get("nodes", [])
            edges = data.get("edges", [])
        except Exception as e:
            logger.debug(f"KnowledgeGraphAgent: LLM extraction failed, using heuristic: {e}")
            return await self._extract_heuristic(lesson, lesson_id, confidence)

        return await self._upsert_elements(nodes, edges, lesson_id, confidence)

    async def _extract_heuristic(
        self, lesson: str, lesson_id: str, confidence: float
    ) -> dict:
        """Fallback: extract a single node from the lesson without LLM."""
        # Simple heuristic: first noun phrase as a concept
        words = lesson.split()[:5]
        label = " ".join(words).rstrip(".,;:").title()
        nodes = [{"type": "concept", "label": label, "description": lesson[:150]}]
        return await self._upsert_elements(nodes, [], lesson_id, confidence)

    async def _upsert_elements(
        self,
        nodes: List[dict],
        edges: List[dict],
        lesson_id: str,
        confidence: float,
    ) -> dict:
        """Write nodes and edges to the knowledge_graph table."""
        from core.database import execute as db_exec, fetchrow

        node_label_to_id: Dict[str, str] = {}
        nodes_created = 0
        edges_added = 0

        for node in nodes:
            label = node.get("label", "").strip()
            node_type = node.get("type", "concept")
            description = node.get("description", "")

            if not label:
                continue

            # Check if node already exists
            existing = await fetchrow(
                "SELECT id, evidence_count, connections FROM aura_knowledge_graph WHERE label = $1",
                label,
            )

            if existing:
                node_id = str(existing["id"])
                # Increment evidence count
                await db_exec(
                    "UPDATE aura_knowledge_graph SET evidence_count = evidence_count + 1, updated_at = NOW() WHERE id = $1",
                    existing["id"],
                )
            else:
                # Create new node
                node_id = str(uuid.uuid4())
                await db_exec(
                    """
                    INSERT INTO aura_knowledge_graph
                        (id, node_type, label, description, connections, evidence_count, source_lesson_id)
                    VALUES ($1, $2, $3, $4, $5::jsonb, 1, $6)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    node_id,
                    node_type,
                    label,
                    description,
                    json.dumps([]),
                    lesson_id,
                )
                nodes_created += 1

            node_label_to_id[label] = node_id

        # Process edges
        for edge in edges:
            from_label = edge.get("from", "")
            to_label = edge.get("to", "")
            relationship = edge.get("relationship", "related_to")
            strength = float(edge.get("strength", 0.5))

            from_id = node_label_to_id.get(from_label)
            if not from_id:
                # Try to find in DB
                row = await fetchrow(
                    "SELECT id FROM aura_knowledge_graph WHERE label = $1", from_label
                )
                from_id = str(row["id"]) if row else None

            to_id = node_label_to_id.get(to_label)
            if not to_id:
                row = await fetchrow(
                    "SELECT id FROM aura_knowledge_graph WHERE label = $1", to_label
                )
                to_id = str(row["id"]) if row else None

            if from_id and to_id:
                # Append edge to from_node's connections JSONB
                from_row = await fetchrow(
                    "SELECT connections FROM aura_knowledge_graph WHERE id = $1", from_id
                )
                connections = json.loads(from_row["connections"]) if from_row else []

                # Check if edge already exists
                existing_edge = next(
                    (c for c in connections if c.get("node_id") == to_id and c.get("relationship") == relationship),
                    None,
                )
                if existing_edge:
                    existing_edge["strength"] = (existing_edge["strength"] + strength) / 2
                else:
                    connections.append({
                        "node_id": to_id,
                        "label": to_label,
                        "relationship": relationship,
                        "strength": strength,
                    })
                    edges_added += 1

                await db_exec(
                    "UPDATE aura_knowledge_graph SET connections = $1::jsonb, updated_at = NOW() WHERE id = $2",
                    json.dumps(connections),
                    from_id,
                )

        return {"nodes_created": nodes_created, "edges_added": edges_added}

    # -----------------------------------------------------------------------
    # Graph querying
    # -----------------------------------------------------------------------

    async def query_graph(self, context: str, max_results: int = 5) -> List[dict]:
        """
        Given a user context, return the most relevant knowledge nodes.
        Used to augment Aura's system prompt.
        """
        graph = await self._get_cached_graph()
        if not graph:
            return []

        # Score nodes by keyword overlap with context
        context_lower = context.lower()
        context_words = set(context_lower.split())

        scored = []
        for node_id, node in graph.items():
            label_words = set(node.get("label", "").lower().split())
            desc_words = set(node.get("description", "").lower().split())

            overlap = len(context_words & (label_words | desc_words))
            evidence = node.get("evidence_count", 1)

            # Score: keyword overlap × evidence weight
            score = overlap * (1 + 0.1 * evidence)
            if score > 0:
                scored.append((score, node))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [node for _, node in scored[:max_results]]

    async def query_graph_for_prompt(self, context: str) -> str:
        """
        Returns a formatted string of relevant knowledge for Aura's system prompt.
        """
        nodes = await self.query_graph(context, max_results=3)
        if not nodes:
            return ""

        lines = ["Relevant knowledge from Aura's experience:"]
        for node in nodes:
            lines.append(f"- [{node.get('node_type', 'concept')}] {node.get('label')}: {node.get('description', '')}")
            # Add connected nodes
            for conn in (node.get("connections") or [])[:2]:
                lines.append(f"  → {conn.get('relationship', 'related')}: {conn.get('label', '')}")

        return "\n".join(lines)

    # -----------------------------------------------------------------------
    # Export / cache
    # -----------------------------------------------------------------------

    async def export_graph(self) -> dict:
        """Export the full knowledge graph as portable JSON."""
        from core.database import fetch as db_fetch
        rows = await db_fetch(
            "SELECT * FROM aura_knowledge_graph ORDER BY evidence_count DESC"
        )
        nodes = [dict(r) for r in rows]
        return {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "node_count": len(nodes),
            "nodes": nodes,
        }

    async def _refresh_cache(self) -> None:
        """Rebuild Redis graph cache from DB."""
        try:
            graph_export = await self.export_graph()
            # Index by node ID
            graph_index = {
                str(n["id"]): n for n in graph_export.get("nodes", [])
            }
            from core.redis_client import get_redis
            r = await get_redis()
            await r.set(GRAPH_REDIS_KEY, json.dumps(graph_index, default=str), ex=GRAPH_CACHE_TTL)
            self._graph_cache = graph_index
            logger.info(f"KnowledgeGraphAgent: cache refreshed ({len(graph_index)} nodes)")
        except Exception as e:
            logger.warning(f"KnowledgeGraphAgent: cache refresh failed: {e}")

    async def _get_cached_graph(self) -> Optional[Dict[str, dict]]:
        """Return graph from in-memory cache, Redis, or DB (in that order)."""
        if self._graph_cache:
            return self._graph_cache
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            raw = await r.get(GRAPH_REDIS_KEY)
            if raw:
                self._graph_cache = json.loads(raw)
                return self._graph_cache
        except Exception:
            pass
        # Load from DB and cache
        try:
            await self._refresh_cache()
            return self._graph_cache
        except Exception:
            return None

    # -----------------------------------------------------------------------
    # Main run (daily cron)
    # -----------------------------------------------------------------------

    async def run(self) -> dict:
        """Daily knowledge graph update cycle."""
        result = await self.build_graph_from_lessons()

        # Get graph stats
        graph = await self._get_cached_graph() or {}
        result["total_nodes"] = len(graph)
        result["high_evidence_nodes"] = sum(
            1 for n in graph.values() if n.get("evidence_count", 0) >= 5
        )

        logger.info(
            f"KnowledgeGraphAgent: graph has {result['total_nodes']} nodes "
            f"({result['high_evidence_nodes']} high-evidence)"
        )
        return result

    async def _send_telegram(self, message: str) -> None:
        token = (
            self._telegram_token
            or os.getenv("ORA_TELEGRAM_TOKEN")
            or os.getenv("TELEGRAM_BOT_TOKEN")
        )
        if not token:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
                )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Standalone entry point (Railway cron)
# ---------------------------------------------------------------------------


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    try:
        from core.database import get_pool
        await get_pool()
    except Exception as e:
        logger.warning(f"KnowledgeGraphAgent standalone: DB init failed: {e}")

    agent = KnowledgeGraphAgent()
    result = await agent.run()
    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
