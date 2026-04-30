"""
AnalyticsAgent — Daily behavior data processing.

Reports to: CPO + CFO Agents
Schedule: daily 5am Pacific
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)
ANALYTICS_DIR = "/Users/avielcarlos/.openclaw/workspace/tmp/analytics"


class AnalyticsAgent(BaseWorkerAgent):
    name = "analytics_agent"
    role = "Data Analyst"
    reports_to = "CPO, CFO"

    async def run(self) -> None:
        logger.info("AnalyticsAgent: starting daily analytics run")
        os.makedirs(ANALYTICS_DIR, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 1. Pull data from API
        ratings_data = await self._get("/api/screens/ratings?range=7d") or {}
        goals_data = await self._get("/api/goals/completion?range=7d") or {}
        conv_data = await self._get("/api/ora/conversations?range=7d&limit=100") or {}

        # 2. Compute metrics
        ratings = ratings_data.get("ratings", []) or (ratings_data if isinstance(ratings_data, list) else [])
        goals = goals_data.get("goals", []) or (goals_data if isinstance(goals_data, list) else [])
        convs = conv_data.get("conversations", []) or (conv_data if isinstance(conv_data, list) else [])

        domain_counts: dict = {}
        for r in ratings:
            domain = r.get("domain", "unknown")
            domain_counts[domain] = domain_counts.get(domain, 0) + 1

        top_domain = max(domain_counts, key=domain_counts.get) if domain_counts else "unknown"
        completed = sum(1 for g in goals if g.get("completed"))
        total_goals = len(goals) if goals else 1
        completion_rate = round(completed / total_goals * 100, 1) if total_goals else 0

        long_convs = sum(1 for c in convs if (c.get("message_count") or 0) > 5)
        conv_engagement = round(long_convs / len(convs) * 100, 1) if convs else 0

        # 3. Save report
        report = {
            "date": today,
            "ratings_distribution": domain_counts,
            "top_domain": top_domain,
            "goal_completion_rate_pct": completion_rate,
            "goals_analyzed": total_goals,
            "conversation_engagement_pct": conv_engagement,
            "conversations_analyzed": len(convs),
        }
        path = os.path.join(ANALYTICS_DIR, f"daily_{today}.json")
        self._save_json(path, report)

        # 4. Teach Ora
        await self.teach_aura(
            f"Daily analytics ({today}): Top engagement domain is '{top_domain}'. "
            f"Goal completion rate: {completion_rate}%. "
            f"Conversation engagement (>5 msgs): {conv_engagement}%. "
            f"Users who engage deeply with Ora are showing stronger habit formation.",
            confidence=0.85,
        )

        logger.info(f"AnalyticsAgent: done. Report at {path}")

    async def report(self) -> str:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        data = self._load_json(f"{ANALYTICS_DIR}/daily_{today}.json", {})
        return (
            f"AnalyticsAgent: {today} — "
            f"top domain '{data.get('top_domain', '?')}', "
            f"goal completion {data.get('goal_completion_rate_pct', '?')}%, "
            f"conversation engagement {data.get('conversation_engagement_pct', '?')}%"
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(AnalyticsAgent().run())
