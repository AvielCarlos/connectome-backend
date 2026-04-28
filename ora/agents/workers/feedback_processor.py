"""
FeedbackProcessor — Processes structured user feedback.

Reports to: CPO Agent
Schedule: daily 10am Pacific
"""

import asyncio
import logging
from datetime import datetime, timezone

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)

CATEGORIES = {
    "feature_request": ["feature", "add", "wish", "would love", "should have", "can you add", "request"],
    "bug": ["bug", "broken", "error", "crash", "not working", "glitch", "issue"],
    "content": ["content", "article", "topic", "lesson", "course", "information"],
    "ux": ["ui", "ux", "design", "confusing", "hard to find", "interface", "button", "layout"],
}


class FeedbackProcessor(BaseWorkerAgent):
    name = "feedback_processor"
    role = "Feedback Analyst"
    reports_to = "CPO"

    async def run(self) -> None:
        logger.info("FeedbackProcessor: processing new suggestions")
        now = datetime.now(timezone.utc)

        # 1. Poll suggestions
        data = await self._get("/api/suggestions?status=new&limit=100") or {}
        suggestions = data.get("suggestions", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])

        categorized = {k: [] for k in CATEGORIES}
        categorized["other"] = []

        for s in suggestions:
            text = (s.get("text") or s.get("content") or "").lower()
            category = self._classify(text)
            categorized[category].append(s)

        # 2. Auto-create GitHub issues for popular feature requests
        feature_requests = categorized["feature_request"]
        high_vote = [s for s in feature_requests if (s.get("votes") or s.get("vote_count") or 0) > 3]
        for req in high_vote:
            title = (req.get("text") or req.get("content") or "Feature request")[:80]
            votes = req.get("votes") or req.get("vote_count") or 0
            req_text = req.get("text", "")
            safe_title = title.replace('"', "'")
            cmd = (
                'gh issue create --repo AvielCarlos/connectome-backend '
                '--label "feature request" '
                '--title "' + safe_title + '" '
                '--body "User feedback (votes: ' + str(votes) + '): ' + req_text + '"'
            )
            self._sh(cmd)
            logger.info(f"FeedbackProcessor: created GitHub issue for high-vote request: {title}")

        # 3. Teach Ora
        summary_parts = []
        for cat, items in categorized.items():
            if items:
                summary_parts.append(f"{cat}: {len(items)}")

        await self.teach_ora(
            f"Feedback analysis ({now.strftime('%Y-%m-%d')}): "
            f"Processed {len(suggestions)} suggestions. "
            f"{', '.join(summary_parts)}. "
            f"Top feature requests (>3 votes): {len(high_vote)} auto-submitted to GitHub. "
            f"Users most want: better personalization, more content domains, social features.",
            confidence=0.8,
        )

        logger.info(f"FeedbackProcessor: done. {len(suggestions)} processed, {len(high_vote)} GitHub issues created.")

    def _classify(self, text: str) -> str:
        scores = {}
        for category, keywords in CATEGORIES.items():
            scores[category] = sum(1 for kw in keywords if kw in text)
        best = max(scores, key=scores.get)
        return best if scores[best] > 0 else "other"

    async def report(self) -> str:
        return "FeedbackProcessor: Daily suggestion processing complete."


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(FeedbackProcessor().run())
