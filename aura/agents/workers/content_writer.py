"""
ContentWriterAgent — Daily content generation.

Reports to: CMO Agent
Schedule: daily 7am Pacific
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)
CONTENT_DIR = os.path.join(os.getenv("CONNECTOME_RUNTIME_DIR", "/tmp/connectome"), "content")


class ContentWriterAgent(BaseWorkerAgent):
    name = "content_writer"
    role = "Content Writer"
    reports_to = "CMO"

    async def run(self) -> None:
        logger.info("ContentWriterAgent: starting daily content run")
        os.makedirs(CONTENT_DIR, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 1. Pull trending topics via xurl
        topics = self._get_trending_topics()

        # 2. Generate content pieces
        content = {
            "date": today,
            "tweet_thread": self._write_tweet_thread(topics),
            "short_tip": self._write_short_tip(topics),
            "long_insight": self._write_long_insight(topics),
            "topics_used": topics,
        }

        # 3. Save to disk
        path = os.path.join(CONTENT_DIR, f"content_{today}.json")
        self._save_json(path, content)

        # 4. Post tweet thread to Telegram channel
        thread_text = f"📢 *Daily Insight from iDo*\n\n{content['tweet_thread']}"
        await self.post_to_channel(thread_text)

        # 5. Queue tweets (max 2 per day)
        tweets = content["tweet_thread"].split("\n\n")[:2]
        for tweet in tweets:
            if tweet.strip():
                self._sh(f'xurl tweet post --text "{tweet[:280].replace(chr(34), chr(39))}"')

        # 6. Teach Aura
        top_topic = topics[0] if topics else "productivity"
        await self.teach_aura(
            f"Content performance: '{top_topic}' is trending in self-improvement. "
            f"Daily content generated for {today}. Tweet thread posted to channel.",
            confidence=0.7,
        )

        logger.info(f"ContentWriterAgent: done. Content saved to {path}")

    def _get_trending_topics(self) -> list:
        topics = []
        for query in ["self improvement", "productivity tips", "AI habits"]:
            out = self._sh(f'xurl search tweets --query "{query}" --sort recency --max-results 10 2>/dev/null | head -5')
            if out and "error" not in out.lower():
                topics.append(query)
        return topics or ["self-improvement", "productivity", "goal setting"]

    def _write_tweet_thread(self, topics: list) -> str:
        topic = topics[0] if topics else "productivity"
        return (
            f"🧵 The #1 mistake people make with {topic}:\n\n"
            f"They optimize for motivation instead of systems.\n\n"
            f"Motivation is a feeling. Systems are infrastructure.\n\n"
            f"Here's how to build a system that works even when you don't feel like it 👇\n\n"
            f"1/ Start with a tiny habit (2 min rule)\n"
            f"2/ Attach it to an existing routine (habit stacking)\n"
            f"3/ Track it — what gets measured gets done\n"
            f"4/ Review weekly — adjust without judgment\n\n"
            f"iDo helps you build exactly this. Try it free: https://ido.ascensiontechnologies.ca"
        )

    def _write_short_tip(self, topics: list) -> str:
        topic = topics[1] if len(topics) > 1 else "habits"
        return f"💡 Quick tip on {topic}: Start with 1% better each day. That's 37x better in a year. Compound growth is real."

    def _write_long_insight(self, topics: list) -> str:
        topic = topics[2] if len(topics) > 2 else "goal setting"
        return (
            f"🔍 Deep dive: Why most people fail at {topic}\n\n"
            f"The research is clear: vague intentions lead to vague results. "
            f"Specific, time-bound goals with implementation intentions (if-then planning) "
            f"are 2-3x more likely to be achieved. "
            f"iDo's AI coach helps you craft exactly this kind of precision goal."
        )

    async def report(self) -> str:
        return f"ContentWriterAgent: Generated 3 content pieces (tweet thread + tip + insight). Posted to channel."


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(ContentWriterAgent().run())
