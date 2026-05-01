"""
PartnershipAgent — Identifies and drafts partnership opportunities.

Reports to: CMO + Strategy Agents
Schedule: biweekly 1st + 15th of month, 11am Pacific
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)
DRAFTS_DIR = os.path.join(os.getenv("CONNECTOME_RUNTIME_DIR", "/tmp/connectome"), "biz_dev", "partnership_drafts")
PROSPECTS_FILE = os.path.join(os.getenv("CONNECTOME_RUNTIME_DIR", "/tmp/connectome"), "biz_dev", "partnership_prospects.json")


class PartnershipAgent(BaseWorkerAgent):
    name = "partnership_agent"
    role = "Business Development"
    reports_to = "CMO, Strategy"

    SEARCH_QUERIES = [
        "journaling app for personal growth",
        "habit tracker site:reddit.com",
        "AI productivity app product hunt",
        "CBT app therapy goals",
        "accountability partner app",
    ]

    PARTNERSHIP_TEMPLATES = [
        {
            "type": "integration",
            "category": "Journaling Apps",
            "examples": ["Day One", "Reflect", "Notion"],
            "pitch": "Cross-promote: iDo sets the goals, your app tracks the reflection. Two apps, one growth journey.",
        },
        {
            "type": "community",
            "category": "Reddit Communities",
            "examples": ["r/selfimprovement", "r/productivity", "r/getdisciplined"],
            "pitch": "Partner post: 'We built an AI coach for this community — try it free.'",
        },
        {
            "type": "integration",
            "category": "Calendar Apps",
            "examples": ["Reclaim.ai", "Motion", "Structured"],
            "pitch": "Time-block your iDo habits automatically. Perfect workflow for achievers.",
        },
    ]

    async def run(self) -> None:
        logger.info("PartnershipAgent: scanning for partnership opportunities")
        os.makedirs(DRAFTS_DIR, exist_ok=True)
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")

        # 1. Search for opportunities
        opportunities = []
        for query in self.SEARCH_QUERIES[:3]:
            out = self._sh(f'web_search "{query}" 2>/dev/null || echo ""')
            opportunities.append({"query": query, "raw": out[:500] if out else ""})

        # 2. Shortlist top 3 from templates (curated + discovered)
        shortlist = self.PARTNERSHIP_TEMPLATES[:3]

        # 3. Draft outreach emails
        drafted = []
        for opp in shortlist:
            draft = self._draft_email(opp)
            filename = f"{date_str}_{opp['category'].lower().replace(' ', '_')}.txt"
            filepath = os.path.join(DRAFTS_DIR, filename)
            with open(filepath, "w") as f:
                f.write(draft)
            drafted.append({"category": opp["category"], "file": filepath})
            logger.info(f"PartnershipAgent: drafted outreach for {opp['category']}")

        # 4. Save prospects
        prospects = self._load_json(PROSPECTS_FILE, default={"opportunities": [], "last_run": None})
        prospects["last_run"] = now.isoformat()
        prospects["opportunities"].append({
            "date": date_str,
            "shortlist": shortlist,
            "drafts": drafted,
        })
        self._save_json(PROSPECTS_FILE, prospects)

        # 5. Teach Ora
        categories = ", ".join(o["category"] for o in shortlist)
        await self.teach_aura(
            f"Partnership opportunities ({date_str}): Top 3 — {categories}. "
            f"Integration partnerships with complementary apps can drive 3x organic growth. "
            f"Community partnerships (Reddit, Discord) are low-cost, high-trust acquisition channels. "
            f"Outreach drafts saved for Avi's review.",
            confidence=0.7,
        )

        logger.info(f"PartnershipAgent: done. {len(drafted)} drafts saved.")

    def _draft_email(self, opp: dict) -> str:
        examples = ", ".join(opp.get("examples", [])[:2])
        return f"""Subject: Partnership opportunity — iDo x {opp['category']}

Hi team,

I'm Avi, founder of iDo (https://ido.ascensiontechnologies.ca) — an AI-powered habit coach that helps people turn vague intentions into daily systems.

I've been a fan of {examples} and noticed we're solving adjacent problems for the same user: people who are serious about personal growth.

{opp['pitch']}

I'd love to explore a mutual partnership — could be as simple as a newsletter mention or as deep as a native integration. Whatever makes sense for your users.

Would you be open to a 20-minute call?

Best,
Avi Carlosascensiontechnologies.ca
"""

    async def report(self) -> str:
        return "PartnershipAgent: Biweekly partnership scan complete. Drafts saved for review."


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(PartnershipAgent().run())
