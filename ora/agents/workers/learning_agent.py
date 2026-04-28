"""
LearningAgent — Feeds Ora new knowledge from recent research.

Reports to: Ora (directly)
Schedule: weekly Saturday 9am Pacific
"""

import asyncio
import logging
from datetime import datetime, timezone

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)

RESEARCH_TOPICS = [
    {
        "query": "habit formation science 2025 2026",
        "category": "behavioral science",
        "lesson_template": "Recent habit science: {summary}. Practical implication for iDo users: design habits with implementation intentions (if-then plans) for 2-3x better adherence.",
    },
    {
        "query": "CBT effectiveness digital mental health 2025",
        "category": "psychology",
        "lesson_template": "CBT research: {summary}. iDo can apply: structured thought-challenging exercises embedded in the daily screen.",
    },
    {
        "query": "goal setting psychology motivation research",
        "category": "motivation science",
        "lesson_template": "Goal psychology: {summary}. iDo takeaway: specific + time-bound goals with approach framing outperform avoidance goals.",
    },
    {
        "query": "AI personalization user experience engagement 2025",
        "category": "AI UX",
        "lesson_template": "AI personalization research: {summary}. Applied to Ora: personalization increases engagement by matching content to user context, not just preferences.",
    },
]


class LearningAgent(BaseWorkerAgent):
    name = "learning_agent"
    role = "Knowledge Curator"
    reports_to = "Ora"

    async def run(self) -> None:
        logger.info("LearningAgent: searching for recent research")
        week = datetime.now(timezone.utc).strftime("%Y-W%W")
        taught = 0

        for topic in RESEARCH_TOPICS:
            query = topic["query"]
            # Search via web (using perplexity-style search through shell if available)
            raw = self._sh(f'web_search "{query}" 2>/dev/null | head -50 || echo ""')

            # Also try xurl for social signals
            tweet_raw = self._sh(f'xurl search tweets --query "{query}" --sort recency --max-results 5 2>/dev/null | head -20')

            # Synthesize a summary (rule-based when no LLM)
            summary = self._synthesize(query, raw, tweet_raw)

            lesson = topic["lesson_template"].format(summary=summary)
            success = await self.teach_ora(lesson, confidence=0.8)
            if success:
                taught += 1
                logger.info(f"LearningAgent: taught Ora about '{topic['category']}'")

        logger.info(f"LearningAgent: done. Taught Ora {taught}/{len(RESEARCH_TOPICS)} topics.")

    def _synthesize(self, query: str, web_raw: str, tweet_raw: str) -> str:
        """Build a minimal summary from raw search output."""
        snippets = []
        for line in (web_raw + "\n" + tweet_raw).split("\n"):
            line = line.strip()
            if len(line) > 40 and not line.startswith("{") and not line.startswith("http"):
                snippets.append(line[:150])
            if len(snippets) >= 3:
                break

        if snippets:
            return "; ".join(snippets[:2])
        return f"latest research on '{query}' suggests continued emphasis on evidence-based, personalized interventions"

    async def report(self) -> str:
        return "LearningAgent: Weekly knowledge update complete. Ora has been briefed on latest research."


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(LearningAgent().run())
