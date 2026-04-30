"""
TrendSpotter — Keeps Ora culturally current with daily Twitter trends.

Reports to: Ora (directly)
Schedule: daily 6am Pacific
"""

import asyncio
import logging
from datetime import datetime, timezone

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)

TOPICS = [
    ("AI tools", "AI & productivity apps"),
    ("self improvement", "personal growth"),
    ("productivity", "habit & focus"),
    ("mental health tips", "mental wellness"),
    ("habit building", "behavior change"),
]


class TrendSpotter(BaseWorkerAgent):
    name = "trend_spotter"
    role = "Trend Intelligence"
    reports_to = "Ora"

    async def run(self) -> None:
        logger.info("TrendSpotter: scanning Twitter trends")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        insights = []

        for search_term, category in TOPICS:
            out = self._sh(
                f'xurl search tweets --query "{search_term}" --sort popularity --max-results 10 2>/dev/null | head -30'
            )
            if not out or "error" in out.lower():
                continue

            # Extract top tweet snippet
            snippet = self._extract_top_quote(out)
            if snippet:
                insights.append({
                    "topic": search_term,
                    "category": category,
                    "snippet": snippet,
                })

        if not insights:
            logger.info("TrendSpotter: no trend data retrieved")
            return

        # Teach Ora about each trend
        for insight in insights[:3]:  # top 3 trends
            topic = insight["topic"]
            snippet = insight["snippet"][:200]
            category = insight["category"]

            relevance = self._compute_relevance(topic)

            await self.teach_aura(
                f"Trending today ({today}): '{topic}' is active in {category}. "
                f"People are saying: \"{snippet}\". "
                f"Relevant to iDo because: {relevance}. "
                f"Consider weaving this into today's content or Ora's coaching conversations.",
                confidence=0.75,
            )

        logger.info(f"TrendSpotter: taught Ora {len(insights[:3])} trends.")

    def _extract_top_quote(self, raw: str) -> str:
        """Extract a meaningful snippet from xurl output."""
        lines = [l.strip() for l in raw.split("\n") if l.strip() and len(l.strip()) > 30]
        # Filter out JSON-looking lines and pick the most natural sentence
        for line in lines:
            if not line.startswith("{") and not line.startswith("[") and not "http" in line:
                return line[:180]
        return lines[0][:180] if lines else ""

    def _compute_relevance(self, topic: str) -> str:
        relevance_map = {
            "AI tools": "our users are early adopters who use AI for self-improvement; they'll relate",
            "self improvement": "core iDo audience — they're already searching for what we offer",
            "productivity": "habit building is the backbone of productivity; direct overlap",
            "mental health": "emotional wellbeing is a core goal category in iDo",
            "habit building": "this IS our core product — perfect content alignment",
        }
        for key, val in relevance_map.items():
            if key in topic.lower():
                return val
        return "adjacent to our audience's interests and goals"

    async def report(self) -> str:
        return "TrendSpotter: Daily trend scan complete. Ora is culturally up to date."


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(TrendSpotter().run())
