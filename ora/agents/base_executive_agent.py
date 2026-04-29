"""
Base Executive Agent — Interface all C-suite agents implement.

Every agent in Ora's Executive Council inherits from this class.
They each own a domain, analyze it, report on it, and can act autonomously.
"""

import json
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_CHAT_ID = 5716959016
LOG_DIR = "/Users/avielcarlos/.openclaw/workspace/tmp/executive_council"
API_BASE = "https://connectome-api-production.up.railway.app"


AGENT_DOMAINS = {
    "cfo": "revenue",
    "cgo": "growth",
    "cmo": "growth",
    "cpo": "product",
    "cto": "tech",
    "coo": "ops",
    "community": "community",
    "strategy": "strategy",
    "executive_council": "strategy",
}

AGENT_COLLABORATORS = {
    "cfo": ["cgo", "cmo", "coo"],
    "cmo": ["cgo", "cpo", "community"],
    "cpo": ["cgo", "cto", "community"],
    "cto": ["cgo", "cpo", "coo"],
    "coo": ["cfo", "cgo", "cmo", "cpo", "cto", "community", "strategy"],
    "cgo": ["cfo", "cmo", "cpo", "cto", "community"],
    "strategy": ["cfo", "cgo", "cmo", "cpo", "cto", "coo", "community"],
    "community": ["cgo", "cmo", "cpo", "strategy"],
}


class BaseExecutiveAgent(ABC):
    """
    Interface all executive agents implement.
    
    Each agent:
    - Has a domain (finance, marketing, product, etc.)
    - Analyzes data in that domain
    - Reports insights in plain English
    - Can take safe autonomous actions
    - Reads and writes to the compound intelligence bus
    - Teaches Ora what it learns via /api/ora/learn
    """

    name: str = "base"
    display_name: str = "Base Agent"
    domain: str = "strategy"
    personality: str = "Mission-aligned executive intelligence serving Ora's AI OS for human flourishing."
    compound_system_prompt: str = (
        "Ora is an AI OS for human flourishing. Avi is The Spark — the visionary "
        "initiating force. The agent team is a living executive council that "
        "compounds collective intelligence: every agent reads what the others found, "
        "references cross-domain signals where relevant, and publishes structured "
        "insights back to the shared memory bus. Outputs should include "
        "agent_insights_published: list[str] and cross_agent_context_used: list[str]."
    )

    def __init__(self):
        self._jwt_token: Optional[str] = None
        self._telegram_token: Optional[str] = None
        self.domain = getattr(self, "domain", AGENT_DOMAINS.get(self.name, "strategy"))
        self._last_compound_context_used: List[str] = []
        self._last_agent_insights_published: List[str] = []
        os.makedirs(LOG_DIR, exist_ok=True)

    # ─── Interface ──────────────────────────────────────────────────────────

    @abstractmethod
    async def analyze(self) -> Dict[str, Any]:
        """Gather data and compute metrics for this domain."""
        ...

    @abstractmethod
    async def report(self) -> str:
        """Return a human-readable insight summary."""
        ...

    @abstractmethod
    async def recommend(self) -> List[str]:
        """Return a list of recommended actions."""
        ...

    @abstractmethod
    async def act(self) -> Dict[str, Any]:
        """Take safe autonomous actions based on analysis."""
        ...

    # ─── Shared helpers ─────────────────────────────────────────────────────

    async def compound_context(self, domains: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Pull relevant cross-agent insights from the compound memory bus.
        Gracefully returns [] if the bus/database is unavailable.
        """
        try:
            from ora.agents.agent_memory import agent_memory_bus

            insights = await agent_memory_bus.read_for(self.name, domains=domains)
            context = [insight.to_dict() for insight in insights]
            self._last_compound_context_used = [
                f"{item['source_agent']}:{item['insight_type']}:{item['content'][:120]}"
                for item in context
            ]
            return context
        except Exception as e:
            logger.debug(f"{self.name}: compound context unavailable: {e}")
            self._last_compound_context_used = []
            return []

    def compound_personality_prompt(self) -> str:
        """Richer prompt/context block for LLM-backed executive planning."""
        collaborators = ", ".join(AGENT_COLLABORATORS.get(self.name, [])) or "the full council"
        return (
            f"{self.compound_system_prompt}\n\n"
            f"You are {self.display_name} ({self.name}), domain={self.domain}. "
            f"Personality: {self.personality} "
            f"Primary collaborators to cross-reference: {collaborators}."
        )

    async def publish_agent_insights(self, data: Dict[str, Any]) -> List[str]:
        """Extract and publish this run's key findings to the shared bus."""
        try:
            from ora.agents.agent_memory import AgentInsight, agent_memory_bus

            insights = self._extract_insights_from_report(data)
            published: List[str] = []
            for insight in insights[:5]:
                insight_id = await agent_memory_bus.publish(insight)
                if insight_id:
                    published.append(insight.content)
            self._last_agent_insights_published = published
            return published
        except Exception as e:
            logger.debug(f"{self.name}: publish_agent_insights failed: {e}")
            self._last_agent_insights_published = []
            return []

    def _extract_insights_from_report(self, data: Dict[str, Any]) -> List[Any]:
        """Best-effort conversion of heterogeneous agent reports into bus insights."""
        from ora.agents.agent_memory import AgentInsight

        domain = getattr(self, "domain", AGENT_DOMAINS.get(self.name, "strategy"))
        collaborators = AGENT_COLLABORATORS.get(self.name, [])
        insights: List[AgentInsight] = []

        def add(insight_type: str, content: str, confidence: float = 0.78, action_required: bool = False, targets: Optional[List[str]] = None) -> None:
            content = " ".join(str(content).split())[:1000]
            if content:
                insights.append(
                    AgentInsight(
                        source_agent=self.name,
                        domain=domain,
                        insight_type=insight_type,
                        content=content,
                        confidence=confidence,
                        action_required=action_required,
                        target_agents=targets or collaborators[:3],
                    )
                )

        if not isinstance(data, dict):
            add("finding", str(data))
            return insights

        if data.get("strategic_synthesis"):
            add("decision", data["strategic_synthesis"], 0.9, True, AGENT_COLLABORATORS.get("strategy", []))
        if data.get("mandate"):
            add("finding", data["mandate"], 0.74)

        for key in ("top_priorities", "key_opportunities", "recommended_actions", "prioritized_action_plan"):
            value = data.get(key)
            if isinstance(value, list):
                for item in value[:2]:
                    text = item.get("action") if isinstance(item, dict) else item
                    add("opportunity" if "opportun" in key or "action" in key else "decision", text, 0.82, "action" in key, collaborators[:3])

        for key in ("key_risks", "risks"):
            value = data.get(key)
            if isinstance(value, list):
                for item in value[:2]:
                    add("risk", item, 0.84, True, collaborators[:3])

        metric_fragments = []
        metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else data
        for key in (
            "mrr_usd", "arr_usd", "revenue_last_30d_usd", "total_users", "new_users_7d",
            "active_users_7d", "weekly_growth_rate_pct", "paid_users", "conversion_rate_pct",
            "system_health_score", "community_health_score", "roadmap_health_score",
        ):
            if key in metrics:
                metric_fragments.append(f"{key}={metrics.get(key)}")
        if metric_fragments:
            add("finding", f"{self.display_name} metrics: " + ", ".join(metric_fragments), 0.8)

        if not insights:
            summary = json.dumps(data, default=str)[:700]
            add("finding", f"{self.display_name} completed run: {summary}", 0.7)
        return insights

    async def teach_ora(self, insight: str, confidence: float = 0.8) -> bool:
        """
        POST an insight to /api/ora/learn so Ora compounds in intelligence.
        Uses JWT auth. Returns True on success.
        """
        try:
            token = await self._get_jwt()
            if not token:
                logger.warning(f"{self.name}: no JWT, cannot teach Ora")
                return False

            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{API_BASE}/api/ora/learn",
                    json={
                        "lesson": insight,
                        "confidence": confidence,
                        "source": f"executive_council.{self.name}",
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code in (200, 201):
                    logger.info(f"{self.name}: taught Ora — {insight[:80]}")
                    return True
                else:
                    logger.warning(f"{self.name}: teach_ora failed {resp.status_code}: {resp.text[:200]}")
                    return False
        except Exception as e:
            logger.error(f"{self.name}: teach_ora error: {e}")
            return False

    async def save_report(self, data: Dict[str, Any], filename: Optional[str] = None) -> str:
        """Save a report dict and publish its key findings to the intelligence bus."""
        fname = filename or f"{self.name}_report.json"
        path = os.path.join(LOG_DIR, fname)
        data["_saved_at"] = datetime.now(timezone.utc).isoformat()
        if "cross_agent_context_used" not in data:
            data["cross_agent_context_used"] = self._last_compound_context_used
        if "agent_insights_published" not in data:
            data["agent_insights_published"] = await self.publish_agent_insights(data)
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        return path

    async def load_last_report(self, filename: Optional[str] = None) -> Optional[Dict]:
        """Load the most recent saved report for this agent."""
        fname = filename or f"{self.name}_report.json"
        path = os.path.join(LOG_DIR, fname)
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return None

    async def set_redis_report(self, summary: str) -> None:
        """Store latest summary in Redis so other agents can read it."""
        try:
            from core.redis_client import get_redis
            redis = await get_redis()
            key = f"ora:executive:last_report:{self.name}"
            await redis.setex(key, 604800, summary)  # 7 day TTL
        except Exception as e:
            logger.debug(f"{self.name}: Redis set failed: {e}")

    async def get_redis_report(self, agent_name: str) -> Optional[str]:
        """Get another agent's latest report from Redis."""
        try:
            from core.redis_client import get_redis
            redis = await get_redis()
            return await redis.get(f"ora:executive:last_report:{agent_name}")
        except Exception as e:
            logger.debug(f"{self.name}: Redis get failed: {e}")
            return None

    async def alert_avi(self, message: str) -> None:
        """Send an urgent Telegram message to Avi."""
        await self._send_telegram(f"🚨 *{self.display_name}*\n\n{message}")

    async def _send_telegram(self, message: str, chat_id: int = TELEGRAM_CHAT_ID) -> None:
        """Send a Telegram message."""
        token = await self._get_telegram_token()
        if not token:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
                )
                if resp.status_code != 200:
                    logger.warning(f"{self.name}: Telegram {resp.status_code}")
        except Exception as e:
            logger.warning(f"{self.name}: Telegram send failed: {e}")

    async def _get_jwt(self) -> Optional[str]:
        """Get JWT by logging in with test credentials."""
        if self._jwt_token:
            return self._jwt_token
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{API_BASE}/api/users/login",
                    json={"email": "test@test.com", "password": "test1234"},
                )
                if resp.status_code == 200:
                    self._jwt_token = resp.json().get("token") or resp.json().get("access_token")
                    return self._jwt_token
        except Exception as e:
            logger.error(f"{self.name}: JWT login failed: {e}")
        return None

    async def _api_get(self, path: str) -> Optional[Dict]:
        """Authenticated GET against the Connectome API."""
        try:
            token = await self._get_jwt()
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(f"{API_BASE}{path}", headers=headers)
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            logger.debug(f"{self.name}: GET {path} failed: {e}")
        return None

    async def _get_telegram_token(self) -> Optional[str]:
        if self._telegram_token:
            return self._telegram_token
        token = os.environ.get("ORA_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            try:
                with open("/Users/avielcarlos/.openclaw/secrets/telegram-bot-token.txt") as f:
                    token = f.read().strip()
            except Exception:
                pass
        if token:
            self._telegram_token = token
        return token
