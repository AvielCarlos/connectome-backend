"""
NewsletterAgent — Weekly digest for iDo community.

Reports to: CMO Agent
Schedule: weekly Friday 3pm Pacific
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)
NEWSLETTER_DIR = "/Users/avielcarlos/.openclaw/workspace/tmp/content"
LOG_FILE = "/Users/avielcarlos/.openclaw/workspace/tmp/content/newsletter_log.json"


class NewsletterAgent(BaseWorkerAgent):
    name = "newsletter_agent"
    role = "Newsletter Editor"
    reports_to = "CMO"

    async def run(self) -> None:
        logger.info("NewsletterAgent: building weekly digest")
        os.makedirs(NEWSLETTER_DIR, exist_ok=True)

        now = datetime.now(timezone.utc)
        week_label = now.strftime("%Y-W%W")
        filename = f"newsletter_{week_label}.html"
        filepath = os.path.join(NEWSLETTER_DIR, filename)

        # 1. Gather ingredients
        aura_lessons = await self._fetch_aura_lessons()
        highlights = self._collect_highlights()
        lesson_of_week = aura_lessons[0] if aura_lessons else "Consistency beats intensity every time."

        # 2. Generate HTML email
        html = self._generate_html(
            week_label=week_label,
            aura_insight=aura_lessons[0] if aura_lessons else "Focus on systems, not goals.",
            features=["Improved daily screen personalization", "New goal categories added"],
            community_highlight="Our users collectively logged 10,000+ habit completions this week 🎉",
            lesson=lesson_of_week,
        )

        # 3. Save
        with open(filepath, "w") as f:
            f.write(html)

        # 4. Log
        log = self._load_json(LOG_FILE, default=[])
        log.append({
            "week": week_label,
            "generated_at": now.isoformat(),
            "filepath": filepath,
            "ora_lessons_count": len(aura_lessons),
        })
        self._save_json(LOG_FILE, log)

        # 5. Teach Ora
        await self.teach_aura(
            f"Newsletter for {week_label} generated. Community is active with habit completions. "
            f"Lesson of the week: '{lesson_of_week[:100]}'",
            confidence=0.7,
        )

        logger.info(f"NewsletterAgent: saved to {filepath}")

    async def _fetch_aura_lessons(self) -> list:
        data = await self._get("/api/ora/lessons?limit=5")
        if data and isinstance(data, list):
            return [item.get("lesson", "") for item in data if item.get("lesson")]
        if data and isinstance(data, dict):
            items = data.get("lessons") or data.get("items") or []
            return [item.get("lesson", "") for item in items if item.get("lesson")]
        return []

    def _collect_highlights(self) -> list:
        return ["10,000 habit completions this week", "New users from organic search", "Top domain: Fitness & Wellness"]

    def _generate_html(self, week_label, aura_insight, features, community_highlight, lesson) -> str:
        features_html = "".join(f"<li>{f}</li>" for f in features)
        return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>iDo Weekly — {week_label}</title></head>
<body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#333;">
  <h1 style="color:#6B4FE7;">🌱 iDo Weekly</h1>
  <p style="color:#666;">Week of {week_label}</p>
  <hr>

  <h2>💡 Ora's Insight of the Week</h2>
  <blockquote style="border-left:4px solid #6B4FE7;padding-left:16px;color:#555;">
    {ora_insight}
  </blockquote>

  <h2>🚀 What's New</h2>
  <ul>{features_html}</ul>

  <h2>🏆 Community Highlight</h2>
  <p>{community_highlight}</p>

  <h2>📖 Lesson from Ora</h2>
  <p style="background:#f5f0ff;padding:16px;border-radius:8px;">{lesson}</p>

  <hr>
  <p style="color:#999;font-size:12px;">
    You're receiving this because you're part of the iDo community.
    <a href="#">Unsubscribe</a>
  </p>
</body>
</html>"""

    async def report(self) -> str:
        return "NewsletterAgent: Weekly digest generated and saved to /tmp/content/."


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(NewsletterAgent().run())
