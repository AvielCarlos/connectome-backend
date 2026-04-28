"""
NPSAgent — Weekly satisfaction tracking (card ratings as NPS proxy).

Reports to: CPO Agent
Schedule: weekly Tuesday 6am Pacific
"""

import asyncio
import logging
from datetime import datetime, timezone

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)


class NPSAgent(BaseWorkerAgent):
    name = "nps_agent"
    role = "NPS Analyst"
    reports_to = "CPO"

    async def run(self) -> None:
        logger.info("NPSAgent: computing weekly satisfaction metrics")
        week = datetime.now(timezone.utc).strftime("%Y-W%W")

        # 1. Fetch card ratings
        data = await self._get("/api/screens/ratings?range=7d&limit=1000") or {}
        ratings = data.get("ratings", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])

        if not ratings:
            logger.info("NPSAgent: no ratings data found")
            await self.teach_ora(
                f"NPS check ({week}): No card rating data available this week. "
                "Consider prompting users to rate more screens to improve feedback loop.",
                confidence=0.5,
            )
            return

        # 2. Compute aggregate metrics
        user_ratings: dict = {}
        for r in ratings:
            uid = r.get("user_id") or r.get("userId", "unknown")
            score = r.get("rating") or r.get("score") or 0
            domain = r.get("domain", "general")
            if uid not in user_ratings:
                user_ratings[uid] = {"scores": [], "domain": domain}
            user_ratings[uid]["scores"].append(score)

        user_avgs = {
            uid: {
                "avg": sum(d["scores"]) / len(d["scores"]),
                "domain": d["domain"],
                "count": len(d["scores"]),
            }
            for uid, d in user_ratings.items()
        }

        all_avgs = [v["avg"] for v in user_avgs.values()]
        overall_avg = round(sum(all_avgs) / len(all_avgs), 2) if all_avgs else 0

        at_risk = {uid: v for uid, v in user_avgs.items() if v["avg"] < 2.5}
        promoters = {uid: v for uid, v in user_avgs.items() if v["avg"] > 4.0}

        # 3. Get dominant domain for detractors
        detractor_domains: dict = {}
        for v in at_risk.values():
            d = v["domain"]
            detractor_domains[d] = detractor_domains.get(d, 0) + 1
        top_detractor_domain = max(detractor_domains, key=detractor_domains.get) if detractor_domains else "unknown"

        # 4. Get dominant profile for promoters
        promoter_domains: dict = {}
        for v in promoters.values():
            d = v["domain"]
            promoter_domains[d] = promoter_domains.get(d, 0) + 1
        top_promoter_domain = max(promoter_domains, key=promoter_domains.get) if promoter_domains else "unknown"

        # 5. Flag users (log for now — real re-engagement via other agents)
        logger.info(f"NPSAgent: {len(at_risk)} at-risk users, {len(promoters)} promoters")

        # 6. Teach Ora
        await self.teach_ora(
            f"User satisfaction ({week}): {overall_avg}/5 average card rating across {len(user_ratings)} users. "
            f"At-risk users (< 2.5 avg): {len(at_risk)} — predominantly in '{top_detractor_domain}' domain. "
            f"Promoters (> 4.0 avg): {len(promoters)} — strongest in '{top_promoter_domain}'. "
            f"Recommend: re-engage at-risk users with a personalized check-in; "
            f"offer promoters early access to premium features.",
            confidence=0.85,
        )

        logger.info(f"NPSAgent: done. Overall avg {overall_avg}/5.")

    async def report(self) -> str:
        return "NPSAgent: Weekly satisfaction metrics computed and taught to Ora."


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(NPSAgent().run())
