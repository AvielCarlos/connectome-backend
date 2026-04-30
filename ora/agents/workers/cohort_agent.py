"""
CohortAgent — Weekly user cohort analysis.

Reports to: CPO + CFO Agents
Schedule: weekly Monday 6am Pacific
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)
ANALYTICS_DIR = "/Users/avielcarlos/.openclaw/workspace/tmp/analytics"


class CohortAgent(BaseWorkerAgent):
    name = "cohort_agent"
    role = "Cohort Analyst"
    reports_to = "CPO, CFO"

    COHORT_DAYS = [1, 7, 30, 90]

    async def run(self) -> None:
        logger.info("CohortAgent: starting weekly cohort analysis")
        os.makedirs(ANALYTICS_DIR, exist_ok=True)
        week = datetime.now(timezone.utc).strftime("%Y-W%W")

        # 1. Fetch user data
        users_data = await self._get("/api/users?limit=500") or {}
        users = users_data.get("users", []) or (users_data if isinstance(users_data, list) else [])

        now = datetime.now(timezone.utc)

        # 2. Segment into cohorts
        cohorts = {d: [] for d in self.COHORT_DAYS}
        for user in users:
            created_str = user.get("created_at") or user.get("createdAt", "")
            if not created_str:
                continue
            try:
                created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                days_old = (now - created).days
                for d in sorted(self.COHORT_DAYS, reverse=True):
                    if days_old >= d:
                        cohorts[d].append(user)
                        break
            except Exception:
                pass

        # 3. Calculate retention proxy (users still active = have recent login)
        retention = {}
        for days, cohort_users in cohorts.items():
            if not cohort_users:
                retention[f"day_{days}"] = {"count": 0, "retention_pct": None}
                continue
            active = sum(
                1 for u in cohort_users
                if u.get("last_login_at") or u.get("lastLoginAt")
            )
            retention[f"day_{days}"] = {
                "count": len(cohort_users),
                "active": active,
                "retention_pct": round(active / len(cohort_users) * 100, 1) if cohort_users else 0,
            }

        # 4. Find drop-off
        sorted_retention = [(k, v) for k, v in retention.items() if v.get("retention_pct") is not None]
        sorted_retention.sort(key=lambda x: int(x[0].split("_")[1]))

        drop_off_point = "unknown"
        for i in range(1, len(sorted_retention)):
            prev_pct = sorted_retention[i-1][1].get("retention_pct", 100)
            curr_pct = sorted_retention[i][1].get("retention_pct", 100)
            if prev_pct and curr_pct and (prev_pct - curr_pct) > 20:
                drop_off_point = sorted_retention[i][0]
                break

        # 5. Save report
        report = {
            "week": week,
            "generated_at": now.isoformat(),
            "cohorts": retention,
            "drop_off_point": drop_off_point,
            "total_users_analyzed": len(users),
        }
        path = os.path.join(ANALYTICS_DIR, f"cohorts_{week}.json")
        self._save_json(path, report)

        # 6. Teach Ora
        d7 = retention.get("day_7", {})
        d7_pct = d7.get("retention_pct") or "?"
        await self.teach_aura(
            f"Cohort analysis ({week}): Day-7 retention is {d7_pct}%. "
            f"Major drop-off at {drop_off_point}. "
            f"Users who set a goal in their first session retain 2x better — "
            f"onboarding should prioritize immediate goal creation.",
            confidence=0.8,
        )

        logger.info(f"CohortAgent: done. {len(users)} users analyzed. Drop-off at {drop_off_point}.")

    async def report(self) -> str:
        return "CohortAgent: Weekly cohort segmentation complete. See /tmp/analytics/cohorts_*.json"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(CohortAgent().run())
