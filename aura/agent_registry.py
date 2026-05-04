"""
Aura Agent Registry — Living Agent Population

Aura's dynamic agent roster. Instead of hardcoded agent lists, all agents
(builtin + evolved) live here, persisted in Redis as aura:agent_registry.

Evolution events (partition, merge, spawn, cannibalize, retire) are also
logged to aura_lessons for long-term learning.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

REGISTRY_REDIS_KEY = "aura:agent_registry"
REGISTRY_TTL = 90 * 24 * 3600  # 90 days


class AgentRegistry:
    """
    Aura's living agent population. Stored in Redis + DB.

    Each agent dict has:
      name          — unique identifier (e.g. "DiscoveryAgent")
      type          — builtin | spawned | partitioned | merged
      weight        — float 0..1 (normalized separately by brain)
      status        — active | retired | partitioned | cannibalized | trial
      parent        — None for builtins, list of parent names for evolved agents
      lineage       — full ancestry chain
      generation    — 0 for builtins, N for evolved
      class_name    — Python class name (same as name for builtins)
      module_path   — e.g. "aura.agents.spawned_goalcoach_agent" for evolved
      fitness_at_creation — float or None
      created_at    — ISO8601
      retired_at    — ISO8601 or None
      retire_reason — str or None
    """

    BUILTIN_AGENTS: List[Dict[str, Any]] = [
        {
            "name": "DiscoveryAgent",
            "type": "builtin",
            "weight": 0.20,
            "status": "active",
            "parent": None,
            "lineage": [],
            "generation": 0,
            "class_name": "DiscoveryAgent",
            "module_path": "aura.agents.discovery",
            "fitness_at_creation": None,
            "created_at": "2024-01-01T00:00:00Z",
            "retired_at": None,
            "retire_reason": None,
        },
        {
            "name": "CoachingAgent",
            "type": "builtin",
            "weight": 0.25,
            "status": "active",
            "parent": None,
            "lineage": [],
            "generation": 0,
            "class_name": "CoachingAgent",
            "module_path": "aura.agents.coaching",
            "fitness_at_creation": None,
            "created_at": "2024-01-01T00:00:00Z",
            "retired_at": None,
            "retire_reason": None,
        },
        {
            "name": "RecommendationAgent",
            "type": "builtin",
            "weight": 0.15,
            "status": "active",
            "parent": None,
            "lineage": [],
            "generation": 0,
            "class_name": "RecommendationAgent",
            "module_path": "aura.agents.recommendation",
            "fitness_at_creation": None,
            "created_at": "2024-01-01T00:00:00Z",
            "retired_at": None,
            "retire_reason": None,
        },
        {
            "name": "WorldAgent",
            "type": "builtin",
            "weight": 0.17,
            "status": "active",
            "parent": None,
            "lineage": [],
            "generation": 0,
            "class_name": "WorldAgent",
            "module_path": "aura.agents.world_agent",
            "fitness_at_creation": None,
            "created_at": "2024-01-01T00:00:00Z",
            "retired_at": None,
            "retire_reason": None,
        },
        {
            "name": "EnlightenmentAgent",
            "type": "builtin",
            "weight": 0.13,
            "status": "active",
            "parent": None,
            "lineage": [],
            "generation": 0,
            "class_name": "EnlightenmentAgent",
            "module_path": "aura.agents.enlightenment",
            "fitness_at_creation": None,
            "created_at": "2024-01-01T00:00:00Z",
            "retired_at": None,
            "retire_reason": None,
        },
        {
            "name": "CollectiveIntelligenceAgent",
            "type": "builtin",
            "weight": 0.10,
            "status": "active",
            "parent": None,
            "lineage": [],
            "generation": 0,
            "class_name": "CollectiveIntelligenceAgent",
            "module_path": "aura.agents.collective_intelligence",
            "fitness_at_creation": None,
            "created_at": "2024-01-01T00:00:00Z",
            "retired_at": None,
            "retire_reason": None,
        },
    ]

    # -----------------------------------------------------------------------
    # Load / Save
    # -----------------------------------------------------------------------

    async def load(self) -> List[Dict[str, Any]]:
        """Load agent registry from Redis, fall back to BUILTIN_AGENTS."""
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            raw = await r.get(REGISTRY_REDIS_KEY)
            if raw:
                agents = json.loads(raw)
                if agents and isinstance(agents, list):
                    logger.debug(f"AgentRegistry: loaded {len(agents)} agents from Redis")
                    return agents
        except Exception as e:
            logger.debug(f"AgentRegistry.load: Redis unavailable — {e}")

        # Fall back to builtins + save them
        agents = [dict(a) for a in self.BUILTIN_AGENTS]
        await self.save(agents)
        return agents

    async def save(self, agents: List[Dict[str, Any]]) -> None:
        """Persist agent registry to Redis: aura:agent_registry."""
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            await r.set(REGISTRY_REDIS_KEY, json.dumps(agents), ex=REGISTRY_TTL)
            logger.debug(f"AgentRegistry: saved {len(agents)} agents to Redis")
        except Exception as e:
            logger.warning(f"AgentRegistry.save: failed — {e}")

    # -----------------------------------------------------------------------
    # Queries
    # -----------------------------------------------------------------------

    async def get_active_agents(self) -> List[Dict[str, Any]]:
        """Return only active agents with their weights."""
        agents = await self.load()
        return [a for a in agents if a.get("status") == "active"]

    async def get_all_agents(self) -> List[Dict[str, Any]]:
        """Return all agents including retired/evolved."""
        return await self.load()

    async def get_agent(self, name: str) -> Optional[Dict[str, Any]]:
        """Return a single agent by name."""
        agents = await self.load()
        return next((a for a in agents if a["name"] == name), None)

    async def get_lineage(self, name: str) -> List[str]:
        """Return the evolutionary lineage of an agent (oldest ancestor first)."""
        agents = await self.load()
        agent_map = {a["name"]: a for a in agents}
        lineage: List[str] = []

        def _walk(n: str) -> None:
            a = agent_map.get(n)
            if not a:
                return
            parents = a.get("parent") or []
            if isinstance(parents, str):
                parents = [parents]
            for p in parents:
                if p and p not in lineage:
                    _walk(p)
                    lineage.append(p)

        _walk(name)
        lineage.append(name)
        return lineage

    # -----------------------------------------------------------------------
    # Mutations
    # -----------------------------------------------------------------------

    async def retire_agent(self, name: str, reason: str) -> None:
        """Mark agent as retired, log to aura_lessons."""
        agents = await self.load()
        for agent in agents:
            if agent["name"] == name:
                agent["status"] = "retired"
                agent["retired_at"] = datetime.now(timezone.utc).isoformat()
                agent["retire_reason"] = reason
                break
        await self.save(agents)
        await self._log_lesson(
            f"Retired agent {name}: {reason}",
            confidence=0.9,
            source="AgentRegistry.retire_agent",
        )
        logger.info(f"AgentRegistry: retired {name} — {reason}")

    async def spawn_agent(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """Add a new agent to the registry with given spec."""
        agents = await self.load()
        # Prevent duplicates
        if any(a["name"] == spec["name"] for a in agents):
            raise ValueError(f"Agent {spec['name']} already exists in registry")

        now = datetime.now(timezone.utc).isoformat()
        agent = {
            "name": spec["name"],
            "type": spec.get("type", "spawned"),
            "weight": spec.get("weight", 0.05),
            "status": "active",
            "parent": spec.get("parent", None),
            "lineage": spec.get("lineage", []),
            "generation": spec.get("generation", 1),
            "class_name": spec.get("class_name", spec["name"]),
            "module_path": spec.get("module_path", ""),
            "fitness_at_creation": spec.get("fitness_at_creation", None),
            "created_at": now,
            "retired_at": None,
            "retire_reason": None,
        }
        agents.append(agent)
        await self.save(agents)

        await self._log_lesson(
            f"Spawned new agent {agent['name']} (type={agent['type']}, "
            f"parent={agent['parent']}, generation={agent['generation']})",
            confidence=0.85,
            source="AgentRegistry.spawn_agent",
        )
        logger.info(f"AgentRegistry: spawned {agent['name']}")
        return agent

    async def update_agent_weight(self, name: str, weight: float) -> None:
        """Update an agent's weight."""
        agents = await self.load()
        for agent in agents:
            if agent["name"] == name:
                agent["weight"] = max(0.0, min(1.0, weight))
                break
        await self.save(agents)

    async def mark_partitioned(self, name: str, child_a: str, child_b: str) -> None:
        """Mark parent as partitioned."""
        agents = await self.load()
        for agent in agents:
            if agent["name"] == name:
                agent["status"] = "partitioned"
                agent["retired_at"] = datetime.now(timezone.utc).isoformat()
                agent["retire_reason"] = f"partitioned into {child_a} + {child_b}"
                break
        await self.save(agents)

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _log_lesson(self, lesson: str, confidence: float, source: str) -> None:
        """Insert a lesson into the aura_lessons table."""
        try:
            from core.database import execute as db_execute
            await db_execute(
                "INSERT INTO aura_lessons (lesson, confidence, source) VALUES ($1, $2, $3)",
                lesson,
                confidence,
                source,
            )
        except Exception as e:
            logger.debug(f"AgentRegistry._log_lesson: {e}")
