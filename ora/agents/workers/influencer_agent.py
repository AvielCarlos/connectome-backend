"""
InfluencerAgent — Finds and tracks influencer collaboration opportunities.

Reports to: CMO Agent
Schedule: weekly Thursday 10am Pacific
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)
PROSPECTS_FILE = "/Users/avielcarlos/.openclaw/workspace/tmp/biz_dev/influencer_prospects.json"
TOPICS = ["self improvement", "productivity", "AI tools", "habit building", "personal growth"]


class InfluencerAgent(BaseWorkerAgent):
    name = "influencer_agent"
    role = "Influencer Relations"
    reports_to = "CMO"

    async def run(self) -> None:
        logger.info("InfluencerAgent: searching for influencer opportunities")
        os.makedirs(os.path.dirname(PROSPECTS_FILE), exist_ok=True)
        week = datetime.now(timezone.utc).strftime("%Y-W%W")

        # 1. Search Twitter for high-engagement accounts per topic
        prospects = self._load_json(PROSPECTS_FILE, default={"prospects": [], "outreach_log": []})
        new_prospects = []

        for topic in TOPICS[:3]:  # limit to 3 searches
            out = self._sh(f'xurl search tweets --query "{topic}" --sort popularity --max-results 20 2>/dev/null')
            if out and "error" not in out.lower():
                # Parse authors from xurl output
                parsed = self._parse_authors(out, topic)
                new_prospects.extend(parsed)

        # Deduplicate
        existing_handles = {p["handle"] for p in prospects["prospects"]}
        for p in new_prospects:
            if p["handle"] not in existing_handles:
                prospects["prospects"].append(p)
                existing_handles.add(p["handle"])

        # 2. Pick 1 new prospect to reach out to this week
        uncontacted = [p for p in prospects["prospects"] if not p.get("contacted")]
        if uncontacted:
            target = uncontacted[0]
            pitch = self._write_pitch(target)

            # Send DM via xurl
            dm_result = self._sh(
                f'xurl dm send --username "{target["handle"]}" --text "{pitch.replace(chr(34), chr(39))}" 2>/dev/null'
            )

            target["contacted"] = True
            target["contacted_week"] = week
            target["pitch_sent"] = pitch
            prospects["outreach_log"].append({
                "week": week,
                "handle": target["handle"],
                "topic": target.get("topic", "self-improvement"),
                "result": "dm_sent" if "error" not in (dm_result or "").lower() else "failed",
            })
            logger.info(f"InfluencerAgent: reached out to @{target['handle']}")

        self._save_json(PROSPECTS_FILE, prospects)

        # 3. Teach Ora
        total = len(prospects["prospects"])
        contacted = sum(1 for p in prospects["prospects"] if p.get("contacted"))
        await self.teach_ora(
            f"Influencer pipeline ({week}): {total} prospects tracked, {contacted} contacted. "
            f"Top topic niches: {', '.join(TOPICS[:3])}. "
            f"Outreach is building brand awareness in self-improvement communities.",
            confidence=0.7,
        )

        logger.info(f"InfluencerAgent: done. {total} prospects, {contacted} contacted.")

    def _parse_authors(self, raw_output: str, topic: str) -> list:
        """Extract @handles from xurl output."""
        import re
        handles = re.findall(r'@([A-Za-z0-9_]{3,30})', raw_output)
        seen = set()
        results = []
        for h in handles:
            if h not in seen:
                seen.add(h)
                results.append({"handle": h, "topic": topic, "discovered_week": datetime.now(timezone.utc).strftime("%Y-W%W"), "contacted": False})
        return results[:5]

    def _write_pitch(self, prospect: dict) -> str:
        topic = prospect.get("topic", "productivity")
        handle = prospect.get("handle", "there")
        return (
            f"Hey @{handle} — I noticed you talk a lot about {topic}, which I love. "
            f"I built iDo, an AI-powered habit coach that helps people turn vague intentions into daily systems. "
            f"I think your audience would genuinely find it useful. "
            f"Would love to chat about a collab if you're open to it — no pressure at all! "
            f"https://ido.ascensiontechnologies.ca"
        )

    async def report(self) -> str:
        data = self._load_json(PROSPECTS_FILE, {})
        total = len(data.get("prospects", []))
        return f"InfluencerAgent: {total} prospects tracked."


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(InfluencerAgent().run())
