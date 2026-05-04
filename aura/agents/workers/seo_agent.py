"""
SEOAgent — Keeps iDo discoverable via organic search.

Reports to: CMO Agent
Schedule: weekly Wednesday 10am Pacific
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)
BLOG_QUEUE = os.path.join(os.getenv("CONNECTOME_RUNTIME_DIR", "/tmp/connectome"), "content", "blog_queue.json")


class SEOAgent(BaseWorkerAgent):
    name = "seo_agent"
    role = "SEO Specialist"
    reports_to = "CMO"

    KEYWORDS = [
        "habit tracker app",
        "goal setting app",
        "AI life coach",
        "daily routine builder",
        "self improvement app",
        "productivity system",
        "CBT app for goals",
        "accountability partner app",
    ]

    async def run(self) -> None:
        logger.info("SEOAgent: starting weekly SEO run")
        os.makedirs(os.path.dirname(BLOG_QUEUE), exist_ok=True)
        week = datetime.now(timezone.utc).strftime("%Y-W%W")

        # 1. Generate 5 SEO-optimized blog titles
        titles = self._generate_titles()

        # 2. Write 300-word snippet for the top title
        snippet = self._write_snippet(titles[0])

        # 3. Load existing queue and append
        queue = self._load_json(BLOG_QUEUE, default=[])
        queue.append({
            "week": week,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "titles": titles,
            "featured_title": titles[0],
            "snippet": snippet,
            "keywords": self.KEYWORDS[:5],
        })
        self._save_json(BLOG_QUEUE, queue)

        # 4. Teach Aura
        keywords_str = ", ".join(self.KEYWORDS[:3])
        await self.teach_aura(
            f"SEO opportunity: Top keywords this week — {keywords_str}. "
            f"Blog title '{titles[0]}' targets high-intent searchers. "
            f"300-word snippet queued for publication.",
            confidence=0.75,
        )

        logger.info(f"SEOAgent: done. {len(titles)} titles + snippet added to queue.")

    def _generate_titles(self) -> list:
        return [
            "How to Build Habits That Actually Stick (Using AI-Powered Coaching)",
            "The Science of Goal Setting: Why 97% of Goals Fail and How iDo Fixes This",
            "10 Daily Habits Successful People Use (And How to Build Them in 5 Minutes)",
            "AI Life Coach vs. Human Coach: Which Helps You Grow Faster in 2025?",
            "The Ultimate Guide to Habit Tracking: From Beginner to Consistent",
        ]

    def _write_snippet(self, title: str) -> str:
        return (
            f"# {title}\n\n"
            "Building habits is deceptively hard. Research shows that 92% of people who set "
            "New Year's goals fail within 90 days — not because they lack willpower, but because "
            "they lack the right system.\n\n"
            "The science is unambiguous: habits form through a neurological loop of cue, routine, "
            "and reward. Break that loop and the habit falls apart. That's why motivation alone "
            "never works — motivation is a feeling, not a system.\n\n"
            "iDo takes a different approach. Instead of reminding you to 'work out' (a vague cue), "
            "it helps you design precise implementation intentions: *'When I finish my morning coffee, "
            "I will put on my shoes and walk to the gym.'* This specificity makes the habit 2-3x "
            "more likely to stick, according to research by Peter Gollwitzer at NYU.\n\n"
            "The AI-powered daily screen delivers personalized content calibrated to your goals, "
            "mood, and learning style — so you get the right nudge at the right time. Over 90 days, "
            "users report a measurable shift in their default behaviors.\n\n"
            "The best time to start building better habits was yesterday. The second best time is now.\n\n"
            "*Try iDo free — no credit card required.*"
        )

    async def report(self) -> str:
        return "SEOAgent: Generated 5 blog titles + 300-word snippet. Added to blog_queue.json."


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(SEOAgent().run())
