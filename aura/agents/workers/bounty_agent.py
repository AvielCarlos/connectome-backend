"""
BountyAgent — Creates and manages contributor task bounties.

Reports to: Community Agent
Schedule: weekly Monday 11am Pacific
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)
REPO = "AvielCarlos/connectome-backend"
BOUNTY_FILE = os.path.join(os.getenv("CONNECTOME_RUNTIME_DIR", "/tmp/connectome"), "biz_dev", "bounties.json")

# CP reward tiers by complexity
CP_TIERS = {
    "trivial": 10,    # typos, docs
    "easy": 25,       # good first issue
    "medium": 75,     # feature, refactor
    "hard": 150,      # new system, complex feature
    "epic": 300,      # major architecture
}


class BountyAgent(BaseWorkerAgent):
    name = "bounty_agent"
    role = "Bounty Manager"
    reports_to = "Community Agent"

    async def run(self) -> None:
        logger.info("BountyAgent: reviewing GitHub issues for bounties")
        os.makedirs(os.path.dirname(BOUNTY_FILE), exist_ok=True)
        week = datetime.now(timezone.utc).strftime("%Y-W%W")

        # 1. Fetch issues
        raw = self._sh(
            f'gh issue list --repo {REPO} --json number,title,labels,body,state --limit 50 2>/dev/null'
        )
        issues = []
        try:
            issues = json.loads(raw) if raw else []
        except Exception:
            pass

        bounty_issues = []
        new_bounties = []

        for issue in issues:
            labels = [l.get("name", "") for l in (issue.get("labels") or [])]
            number = issue.get("number")
            title = issue.get("title", "")
            body = issue.get("body") or ""

            is_bounty = "bounty" in labels or "good first issue" in labels
            already_has_cp = any("CP" in l or "cp" in l.lower() for l in labels)

            if is_bounty:
                bounty_issues.append(issue)

                if not already_has_cp:
                    # Estimate complexity
                    cp = self._estimate_cp(title, body, labels)

                    # Add CP label
                    label_name = f"reward:{cp}CP"
                    self._sh(f'gh label create "{label_name}" --repo {REPO} --color "gold" 2>/dev/null || true')
                    self._sh(f'gh issue edit {number} --repo {REPO} --add-label "{label_name}" 2>/dev/null')

                    # Comment on issue
                    self._sh(
                        f'gh issue comment {number} --repo {REPO} --body '
                        f'"💰 **Bounty: {cp} CP** (Contribution Points)\n\n'
                        f'This issue has been tagged as a bounty! '
                        f'Claim it by commenting below, then open a PR. '
                        f'CP are redeemable for services and recognition in the Connectome DAO." 2>/dev/null'
                    )

                    new_bounties.append({"number": number, "title": title, "cp": cp})
                    logger.info(f"BountyAgent: assigned {cp} CP to issue #{number}")

        # 2. Post new bounties to Telegram channel
        if new_bounties:
            for bounty in new_bounties[:3]:  # max 3 per run
                msg = (
                    f"🎯 *New Bounty Available!*\n\n"
                    f"**{bounty['title']}**\n"
                    f"Worth: {bounty['cp']} CP\n"
                    f"Claim: https://github.com/{REPO}/issues/{bounty['number']}\n\n"
                    f"First to open a merged PR wins the bounty!"
                )
                await self.post_to_channel(msg)

        # 3. Save state
        bounty_state = self._load_json(BOUNTY_FILE, default={"weeks": []})
        bounty_state["weeks"].append({
            "week": week,
            "total_bounties": len(bounty_issues),
            "new_this_week": len(new_bounties),
            "new_bounties": new_bounties,
        })
        self._save_json(BOUNTY_FILE, bounty_state)

        logger.info(f"BountyAgent: done. {len(new_bounties)} new bounties assigned.")

    def _estimate_cp(self, title: str, body: str, labels: list) -> int:
        text = (title + " " + body).lower()
        if "good first issue" in labels:
            return CP_TIERS["easy"]
        if any(w in text for w in ["refactor", "architecture", "system", "migration"]):
            return CP_TIERS["hard"]
        if any(w in text for w in ["feature", "new", "add", "implement"]):
            return CP_TIERS["medium"]
        if any(w in text for w in ["typo", "doc", "readme", "comment"]):
            return CP_TIERS["trivial"]
        return CP_TIERS["easy"]

    async def report(self) -> str:
        data = self._load_json(BOUNTY_FILE, {})
        last = (data.get("weeks") or [{}])[-1]
        return f"BountyAgent: {last.get('total_bounties','?')} active bounties."


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(BountyAgent().run())
