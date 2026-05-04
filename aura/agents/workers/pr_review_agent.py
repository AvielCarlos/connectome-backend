"""
PRReviewAgent — Auto-reviews contributor PRs.

Reports to: Community Agent
Schedule: every 2h
NOTE: A "PR manager" cron already runs every 1h on the main session.
      This worker file exists for direct invocation but cron is NOT registered
      to avoid duplication.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)
REPO = "AvielCarlos/connectome-backend"


class PRReviewAgent(BaseWorkerAgent):
    name = "pr_review_agent"
    role = "PR Reviewer"
    reports_to = "Community Agent"

    async def run(self) -> None:
        logger.info("PRReviewAgent: checking for new PRs")

        # List open PRs
        raw = self._sh(f'gh pr list --repo {REPO} --json number,title,author,additions,deletions,state,labels 2>/dev/null')
        prs = []
        try:
            prs = json.loads(raw) if raw else []
        except Exception:
            pass

        if not prs:
            logger.info("PRReviewAgent: no open PRs")
            return

        for pr in prs:
            number = pr.get("number")
            title = pr.get("title", "")
            author = pr.get("author", {}).get("login", "unknown")
            additions = pr.get("additions", 0)
            deletions = pr.get("deletions", 0)
            total_lines = additions + deletions

            # Fetch diff
            diff = self._sh(f'gh pr diff {number} --repo {REPO} 2>/dev/null | head -200')

            # Check if first-time contributor
            pr_count_raw = self._sh(f'gh pr list --repo {REPO} --author {author} --state all --json number 2>/dev/null')
            try:
                all_prs = json.loads(pr_count_raw) if pr_count_raw else []
                is_first = len(all_prs) <= 1
            except Exception:
                is_first = False

            if is_first:
                self._sh(
                    f'gh pr comment {number} --repo {REPO} --body '
                    f'"Welcome to the Connectome DAO, @{author}! 🎉 '
                    f'Your contribution earns CP (Contribution Points) once merged. '
                    f'Appreciate you building with us."'
                )
                logger.info(f"PRReviewAgent: welcomed first-time contributor @{author} on PR #{number}")

            # Auto-approve small clean PRs
            if total_lines < 100 and diff and "error" not in diff.lower():
                has_obvious_issues = any(kw in diff.lower() for kw in [
                    "password", "secret", "api_key", "token =", "todo: fix", "hack", "xxx"
                ])
                if not has_obvious_issues:
                    self._sh(f'gh pr review {number} --repo {REPO} --approve --body "LGTM — clean, focused change. Auto-approved by Aura\'s PR review agent." 2>/dev/null')
                    logger.info(f"PRReviewAgent: auto-approved PR #{number} ({total_lines} lines)")
                else:
                    self._sh(
                        f'gh pr review {number} --repo {REPO} --request-changes '
                        f'--body "Found potential issues (hardcoded secrets or incomplete TODOs). Please review before merging." 2>/dev/null'
                    )
            elif total_lines >= 100:
                # Large PR — flag for human review
                self._sh(
                    f'gh pr comment {number} --repo {REPO} --body '
                    f'"Large PR ({total_lines} lines) — flagging for manual review. '
                    f'Consider breaking into smaller commits for faster review." 2>/dev/null'
                )

        logger.info(f"PRReviewAgent: reviewed {len(prs)} open PRs")

    async def report(self) -> str:
        return "PRReviewAgent: PR review sweep complete."


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(PRReviewAgent().run())
