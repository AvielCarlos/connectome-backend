"""
SupportAgent — Monitors Ora conversations for user issues.

Reports to: CPO Agent
Schedule: every 4h
"""

import asyncio
import logging
from datetime import datetime, timezone

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)

ISSUE_KEYWORDS = [
    "not working", "error", "broken", "can't", "cannot", "doesn't work",
    "doesn't load", "crashed", "bug", "help", "problem", "issue", "fail",
]

KNOWN_ISSUES = [
    "login", "password reset", "notification", "slow loading",
]


class SupportAgent(BaseWorkerAgent):
    name = "support_agent"
    role = "Customer Support"
    reports_to = "CPO"

    async def run(self) -> None:
        logger.info("SupportAgent: scanning conversations for issues")
        now = datetime.now(timezone.utc)

        # 1. Fetch recent conversations
        data = await self._get("/api/ora/conversations?limit=200&range=4h") or {}
        convs = data.get("conversations", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])

        flagged = []
        for conv in convs:
            messages = conv.get("messages", []) or []
            user_msgs = [m.get("content", "") for m in messages if m.get("role") == "user"]
            for msg in user_msgs:
                msg_lower = msg.lower()
                if any(kw in msg_lower for kw in ISSUE_KEYWORDS):
                    flagged.append({
                        "conversation_id": conv.get("id"),
                        "user_id": conv.get("user_id") or conv.get("userId"),
                        "message": msg,
                        "timestamp": conv.get("created_at") or conv.get("updatedAt", ""),
                    })
                    break

        logger.info(f"SupportAgent: found {len(flagged)} flagged conversations")

        for issue in flagged:
            await self._handle_issue(issue)

        if flagged:
            await self.teach_aura(
                f"Support scan ({now.strftime('%Y-%m-%d %H:%M')}): Found {len(flagged)} user issue(s). "
                f"Common themes: errors, crashes, login problems. "
                f"Responding empathetically with guidance improves retention.",
                confidence=0.8,
            )

        logger.info("SupportAgent: done.")

    async def _handle_issue(self, issue: dict) -> None:
        msg = issue.get("message", "").lower()
        conv_id = issue.get("conversation_id")
        user_id = issue.get("user_id")
        issue_msg = issue.get("message", "")

        # Classify
        is_known = any(k in msg for k in KNOWN_ISSUES)

        if not is_known:
            # Create GitHub issue for new bugs
            short_msg = issue_msg[:60].replace('"', "'")
            body_text = "Reported by user " + str(user_id) + " in conversation " + str(conv_id) + ".\n\nMessage: " + issue_msg
            cmd = (
                'gh issue create --repo AvielCarlos/connectome-backend '
                '--label bug '
                '--title "User-reported issue: ' + short_msg + '" '
                '--body "' + body_text.replace('"', "'") + '"'
            )
            self._sh(cmd)
            logger.info(f"SupportAgent: created GitHub issue for conversation {conv_id}")

        # Reply via Ora
        if user_id:
            await self._post("/api/ora/chat", {
                "user_id": user_id,
                "message": (
                    "Hi! I noticed you might be having trouble — I'm so sorry about that! "
                    "Our team has been notified and is looking into it. "
                    "In the meantime, try refreshing the app or logging out and back in. "
                    "If the issue persists, reply here and I'll escalate it right away."
                ),
                "source": "support_agent",
            })

    async def report(self) -> str:
        return "SupportAgent: Conversation scan complete. Issues flagged, GitHub issues created, users replied to."


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(SupportAgent().run())
