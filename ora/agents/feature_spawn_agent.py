"""
FeatureSpawnAgent — Detects recurring user needs and proposes new features.

When users repeatedly ask for something Connectome doesn't have, Ora opens a
GitHub Issue with a full spec.

Flow:
  1. Read last 200 ora_conversations messages (user messages only)
  2. Use GPT-4o to identify recurring requests/needs (min 3 occurrences)
  3. For each identified gap: write a feature spec + open GitHub Issue
  4. Store in Redis: ora:feature_proposals (24h TTL)
"""

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TELEGRAM_CHAT_ID = 5716959016


class FeatureSpawnAgent:
    """Detects recurring user needs and spawns GitHub feature issues."""

    def __init__(self, openai_client=None, telegram_token: Optional[str] = None):
        self._openai = openai_client
        self._telegram_token = telegram_token
        self._github_token = os.environ.get("GITHUB_TOKEN", "")

    async def run(self) -> Dict[str, Any]:
        """
        Execute the feature detection pipeline.
        Returns list of proposed features.
        """
        logger.info("FeatureSpawnAgent: starting run")

        # 1. Read recent conversations
        messages = await self._get_recent_messages(limit=200)
        if not messages:
            logger.info("FeatureSpawnAgent: no conversation data available")
            return {"proposals": [], "issues_created": 0}

        # 2. Identify recurring needs
        gaps = await self._identify_gaps(messages)
        if not gaps:
            logger.info("FeatureSpawnAgent: no recurring needs identified")
            return {"proposals": [], "issues_created": 0}

        # 3. Create GitHub Issues for each gap
        issues_created = 0
        proposals = []

        for gap in gaps:
            issue_url = await self._create_feature_issue(gap)
            if issue_url:
                gap["issue_url"] = issue_url
                issues_created += 1
            proposals.append(gap)

        # 4. Store in Redis
        await self._store_proposals(proposals)

        # 5. Notify Avi if meaningful features found
        if issues_created > 0:
            await self._send_telegram(
                f"💡 *Ora Feature Proposals*\n\n"
                f"Detected {issues_created} recurring user need(s):\n"
                + "\n".join(
                    f"• {p.get('title', 'Feature')} — {p.get('issue_url', 'no issue')}"
                    for p in proposals
                    if p.get("issue_url")
                )
            )

        logger.info(f"FeatureSpawnAgent: {issues_created} feature issues created")
        return {"proposals": proposals, "issues_created": issues_created}

    # ------------------------------------------------------------------
    # Data gathering
    # ------------------------------------------------------------------

    async def _get_recent_messages(self, limit: int = 200) -> List[str]:
        """Fetch recent user messages from ora_conversations."""
        try:
            from core.database import fetch as db_fetch
            rows = await db_fetch(
                """
                SELECT message
                FROM ora_conversations
                WHERE role = 'user'
                  AND created_at >= NOW() - INTERVAL '30 days'
                  AND message IS NOT NULL
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
            return [r["message"] for r in (rows or []) if r.get("message")]
        except Exception as e:
            logger.warning(f"FeatureSpawnAgent: DB query failed: {e}")
            return []

    # ------------------------------------------------------------------
    # Gap identification
    # ------------------------------------------------------------------

    async def _identify_gaps(self, messages: List[str]) -> List[Dict[str, Any]]:
        """Use GPT-4o to identify recurring feature requests (min 3 occurrences)."""
        if not self._openai:
            return []

        # Check Redis to avoid re-opening issues for things we've already proposed
        already_proposed = await self._get_already_proposed_topics()

        messages_text = "\n".join(f"- {m[:200]}" for m in messages[:150])

        prompt = f"""You are Ora's feature detection agent for Connectome — a personalized growth app with:
- AI-powered content feed (articles, coaching cards)
- Goal tracking (iVive self-growth, Eviva contribution, Aventi aliveness)
- Ora AI chat assistant
- DAO community features
- Twitter/X integration for personalization

These are recent user messages. Identify recurring feature requests that appear 3+ times.
Exclude already-proposed: {json.dumps(already_proposed)}

User messages:
{messages_text}

Return JSON array of identified gaps: [{{
  "title": "Feature: short name",
  "occurrences": estimated_count,
  "user_story": "As a user, I want... so that...",
  "acceptance_criteria": ["criterion 1", "criterion 2", "criterion 3"],
  "target_repo": "connectome-backend or connectome-web",
  "implementation_sketch": "brief technical approach (2-3 sentences)",
  "priority": "high|medium|low",
  "representative_quotes": ["example user quote 1", "example user quote 2"]
}}]

Only include items with clear recurring demand (3+ occurrences). Return empty array if none found."""

        try:
            response = await self._openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1500,
                response_format={"type": "json_object"},
            )

            raw = json.loads(response.choices[0].message.content)
            gaps = []
            if isinstance(raw, list):
                gaps = raw
            elif isinstance(raw, dict):
                for k in ("gaps", "features", "proposals", "items"):
                    if k in raw and isinstance(raw[k], list):
                        gaps = raw[k]
                        break

            # Filter minimum occurrences
            return [g for g in gaps if g.get("occurrences", 0) >= 3]

        except Exception as e:
            logger.warning(f"FeatureSpawnAgent: GPT identification failed: {e}")
            return []

    async def _get_already_proposed_topics(self) -> List[str]:
        """Get list of topics already proposed to avoid duplicates."""
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            raw = await r.get("ora:feature_proposals")
            if not raw:
                return []
            data = json.loads(raw)
            return [p.get("title", "") for p in data.get("proposals", [])]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # GitHub Issues
    # ------------------------------------------------------------------

    async def _create_feature_issue(self, gap: Dict[str, Any]) -> Optional[str]:
        """Create a GitHub Issue with full feature spec."""
        title = gap.get("title", "Feature Proposal")
        user_story = gap.get("user_story", "")
        acceptance_criteria = gap.get("acceptance_criteria", [])
        implementation_sketch = gap.get("implementation_sketch", "")
        priority = gap.get("priority", "medium")
        occurrences = gap.get("occurrences", 0)
        quotes = gap.get("representative_quotes", [])
        target_repo = gap.get("target_repo", "connectome-web")

        # Validate repo name
        if "backend" in target_repo.lower():
            repo = "AvielCarlos/connectome-backend"
        else:
            repo = "AvielCarlos/connectome-web"

        ac_list = "\n".join(f"- [ ] {c}" for c in acceptance_criteria)
        quotes_text = "\n".join(f'> "{q}"' for q in quotes[:3]) if quotes else "_No direct quotes_"

        body = f"""## 🤖 Ora Feature Proposal

**Generated by:** FeatureSpawnAgent  
**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d')}  
**Priority:** {priority.upper()}  
**Signal Strength:** ~{occurrences} user requests detected  

## User Story
{user_story}

## Acceptance Criteria
{ac_list}

## Implementation Sketch
{implementation_sketch}

## User Evidence
{quotes_text}

---
*Automatically generated by Ora's FeatureSpawnAgent. Detected {occurrences} users requesting this feature.*
"""

        # Try gh CLI first
        try:
            result = subprocess.run(
                [
                    "gh", "issue", "create",
                    "--repo", repo,
                    "--title", title[:100],
                    "--body", body,
                    "--label", "ora-proposal",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                url = result.stdout.strip()
                logger.info(f"FeatureSpawnAgent: created issue {url}")
                return url
            else:
                logger.warning(f"FeatureSpawnAgent: gh issue create failed: {result.stderr}")
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"FeatureSpawnAgent: gh CLI error: {e}")

        # Fallback: GitHub API
        if self._github_token:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.post(
                        f"https://api.github.com/repos/{repo}/issues",
                        headers={
                            "Authorization": f"token {self._github_token}",
                            "Accept": "application/vnd.github.v3+json",
                        },
                        json={"title": title[:100], "body": body, "labels": ["ora-proposal"]},
                    )
                    if resp.status_code == 201:
                        url = resp.json().get("html_url", "")
                        logger.info(f"FeatureSpawnAgent: created issue via API {url}")
                        return url
            except Exception as e:
                logger.warning(f"FeatureSpawnAgent: GitHub API failed: {e}")

        return None

    # ------------------------------------------------------------------
    # Redis store
    # ------------------------------------------------------------------

    async def _store_proposals(self, proposals: List[Dict]) -> None:
        """Store proposals in Redis: ora:feature_proposals (24h TTL)."""
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            await r.set(
                "ora:feature_proposals",
                json.dumps({
                    "proposals": proposals,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }),
                ex=24 * 3600,
            )
        except Exception as e:
            logger.warning(f"FeatureSpawnAgent: Redis store failed: {e}")

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
            logger.debug(f"FeatureSpawnAgent: Telegram send failed: {e}")
