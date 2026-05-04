"""
Compound Intelligence Layer — shared memory bus for Aura's agent council.

Every executive agent can publish structured insights after a run. Other agents
can read relevant cross-domain context before planning. Insights expire after
7 days by default unless promoted to permanent learnings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from core.database import execute, fetch, fetchrow

logger = logging.getLogger(__name__)

DEFAULT_TTL_DAYS = 7


@dataclass
class AgentInsight:
    source_agent: str
    domain: str
    insight_type: str
    content: str
    confidence: float = 0.8
    action_required: bool = False
    target_agents: List[str] = field(default_factory=list)
    expires_at: Optional[datetime] = None
    id: Optional[str] = None
    created_at: Optional[datetime] = None
    promoted_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.expires_at is None:
            self.expires_at = datetime.now(timezone.utc) + timedelta(days=DEFAULT_TTL_DAYS)
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        self.target_agents = [a.strip().lower() for a in (self.target_agents or []) if a]
        self.source_agent = self.source_agent.strip().lower()
        self.domain = self.domain.strip().lower()
        self.insight_type = self.insight_type.strip().lower()

    @classmethod
    def from_record(cls, row: Any) -> "AgentInsight":
        return cls(
            id=str(row["id"]),
            source_agent=row["source_agent"],
            domain=row["domain"],
            insight_type=row["insight_type"],
            content=row["content"],
            confidence=float(row["confidence"]),
            action_required=bool(row["action_required"]),
            target_agents=list(row["target_agents"] or []),
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            promoted_at=row["promoted_at"],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source_agent": self.source_agent,
            "domain": self.domain,
            "insight_type": self.insight_type,
            "content": self.content,
            "confidence": self.confidence,
            "action_required": self.action_required,
            "target_agents": self.target_agents,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "promoted_at": self.promoted_at.isoformat() if self.promoted_at else None,
        }


class AgentMemoryBus:
    """PostgreSQL-backed shared knowledge bus for executive agents."""

    def __init__(self) -> None:
        self._ready = False

    async def ensure_schema(self) -> None:
        if self._ready:
            return
        await execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
        await execute(
            """
            CREATE TABLE IF NOT EXISTS agent_insights (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                source_agent TEXT NOT NULL,
                domain TEXT NOT NULL,
                insight_type TEXT NOT NULL,
                content TEXT NOT NULL,
                confidence DOUBLE PRECISION DEFAULT 0.8,
                action_required BOOLEAN DEFAULT FALSE,
                target_agents TEXT[] DEFAULT ARRAY[]::TEXT[],
                expires_at TIMESTAMPTZ NOT NULL,
                promoted_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )
        await execute(
            """
            CREATE TABLE IF NOT EXISTS agent_trigger_queue (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                insight_id UUID REFERENCES agent_insights(id) ON DELETE CASCADE,
                source_agent TEXT NOT NULL,
                target_agent TEXT NOT NULL,
                reason TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                consumed_at TIMESTAMPTZ
            )
            """
        )
        await execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_insights_recent ON agent_insights (created_at DESC)"
        )
        await execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_insights_targets ON agent_insights USING GIN (target_agents)"
        )
        await execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_trigger_queue_pending ON agent_trigger_queue (status, target_agent, created_at DESC)"
        )
        self._ready = True

    async def publish(self, insight: AgentInsight) -> Optional[str]:
        """Publish an insight and enqueue any required target-agent follow-ups."""
        try:
            await self.ensure_schema()
            row = await fetchrow(
                """
                INSERT INTO agent_insights (
                    source_agent, domain, insight_type, content, confidence,
                    action_required, target_agents, expires_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7::TEXT[], $8)
                RETURNING id, created_at
                """,
                insight.source_agent,
                insight.domain,
                insight.insight_type,
                insight.content,
                insight.confidence,
                insight.action_required,
                insight.target_agents,
                insight.expires_at,
            )
            if not row:
                return None
            insight.id = str(row["id"])
            insight.created_at = row["created_at"]

            if insight.action_required:
                for agent in insight.target_agents:
                    await execute(
                        """
                        INSERT INTO agent_trigger_queue (insight_id, source_agent, target_agent, reason)
                        VALUES ($1, $2, $3, $4)
                        """,
                        UUID(insight.id),
                        insight.source_agent,
                        agent,
                        insight.content[:500],
                    )
            return insight.id
        except Exception as exc:
            logger.warning("AgentMemoryBus.publish failed: %s", exc)
            return None

    async def read_for(self, agent_name: str, domains: Optional[List[str]] = None) -> List[AgentInsight]:
        """Read active insights relevant to an agent and optional domains."""
        try:
            await self.ensure_schema()
            agent = agent_name.lower()
            domain_filter = [d.lower() for d in domains] if domains else None
            if domain_filter:
                rows = await fetch(
                    """
                    SELECT * FROM agent_insights
                    WHERE (expires_at > NOW() OR promoted_at IS NOT NULL)
                      AND source_agent != $1
                      AND domain = ANY($2::TEXT[])
                      AND (target_agents = ARRAY[]::TEXT[] OR $1 = ANY(target_agents))
                    ORDER BY action_required DESC, confidence DESC, created_at DESC
                    LIMIT 25
                    """,
                    agent,
                    domain_filter,
                )
            else:
                rows = await fetch(
                    """
                    SELECT * FROM agent_insights
                    WHERE (expires_at > NOW() OR promoted_at IS NOT NULL)
                      AND source_agent != $1
                      AND (target_agents = ARRAY[]::TEXT[] OR $1 = ANY(target_agents))
                    ORDER BY action_required DESC, confidence DESC, created_at DESC
                    LIMIT 25
                    """,
                    agent,
                )
            return [AgentInsight.from_record(row) for row in rows]
        except Exception as exc:
            logger.debug("AgentMemoryBus.read_for failed: %s", exc)
            return []

    async def read_all_recent(self, hours: int = 168) -> List[AgentInsight]:
        """Read all non-expired insights created within the requested window."""
        try:
            await self.ensure_schema()
            rows = await fetch(
                """
                SELECT * FROM agent_insights
                WHERE created_at > NOW() - ($1::INT * INTERVAL '1 hour')
                  AND (expires_at > NOW() OR promoted_at IS NOT NULL)
                ORDER BY created_at DESC
                LIMIT 250
                """,
                int(hours),
            )
            return [AgentInsight.from_record(row) for row in rows]
        except Exception as exc:
            logger.debug("AgentMemoryBus.read_all_recent failed: %s", exc)
            return []

    async def promote_to_permanent(self, insight_id: str) -> bool:
        try:
            await self.ensure_schema()
            await execute(
                "UPDATE agent_insights SET promoted_at = NOW(), expires_at = NOW() + INTERVAL '100 years' WHERE id = $1",
                UUID(insight_id),
            )
            return True
        except Exception as exc:
            logger.warning("AgentMemoryBus.promote_to_permanent failed: %s", exc)
            return False

    async def read_trigger_queue(self, status: str = "pending") -> List[Dict[str, Any]]:
        """Read pending agent follow-up triggers for council fast-tracking."""
        try:
            await self.ensure_schema()
            rows = await fetch(
                """
                SELECT q.*, i.domain, i.insight_type, i.confidence
                FROM agent_trigger_queue q
                LEFT JOIN agent_insights i ON i.id = q.insight_id
                WHERE q.status = $1
                ORDER BY q.created_at ASC
                LIMIT 100
                """,
                status,
            )
            return [dict(row) for row in rows]
        except Exception as exc:
            logger.debug("AgentMemoryBus.read_trigger_queue failed: %s", exc)
            return []

    async def mark_trigger_consumed(self, trigger_id: str) -> bool:
        try:
            await self.ensure_schema()
            await execute(
                "UPDATE agent_trigger_queue SET status = 'consumed', consumed_at = NOW() WHERE id = $1",
                UUID(trigger_id),
            )
            return True
        except Exception as exc:
            logger.debug("AgentMemoryBus.mark_trigger_consumed failed: %s", exc)
            return False


agent_memory_bus = AgentMemoryBus()
