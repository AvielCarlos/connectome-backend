"""
CPRewardsAgent — Processes contributor rewards for merged PRs.

Reports to: Community Agent
Schedule: daily 8pm Pacific
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)
REPO = "AvielCarlos/connectome-backend"
REWARDS_LOG = os.path.join(os.getenv("CONNECTOME_RUNTIME_DIR", "/tmp/connectome"), "biz_dev", "cp_rewards_log.json")

# CP per PR size
BASE_CP = 25
BONUS_PER_100_LINES = 10
MAX_CP = 200


class CPRewardsAgent(BaseWorkerAgent):
    name = "cp_rewards_agent"
    role = "Contributor Rewards"
    reports_to = "Community Agent"

    async def run(self) -> None:
        logger.info("CPRewardsAgent: checking for recently merged PRs")
        os.makedirs(os.path.dirname(REWARDS_LOG), exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 1. Get recently merged PRs
        raw = self._sh(
            f'gh pr list --repo {REPO} --state closed '
            f'--json number,title,author,mergedAt,additions,deletions '
            f'--search "is:merged" --limit 20 2>/dev/null'
        )
        prs = []
        try:
            prs = json.loads(raw) if raw else []
        except Exception:
            pass

        # Filter to PRs merged in last 24h
        processed = self._load_json(REWARDS_LOG, default={"processed_prs": [], "daily": []})
        already_processed = set(processed.get("processed_prs", []))

        newly_merged = []
        for pr in prs:
            merged_at = pr.get("mergedAt") or ""
            if not merged_at or str(pr.get("number")) in already_processed:
                continue
            newly_merged.append(pr)

        logger.info(f"CPRewardsAgent: {len(newly_merged)} new merged PRs to process")

        rewarded = []
        for pr in newly_merged:
            number = pr.get("number")
            author = pr.get("author", {}).get("login", "unknown")
            additions = pr.get("additions", 0)
            deletions = pr.get("deletions", 0)
            total_lines = additions + deletions

            # Skip bots
            if author.endswith("[bot]") or author == "dependabot":
                continue

            # Calculate CP
            bonus = min((total_lines // 100) * BONUS_PER_100_LINES, MAX_CP - BASE_CP)
            cp = min(BASE_CP + bonus, MAX_CP)

            # Check for bounty label CP bonus
            labels_raw = self._sh(f'gh pr view {number} --repo {REPO} --json labels 2>/dev/null')
            try:
                labels = json.loads(labels_raw).get("labels", []) if labels_raw else []
                for label in labels:
                    name = label.get("name", "")
                    if "CP" in name:
                        try:
                            bounty_cp = int("".join(filter(str.isdigit, name.replace("CP", ""))))
                            cp = max(cp, bounty_cp)
                        except Exception:
                            pass
            except Exception:
                pass

            # Award CP via API
            await self._post("/api/dao/rewards", {
                "contributor": author,
                "amount": cp,
                "reason": f"PR #{number} merged: {pr.get('title', '')[:60]}",
                "pr_number": number,
                "repo": REPO,
            })

            # Congratulations message via GitHub
            self._sh(
                f'gh pr comment {number} --repo {REPO} --body '
                f'"🎉 **{cp} CP awarded!** Thanks @{author} for your contribution! '
                f'Your CP have been added to your Connectome DAO balance." 2>/dev/null'
            )

            already_processed.add(str(number))
            rewarded.append({"pr": number, "author": author, "cp": cp})
            logger.info(f"CPRewardsAgent: awarded {cp} CP to @{author} for PR #{number}")

        # 3. Log
        processed["processed_prs"] = list(already_processed)[-200:]
        processed["daily"].append({
            "date": today,
            "rewarded": rewarded,
            "total_cp_distributed": sum(r["cp"] for r in rewarded),
        })
        self._save_json(REWARDS_LOG, processed)

        if rewarded:
            total_cp = sum(r["cp"] for r in rewarded)
            logger.info(f"CPRewardsAgent: {len(rewarded)} contributors rewarded, {total_cp} CP distributed")

    async def report(self) -> str:
        data = self._load_json(REWARDS_LOG, {})
        last = (data.get("daily") or [{}])[-1]
        return f"CPRewardsAgent: {last.get('total_cp_distributed','0')} CP distributed today."


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(CPRewardsAgent().run())
