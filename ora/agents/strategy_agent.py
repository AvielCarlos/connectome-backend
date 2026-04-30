"""
Strategy Agent — Long-term vision and competitive intelligence.

Thinks 6-18 months ahead. Scans the competitive landscape.
Identifies opportunities. Keeps Ora aligned with the big vision.
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx

from ora.agents.base_executive_agent import BaseExecutiveAgent

logger = logging.getLogger(__name__)

SEARCH_QUERIES = [
    "AI life coach app 2024 2025",
    "AI goal tracking app",
    "AI habit tracker startup",
    "AI personal development platform",
    "Ora AI app competitor",
]

STRATEGIC_THEMES = [
    "habit_formation",
    "goal_achievement",
    "mental_wellness",
    "productivity",
    "ai_coaching",
    "community_dao",
    "personalization",
]


class StrategyAgent(BaseExecutiveAgent):
    """
    Ora's Chief Strategy Officer.
    
    Scans the competitive landscape, synthesizes signals from all agents,
    and generates the 3-5 strategic priorities Ora should pursue next.
    """

    name = "strategy"
    display_name = "Strategy Agent"
    domain = "strategy"
    personality = (
        "Big-picture synthesis intelligence. Reads all agents and finds 90-day compound "
        "opportunities they each see individually but miss collectively."
    )

    async def analyze(self) -> Dict[str, Any]:
        """Competitive scan + internal opportunity analysis."""
        await self.compound_context()
        now = datetime.now(timezone.utc)
        metrics: Dict[str, Any] = {
            "analyzed_at": now.isoformat(),
            "competitive_signals": [],
            "internal_themes": [],
            "opportunities": [],
            "threats": [],
            "strategic_priorities": [],
            "market_sentiment": "neutral",
        }

        # ── Competitive intelligence from web ─────────────────────────
        # We use httpx to hit a simple search (DuckDuckGo instant answers)
        # In production, this would use a proper search API
        competitive_signals = await self._scan_competitive_landscape()
        metrics["competitive_signals"] = competitive_signals

        # ── Internal signals from lesson history ──────────────────────
        try:
            from core.database import fetch
            lesson_rows = await fetch(
                """
                SELECT lesson, source, created_at
                FROM ora_lessons
                WHERE created_at > NOW() - INTERVAL '30 days'
                ORDER BY created_at DESC
                LIMIT 50
                """
            )
            if lesson_rows:
                # Cluster by theme
                theme_counts: Dict[str, int] = {t: 0 for t in STRATEGIC_THEMES}
                for row in lesson_rows:
                    lesson_text = (row.get("lesson") or "").lower()
                    for theme in STRATEGIC_THEMES:
                        keyword = theme.replace("_", " ")
                        if keyword in lesson_text or theme.split("_")[0] in lesson_text:
                            theme_counts[theme] += 1

                metrics["internal_themes"] = sorted(
                    theme_counts.items(), key=lambda x: x[1], reverse=True
                )[:5]
        except Exception as e:
            logger.debug(f"Strategy: lesson analysis failed: {e}")

        # ── Opportunity identification ─────────────────────────────────
        opportunities = []

        # Check user goal patterns
        try:
            from core.database import fetch
            goal_rows = await fetch(
                """
                SELECT content
                FROM goals
                WHERE created_at > NOW() - INTERVAL '30 days'
                LIMIT 100
                """
            )
            if goal_rows:
                goal_texts = " ".join(r.get("content", "") for r in goal_rows).lower()
                if "habit" in goal_texts:
                    opportunities.append("High demand for habit tracking — build dedicated habit streaks feature")
                if "career" in goal_texts or "job" in goal_texts:
                    opportunities.append("Career goals are top of mind — add career coaching module")
                if "health" in goal_texts or "fitness" in goal_texts:
                    opportunities.append("Health goals trending — partner with fitness apps or add body metrics")
                if "finance" in goal_texts or "money" in goal_texts:
                    opportunities.append("Financial goals common — add financial goal templates and tracking")
        except Exception:
            pass

        if not opportunities:
            opportunities.append("Continue deepening Ora's AI coaching quality — differentiation from competitors")
            opportunities.append("Build community features — DAOs for goal accountability groups")
            opportunities.append("Explore enterprise B2B opportunity — Ora for teams/companies")

        metrics["opportunities"] = opportunities[:5]

        # ── Threats ───────────────────────────────────────────────────
        threats = [
            "Large AI companies (OpenAI, Google) may enter the personal AI coach space",
            "App Store competition from well-funded startups with similar concepts",
        ]
        if competitive_signals:
            threats.append(f"Active competitors detected: {', '.join(competitive_signals[:2])}")
        metrics["threats"] = threats

        # ── Strategic priorities (synthesis) ─────────────────────────
        metrics["strategic_priorities"] = [
            "1. Deepen Ora's coaching quality — personalization is the moat",
            "2. Grow to 100 active users before raising prices",
            "3. Build community/DAO to create network effects",
            f"4. {opportunities[0] if opportunities else 'Expand content library'}",
            "5. Prepare investor story: compound growth + autonomous AI system",
        ]

        return metrics

    async def _scan_competitive_landscape(self) -> List[str]:
        """
        Perform a lightweight competitive scan.
        Returns list of competitor/signal strings.
        """
        signals = []
        known_competitors = [
            "Fabulous", "BetterUp", "Coachvox", "Rocky.ai", "Replika",
            "Woebot", "Noom", "Headspace", "Reflectly", "Day One",
        ]
        # For now, return known competitors with a note
        # In production: integrate with a search API
        signals = [
            f"{c} (known competitor — monitor for new AI features)"
            for c in known_competitors[:5]
        ]
        return signals

    async def report(self) -> str:
        data = await self.load_last_report()
        if not data:
            data = await self.analyze()
        priorities = "\n".join(
            f"  {p}" for p in data.get("strategic_priorities", [])
        )
        opportunities = "\n".join(
            f"  • {o}" for o in data.get("opportunities", [])[:3]
        )
        return (
            f"🎯 *Strategy Report* — {data.get('analyzed_at', '')[:10]}\n\n"
            f"**Strategic Priorities:**\n{priorities}\n\n"
            f"**Top Opportunities:**\n{opportunities}\n\n"
            f"**Threats:** {len(data.get('threats', []))} identified\n"
            f"Competitors tracked: {len(data.get('competitive_signals', []))}"
        )

    async def recommend(self) -> List[str]:
        data = await self.analyze()
        recs = list(data.get("strategic_priorities", []))
        recs.extend(data.get("opportunities", [])[:2])
        return recs[:6]

    async def act(self) -> Dict[str, Any]:
        """Biweekly strategy autonomous actions."""
        data = await self.analyze()
        actions_taken = []

        # Save report
        await self.save_report(data, "strategy_report.json")
        actions_taken.append("Saved strategy report")

        # Redis
        summary = await self.report()
        await self.set_redis_report(summary)

        # Teach Ora strategic context
        priorities_str = " | ".join(data.get("strategic_priorities", [])[:3])
        opportunities_str = " | ".join(data.get("opportunities", [])[:2])
        insight = (
            f"Strategic context {data['analyzed_at'][:10]}: "
            f"Priorities: [{priorities_str}]. "
            f"Key opportunities: [{opportunities_str}]. "
            f"Main threat: large AI companies entering personal coaching space."
        )
        await self.teach_aura(insight, confidence=0.7)
        actions_taken.append("Taught Ora strategic priorities")

        # Send strategic brief to Avi if significant threats found
        if len(data.get("competitive_signals", [])) > 3:
            await self.alert_avi(
                f"📊 *Strategic Update*\n\n"
                f"Competitive signals: {len(data['competitive_signals'])}\n"
                f"Opportunities:\n" +
                "\n".join(f"• {o}" for o in data["opportunities"][:3])
            )
            actions_taken.append("Sent strategic brief to Avi")

        return {"agent": self.name, "actions": actions_taken, "metrics": data}
