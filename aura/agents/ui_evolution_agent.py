"""
UIEvolutionAgent — Proposes and applies frontend changes based on UX insights.

- Reads aura:ux_insights from Redis
- Uses GPT-4o to propose component-level UI changes
- Classifies risk: LOW (text/color) vs HIGH (new screens, flow changes)
- Auto-applies LOW risk changes to connectome-web via GitHub API
- Creates GitHub Issues for HIGH risk changes
- Stores proposals in Redis: aura:ui_proposals
"""

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TELEGRAM_CHAT_ID = 5716959016


class UIEvolutionAgent:
    """Proposes and applies frontend UI changes based on UX insights."""

    def __init__(self, openai_client=None, telegram_token: Optional[str] = None):
        self._openai = openai_client
        self._telegram_token = telegram_token
        self._github_token = os.environ.get("GITHUB_TOKEN", "")

    async def run(self) -> Dict[str, Any]:
        """
        Execute the UI evolution pipeline:
        1. Read UX insights from Redis
        2. Propose UI changes via GPT-4o
        3. Apply LOW risk changes via GitHub API
        4. Create GitHub Issues for HIGH risk changes
        """
        logger.info("UIEvolutionAgent: starting run")

        # 1. Read UX insights from Redis
        insights = await self._read_ux_insights()
        if not insights:
            logger.info("UIEvolutionAgent: no UX insights available, skipping")
            return {"proposals": [], "applied": [], "issues": [], "skipped": "no_insights"}

        # 2. Propose UI changes
        proposals = await self._propose_changes(insights)

        if not proposals:
            return {"proposals": [], "applied": [], "issues": []}

        # 3. Classify and act
        applied = []
        issues_created = []

        for proposal in proposals:
            risk = proposal.get("risk", "high").lower()
            if risk == "low":
                result = await self._apply_low_risk_change(proposal)
                if result.get("success"):
                    applied.append(proposal)
            else:
                issue_url = await self._create_github_issue(proposal)
                if issue_url:
                    proposal["issue_url"] = issue_url
                    issues_created.append(proposal)

        # 4. Store proposals in Redis
        await self._store_proposals(proposals, applied, issues_created)

        logger.info(
            f"UIEvolutionAgent: {len(applied)} applied, {len(issues_created)} issues created"
        )
        return {
            "proposals": proposals,
            "applied": applied,
            "issues": issues_created,
        }

    # ------------------------------------------------------------------
    # Redis helpers
    # ------------------------------------------------------------------

    async def _read_ux_insights(self) -> List[Dict[str, Any]]:
        """Read UX insights from Redis."""
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            raw = await r.get("aura:ux_insights")
            if not raw:
                return []
            data = json.loads(raw)
            return data.get("insights", [])
        except Exception as e:
            logger.warning(f"UIEvolutionAgent: Redis read failed: {e}")
            return []

    async def _store_proposals(
        self,
        proposals: List[Dict],
        applied: List[Dict],
        issues: List[Dict],
    ) -> None:
        """Store proposals in Redis: aura:ui_proposals (24h TTL)."""
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            await r.set(
                "aura:ui_proposals",
                json.dumps({
                    "proposals": proposals,
                    "applied": applied,
                    "issues": issues,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }),
                ex=24 * 3600,
            )
        except Exception as e:
            logger.warning(f"UIEvolutionAgent: Redis store failed: {e}")

    # ------------------------------------------------------------------
    # Proposal generation
    # ------------------------------------------------------------------

    async def _propose_changes(self, insights: List[Dict]) -> List[Dict[str, Any]]:
        """Use GPT-4o to propose specific UI changes based on UX insights."""
        if not self._openai:
            return []

        insights_text = json.dumps(insights, indent=2)[:2000]

        try:
            # Also read recent user requests from aura_conversations
            recent_requests = await self._get_recent_user_requests()

            prompt = f"""You are Aura's UI evolution agent for a React/TypeScript growth app (Connectome).

UX Insights:
{insights_text}

Recent user requests:
{recent_requests}

Propose 3-5 specific UI improvements. For each proposal:
- Classify risk: LOW (only text copy, colors, button labels) or HIGH (new screens, navigation changes, new components)
- Be specific about what file/component to change
- LOW risk changes should be small and safe to auto-apply

Return JSON array: [{{
  "title": "short change title",
  "description": "what to change and why",
  "risk": "low|high",
  "file": "relative path in src/ e.g. src/pages/FeedPage.tsx",
  "change_type": "copy|color|layout|new_feature|flow_change",
  "before": "current text/value (if copy change)",
  "after": "new text/value (if copy change)",
  "rationale": "data-driven reason"
}}]"""

            response = await self._openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=1000,
                response_format={"type": "json_object"},
            )

            raw = json.loads(response.choices[0].message.content)
            if isinstance(raw, list):
                return raw
            if isinstance(raw, dict):
                for k in ("proposals", "changes", "items"):
                    if k in raw and isinstance(raw[k], list):
                        return raw[k]
            return []

        except Exception as e:
            logger.warning(f"UIEvolutionAgent: proposal generation failed: {e}")
            return []

    async def _get_recent_user_requests(self) -> str:
        """Get recent user messages to identify patterns."""
        try:
            from core.database import fetch as db_fetch
            rows = await db_fetch(
                """
                SELECT message
                FROM aura_conversations
                WHERE role = 'user'
                  AND created_at >= NOW() - INTERVAL '7 days'
                ORDER BY created_at DESC
                LIMIT 100
                """
            )
            if not rows:
                return "No recent requests available"
            msgs = [r["message"] for r in rows if r.get("message")]
            # Sample unique patterns
            return "\n".join(f"- {m[:120]}" for m in msgs[:20])
        except Exception as e:
            logger.debug(f"UIEvolutionAgent: could not fetch user requests: {e}")
            return "DB not available"

    # ------------------------------------------------------------------
    # Apply LOW risk changes
    # ------------------------------------------------------------------

    async def _apply_low_risk_change(self, proposal: Dict[str, Any]) -> Dict[str, Any]:
        """
        Apply a LOW risk change (copy/color) to connectome-web via GitHub API.
        Only applies copy (text) changes for safety.
        """
        if not self._github_token:
            logger.info("UIEvolutionAgent: no GITHUB_TOKEN, skipping auto-apply")
            return {"success": False, "reason": "no_github_token"}

        change_type = proposal.get("change_type", "")
        if change_type not in ("copy", "color"):
            # Only auto-apply text/color changes
            return {"success": False, "reason": "change_type_not_auto_applicable"}

        before = proposal.get("before", "")
        after = proposal.get("after", "")
        file_path = proposal.get("file", "")

        if not all([before, after, file_path]):
            return {"success": False, "reason": "missing_fields"}

        try:
            import base64
            import httpx

            headers = {
                "Authorization": f"token {self._github_token}",
                "Accept": "application/vnd.github.v3+json",
            }

            async with httpx.AsyncClient(timeout=20) as client:
                # Get current file content
                resp = await client.get(
                    f"https://api.github.com/repos/AvielCarlos/connectome-web/contents/{file_path}",
                    headers=headers,
                )
                if resp.status_code != 200:
                    return {"success": False, "reason": f"github_get_failed_{resp.status_code}"}

                file_data = resp.json()
                current_content = base64.b64decode(file_data["content"]).decode("utf-8")
                sha = file_data["sha"]

                if before not in current_content:
                    return {"success": False, "reason": "before_text_not_found"}

                new_content = current_content.replace(before, after, 1)
                encoded = base64.b64encode(new_content.encode("utf-8")).decode("utf-8")

                # Commit the change
                commit_resp = await client.put(
                    f"https://api.github.com/repos/AvielCarlos/connectome-web/contents/{file_path}",
                    headers=headers,
                    json={
                        "message": f"aura: {proposal.get('title', 'UI improvement')[:60]}",
                        "content": encoded,
                        "sha": sha,
                        "branch": "main",
                    },
                )

                if commit_resp.status_code in (200, 201):
                    logger.info(f"UIEvolutionAgent: applied change to {file_path}")
                    await self._send_telegram(
                        f"🎨 *Aura UI Change Applied*\n\n"
                        f"File: `{file_path}`\n"
                        f"Change: {proposal.get('title', '')}\n"
                        f"Before: `{before[:60]}`\n"
                        f"After: `{after[:60]}`"
                    )
                    return {"success": True, "file": file_path}
                else:
                    return {"success": False, "reason": f"commit_failed_{commit_resp.status_code}"}

        except Exception as e:
            logger.warning(f"UIEvolutionAgent: auto-apply failed: {e}")
            return {"success": False, "reason": str(e)}

    # ------------------------------------------------------------------
    # GitHub Issues for HIGH risk
    # ------------------------------------------------------------------

    async def _create_github_issue(self, proposal: Dict[str, Any]) -> Optional[str]:
        """Create a GitHub Issue for HIGH risk proposals."""
        title = proposal.get("title", "UI Improvement Proposal")
        description = proposal.get("description", "")
        rationale = proposal.get("rationale", "")
        file_path = proposal.get("file", "")
        change_type = proposal.get("change_type", "")

        body = f"""## 🤖 Aura UI Proposal

**Generated by:** UIEvolutionAgent  
**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d')}  
**Risk Level:** HIGH  
**Change Type:** {change_type}  
**Target File:** `{file_path}`

## Description
{description}

## Rationale (Data-Driven)
{rationale}

## Implementation Notes
- This change was classified as HIGH risk and requires human review before implementation
- Please review and implement when appropriate

---
*Automatically generated by Aura's UIEvolutionAgent based on UX research data*
"""

        try:
            # Try gh CLI first (faster, already authenticated)
            result = subprocess.run(
                [
                    "gh", "issue", "create",
                    "--repo", "AvielCarlos/connectome-web",
                    "--title", title[:100],
                    "--body", body,
                    "--label", "aura-proposal",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                issue_url = result.stdout.strip()
                logger.info(f"UIEvolutionAgent: created issue {issue_url}")
                return issue_url
            else:
                logger.warning(f"UIEvolutionAgent: gh issue create failed: {result.stderr}")

        except FileNotFoundError:
            # gh CLI not available, try GitHub API
            pass
        except Exception as e:
            logger.warning(f"UIEvolutionAgent: gh CLI failed: {e}")

        # Fallback: GitHub API
        if self._github_token:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.post(
                        "https://api.github.com/repos/AvielCarlos/connectome-web/issues",
                        headers={
                            "Authorization": f"token {self._github_token}",
                            "Accept": "application/vnd.github.v3+json",
                        },
                        json={"title": title[:100], "body": body, "labels": ["aura-proposal"]},
                    )
                    if resp.status_code == 201:
                        url = resp.json().get("html_url", "")
                        logger.info(f"UIEvolutionAgent: created issue via API {url}")
                        return url
            except Exception as e:
                logger.warning(f"UIEvolutionAgent: GitHub API issue creation failed: {e}")

        return None

    # ------------------------------------------------------------------
    # Telegram
    # ------------------------------------------------------------------

    async def _send_telegram(self, message: str) -> None:
        if not self._telegram_token:
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{self._telegram_token}/sendMessage",
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": message,
                        "parse_mode": "Markdown",
                    },
                )
        except Exception as e:
            logger.debug(f"UIEvolutionAgent: Telegram send failed: {e}")
