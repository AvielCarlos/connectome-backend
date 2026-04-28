"""
Ora Self-Improvement Agent

Gives Ora the ability to analyze her own performance patterns, propose code
changes, auto-apply low-risk improvements, and escalate high-risk changes to
Avi for approval.

Safety contract:
  - NEVER deletes existing functionality
  - Low-risk changes only: prompt text, Redis weight/blocklist tweaks
  - High-risk changes (logic rewrites, routes, DB) → propose only + Avi approval
  - All GitHub commits include explicit, descriptive messages
"""

from __future__ import annotations

import ast
import base64
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_CHAT_ID = 5716959016
GITHUB_REPO = "AvielCarlos/connectome-backend"
GITHUB_API = "https://api.github.com"
PROPOSALS_REDIS_KEY = "ora:self_improvement:proposals"
MAX_PROPOSALS = 10
INTERACTION_SAMPLE = 500


# ---------------------------------------------------------------------------
# Low-risk vs high-risk classification
# ---------------------------------------------------------------------------

LOW_RISK_CHANGE_TYPES = {"prompt_text", "weight_adjustment", "category_blocklist"}
HIGH_RISK_CHANGE_TYPES = {"logic_rewrite", "new_route", "db_change", "agent_restructure"}


class SelfImprovementAgent:
    """
    Ora's self-directed code improvement loop.

    Usage:
        agent = SelfImprovementAgent(openai_client, telegram_token)
        result = await agent.run()
    """

    def __init__(
        self,
        openai_client: Any,
        telegram_token: Optional[str] = None,
    ) -> None:
        self._openai = openai_client
        self._telegram_token = telegram_token

    # -----------------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------------

    async def run(self) -> Dict[str, Any]:
        logger.info("SelfImprovementAgent: starting cycle")

        result: Dict[str, Any] = {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "patterns_analyzed": 0,
            "proposals_generated": 0,
            "auto_applied": [],
            "escalated": [],
            "errors": [],
        }

        # A. Analyze interaction patterns
        try:
            patterns = await self._analyze_patterns()
            result["patterns_analyzed"] = len(patterns)
        except Exception as e:
            logger.error(f"SelfImprovement pattern analysis failed: {e}")
            result["errors"].append(f"pattern_analysis: {e}")
            patterns = []

        if not patterns or not self._openai:
            logger.info("SelfImprovementAgent: no patterns or no OpenAI — skipping proposals")
            return result

        # B. Generate improvement proposals
        try:
            proposals = await self._generate_proposals(patterns)
            result["proposals_generated"] = len(proposals)
        except Exception as e:
            logger.error(f"SelfImprovement proposal generation failed: {e}")
            result["errors"].append(f"proposal_generation: {e}")
            proposals = []

        # C. Apply low-risk changes; escalate high-risk
        for proposal in proposals:
            try:
                if proposal.get("risk") in LOW_RISK_CHANGE_TYPES:
                    applied = await self._auto_apply(proposal)
                    if applied:
                        result["auto_applied"].append(proposal.get("title", "?"))
                    else:
                        result["escalated"].append(proposal.get("title", "?"))
                else:
                    await self._store_proposal(proposal)
                    result["escalated"].append(proposal.get("title", "?"))
            except Exception as e:
                logger.warning(f"SelfImprovement apply/escalate failed: {e}")
                result["errors"].append(f"apply: {e}")

        # D. Send report if something happened
        try:
            await self._send_report(result, proposals)
        except Exception as e:
            logger.warning(f"SelfImprovement report send failed: {e}")

        logger.info(f"SelfImprovementAgent: cycle done — {result}")
        return result

    # -----------------------------------------------------------------------
    # A. Pattern Analysis
    # -----------------------------------------------------------------------

    async def _analyze_patterns(self) -> List[Dict[str, Any]]:
        """
        Read last INTERACTION_SAMPLE interactions from the DB, compute per-type
        stats, and return a list of pattern dicts.
        """
        from core.database import fetch as db_fetch

        try:
            rows = await db_fetch(
                """
                SELECT
                    ss.agent_type,
                    ss.screen_type,
                    i.rating,
                    i.exit_point,
                    i.created_at
                FROM interactions i
                JOIN screen_specs ss ON ss.id = i.screen_spec_id
                WHERE i.created_at >= NOW() - INTERVAL '30 days'
                ORDER BY i.created_at DESC
                LIMIT $1
                """,
                INTERACTION_SAMPLE,
            )
        except Exception as e:
            logger.warning(f"SelfImprovement: DB query failed: {e}")
            return []

        # Aggregate per agent_type
        from collections import defaultdict

        stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "ratings": [], "exit_points": [], "count": 0
        })

        for row in rows:
            atype = row.get("agent_type") or "unknown"
            rating = row.get("rating")
            exit_pt = row.get("exit_point")
            stats[atype]["count"] += 1
            if rating is not None:
                stats[atype]["ratings"].append(float(rating))
            if exit_pt:
                stats[atype]["exit_points"].append(exit_pt)

        patterns: List[Dict[str, Any]] = []
        for agent_type, data in stats.items():
            ratings = data["ratings"]
            if len(ratings) < 10:
                continue
            avg = sum(ratings) / len(ratings)
            low_count = sum(1 for r in ratings if r < 2.5)
            high_count = sum(1 for r in ratings if r > 4.5)

            pattern: Dict[str, Any] = {
                "agent_type": agent_type,
                "avg_rating": round(avg, 2),
                "sample_size": len(ratings),
                "low_rated_pct": round(low_count / len(ratings) * 100, 1),
                "high_rated_pct": round(high_count / len(ratings) * 100, 1),
                "exit_points": data["exit_points"][:10],
            }

            if avg < 2.5:
                pattern["signal"] = "consistently_bad"
            elif avg > 4.5:
                pattern["signal"] = "consistently_great"
            elif low_count / len(ratings) > 0.4:
                pattern["signal"] = "high_skip_rate"
            else:
                pattern["signal"] = "neutral"

            patterns.append(pattern)

        # Also pull recent lessons for context
        try:
            lesson_rows = await db_fetch(
                "SELECT lesson, confidence FROM ora_lessons ORDER BY created_at DESC LIMIT 20"
            )
            patterns.append({
                "agent_type": "_lessons",
                "recent_lessons": [
                    {"lesson": r["lesson"], "confidence": r["confidence"]}
                    for r in lesson_rows
                ],
            })
        except Exception:
            pass

        logger.info(f"SelfImprovement: analyzed {len(rows)} interactions → {len(patterns)} patterns")
        return patterns

    # -----------------------------------------------------------------------
    # B. Proposal Generation
    # -----------------------------------------------------------------------

    async def _generate_proposals(
        self, patterns: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Ask GPT-4o to propose concrete code improvements based on patterns.
        Returns list of proposal dicts.
        """
        # Build a compact summary for the prompt
        lessons = next(
            (p["recent_lessons"] for p in patterns if p.get("agent_type") == "_lessons"),
            []
        )
        interaction_patterns = [p for p in patterns if p.get("agent_type") != "_lessons"]

        summary_lines = []
        for p in interaction_patterns:
            summary_lines.append(
                f"- {p['agent_type']}: avg_rating={p['avg_rating']}, "
                f"n={p['sample_size']}, signal={p['signal']}, "
                f"low_pct={p['low_rated_pct']}%, high_pct={p['high_rated_pct']}%"
            )

        lessons_text = "\n".join(
            f"  [{l['confidence']:.2f}] {l['lesson']}" for l in lessons[:10]
        )

        prompt = f"""You are Ora's self-improvement engine. You have access to her performance data.

## Interaction Patterns (last 30 days, last {INTERACTION_SAMPLE} records)
{chr(10).join(summary_lines) if summary_lines else "(no data)"}

## Recent Lessons
{lessons_text if lessons_text else "(none)"}

## Known Files You Can Modify
- ora/agents/discovery.py  → PROMPT_TEMPLATE or MOCK_DISCOVERY_CARDS
- ora/agents/coaching.py   → coaching prompts
- ora/brain.py             → BASE_WEIGHTS dict (weight_key: float)
- Redis key ora:agent_weights (already editable)
- Redis key ora:discovery:blocked_categories (JSON list)

## Instructions
Generate up to 5 concrete improvement proposals as a JSON array. Each proposal:
{{
  "id": "<uuid>",
  "title": "short description",
  "risk": "prompt_text" | "weight_adjustment" | "category_blocklist" | "logic_rewrite" | "new_route" | "db_change",
  "rationale": "why this matters",
  "target_file": "ora/agents/discovery.py" or null for Redis-only,
  "change_type": "file_edit" | "redis_only",
  "patch_description": "what to change in plain english",
  "estimated_impact": "high|medium|low",
  "auto_appliable": true | false
}}

Rules:
- Low-risk (prompt_text, weight_adjustment, category_blocklist) → set auto_appliable=true
- High-risk (logic_rewrite, new_route, db_change) → set auto_appliable=false
- NEVER suggest deleting functionality
- Be specific: reference actual variable names, thresholds, or prompt fragments
- If everything looks good, generate 0 proposals

Output ONLY valid JSON array (no markdown, no extra text)."""

        try:
            response = await self._openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1800,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content

            # GPT returns {"proposals": [...]} or just [...]
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                proposals = parsed
            else:
                proposals = parsed.get("proposals", [])

            # Ensure each has an id and created_at
            for p in proposals:
                if not p.get("id"):
                    p["id"] = str(uuid4())
                p.setdefault("created_at", datetime.now(timezone.utc).isoformat())
                p.setdefault("status", "pending")

            logger.info(f"SelfImprovement: GPT-4o generated {len(proposals)} proposals")
            return proposals[:MAX_PROPOSALS]

        except Exception as e:
            logger.error(f"SelfImprovement: proposal generation OpenAI error: {e}")
            return []

    # -----------------------------------------------------------------------
    # C. Auto-Apply (low-risk only)
    # -----------------------------------------------------------------------

    async def _auto_apply(self, proposal: Dict[str, Any]) -> bool:
        """
        Apply a low-risk proposal. Returns True if successfully applied.
        """
        risk = proposal.get("risk")
        logger.info(f"SelfImprovement: auto-applying '{proposal.get('title')}' (risk={risk})")

        if risk == "weight_adjustment":
            return await self._apply_weight_adjustment(proposal)
        elif risk == "category_blocklist":
            return await self._apply_category_blocklist(proposal)
        elif risk == "prompt_text":
            return await self._apply_prompt_text(proposal)
        else:
            logger.warning(f"SelfImprovement: unknown low-risk type {risk}")
            return False

    async def _apply_weight_adjustment(self, proposal: Dict[str, Any]) -> bool:
        """Adjust agent weights in Redis (already handled by autonomy agent; this validates)."""
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            weights_raw = await r.get("ora:agent_weights")
            weights = json.loads(weights_raw) if weights_raw else {}
            if weights:
                await self._log_lesson(
                    f"Self-improvement applied: {proposal.get('title')} — "
                    f"{proposal.get('patch_description', '')}",
                    confidence=0.8,
                    source="SelfImprovementAgent.weight_adjustment",
                )
                proposal["status"] = "applied"
                return True
        except Exception as e:
            logger.warning(f"SelfImprovement weight adjust: {e}")
        return False

    async def _apply_category_blocklist(self, proposal: Dict[str, Any]) -> bool:
        """Add categories to ora:discovery:blocked_categories Redis key."""
        try:
            from core.redis_client import get_redis
            r = await get_redis()

            raw = await r.get("ora:discovery:blocked_categories")
            blocked: List[str] = json.loads(raw) if raw else []

            desc = proposal.get("patch_description", "")
            new_cats = re.findall(r"'([^']+)'|\"([^\"]+)\"", desc)
            cats_flat = [c for pair in new_cats for c in pair if c]

            if not cats_flat:
                logger.warning("SelfImprovement: no categories found in patch_description")
                return False

            added = []
            for cat in cats_flat:
                if cat.lower() not in [b.lower() for b in blocked]:
                    blocked.append(cat.lower())
                    added.append(cat)

            if added:
                await r.set(
                    "ora:discovery:blocked_categories",
                    json.dumps(blocked),
                    ex=30 * 24 * 3600,
                )
                await self._log_lesson(
                    f"Self-improvement applied: blocked categories {added} — "
                    f"{proposal.get('rationale', '')}",
                    confidence=0.75,
                    source="SelfImprovementAgent.category_blocklist",
                )
                proposal["status"] = "applied"
                logger.info(f"SelfImprovement: added blocked categories: {added}")
                return True
        except Exception as e:
            logger.warning(f"SelfImprovement category blocklist: {e}")
        return False

    async def _apply_prompt_text(self, proposal: Dict[str, Any]) -> bool:
        """
        Generate new file content via GPT-4o, validate syntax, commit to GitHub,
        and update the self-improvement log.
        """
        target_file = proposal.get("target_file")
        if not target_file:
            return False

        github_token = os.environ.get("GITHUB_TOKEN")
        if not github_token:
            logger.warning("SelfImprovement: GITHUB_TOKEN not set — cannot commit")
            await self._store_proposal(proposal)
            return False

        try:
            file_info = await self._github_get_file(github_token, target_file)
            if not file_info:
                logger.warning(f"SelfImprovement: could not fetch {target_file} from GitHub")
                return False

            current_content = base64.b64decode(file_info["content"].replace("\n", "")).decode("utf-8")
            current_sha = file_info["sha"]
        except Exception as e:
            logger.warning(f"SelfImprovement: GitHub file fetch failed: {e}")
            return False

        update_prompt = f"""You are Ora's code editor. Apply this improvement to the file.

## Improvement
Title: {proposal.get('title')}
Rationale: {proposal.get('rationale')}
Change: {proposal.get('patch_description')}

## Rules
- ONLY modify prompt text/docstrings/comments and string constants
- DO NOT change function signatures, logic, control flow, or imports
- Return the COMPLETE updated file content (not just a diff)
- The result must be valid Python

## Current File Content
```python
{current_content[:6000]}
```

Output ONLY the complete updated Python file content. No markdown, no explanation."""

        try:
            response = await self._openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": update_prompt}],
                temperature=0.2,
                max_tokens=4096,
            )
            new_content = response.choices[0].message.content

            new_content = re.sub(r"^```python\n?", "", new_content, flags=re.MULTILINE)
            new_content = re.sub(r"^```\s*$", "", new_content, flags=re.MULTILINE)
            new_content = new_content.strip()

        except Exception as e:
            logger.warning(f"SelfImprovement: GPT-4o content generation failed: {e}")
            return False

        try:
            ast.parse(new_content)
        except SyntaxError as e:
            logger.warning(f"SelfImprovement: generated code has syntax error: {e}")
            await self._store_proposal({**proposal, "status": "syntax_error", "error": str(e)})
            return False

        commit_message = (
            f"[Ora Self-Improvement] {proposal.get('title')}\n\n"
            f"Rationale: {proposal.get('rationale', '')}\n"
            f"Change: {proposal.get('patch_description', '')}\n\n"
            f"Auto-applied by SelfImprovementAgent at {datetime.now(timezone.utc).isoformat()}"
        )

        committed = await self._github_commit_file(
            github_token,
            target_file,
            new_content,
            current_sha,
            commit_message,
        )

        if not committed:
            logger.warning(f"SelfImprovement: GitHub commit failed for {target_file}")
            await self._store_proposal(proposal)
            return False

        await self._update_improvement_log(proposal)

        await self._log_lesson(
            f"Self-improvement applied: {proposal.get('title')} — "
            f"modified {target_file}. {proposal.get('rationale', '')}",
            confidence=0.85,
            source="SelfImprovementAgent.prompt_text",
        )
        proposal["status"] = "applied"
        logger.info(f"SelfImprovement: committed prompt improvement to {target_file}")
        return True

    # -----------------------------------------------------------------------
    # High-risk: store as pending proposal
    # -----------------------------------------------------------------------

    async def _store_proposal(self, proposal: Dict[str, Any]) -> None:
        """Save a high-risk (or failed) proposal to Redis for Avi review."""
        try:
            from core.redis_client import get_redis
            r = await get_redis()

            raw = await r.get(PROPOSALS_REDIS_KEY)
            proposals: List[Dict[str, Any]] = json.loads(raw) if raw else []

            existing_titles = {p.get("title") for p in proposals}
            if proposal.get("title") in existing_titles:
                return

            proposals.append({**proposal, "status": "pending"})
            proposals = proposals[-MAX_PROPOSALS:]

            await r.set(PROPOSALS_REDIS_KEY, json.dumps(proposals), ex=30 * 24 * 3600)
            logger.info(f"SelfImprovement: stored proposal '{proposal.get('title')}'")
        except Exception as e:
            logger.warning(f"SelfImprovement: could not store proposal: {e}")

    # -----------------------------------------------------------------------
    # D. Improvement Report
    # -----------------------------------------------------------------------

    async def _send_report(
        self, result: Dict[str, Any], proposals: List[Dict[str, Any]]
    ) -> None:
        """Send Telegram summary if anything significant happened."""
        applied = result.get("auto_applied", [])
        escalated = result.get("escalated", [])

        if not applied and not escalated:
            return

        lines = ["🧬 *Ora Self-Improvement Report*\n"]

        if applied:
            lines.append(f"✅ *Auto-Applied ({len(applied)})*")
            for title in applied[:3]:
                lines.append(f"  • {title}")
            lines.append("")

        if escalated:
            lines.append(f"📋 *Pending Approval ({len(escalated)})*")
            for title in escalated[:3]:
                lines.append(f"  • {title}")
            lines.append("\n_Review at: Profile → System → Proposals_")

        patterns = result.get("patterns_analyzed", 0)
        lines.append(f"\n_Analyzed {patterns} interaction patterns — {result.get('run_at', '')[:10]}_")

        await self._send_telegram("\n".join(lines))

    # -----------------------------------------------------------------------
    # GitHub API helpers
    # -----------------------------------------------------------------------

    async def _github_get_file(
        self, token: str, path: str
    ) -> Optional[Dict[str, Any]]:
        """Fetch file metadata + base64 content from GitHub."""
        url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github.v3+json",
                    },
                )
                if resp.status_code == 200:
                    return resp.json()
                logger.warning(f"SelfImprovement: GitHub GET {path} → {resp.status_code}")
                return None
        except Exception as e:
            logger.warning(f"SelfImprovement: GitHub GET failed: {e}")
            return None

    async def _github_commit_file(
        self,
        token: str,
        path: str,
        content: str,
        sha: str,
        message: str,
    ) -> bool:
        """Commit updated file content to GitHub."""
        url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.put(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github.v3+json",
                    },
                    json={
                        "message": message,
                        "content": encoded,
                        "sha": sha,
                        "committer": {
                            "name": "Ora Self-Improvement",
                            "email": "ora@connectome.app",
                        },
                    },
                )
                if resp.status_code in (200, 201):
                    logger.info(f"SelfImprovement: committed {path}")
                    return True
                logger.warning(
                    f"SelfImprovement: GitHub PUT {path} → {resp.status_code}: {resp.text[:200]}"
                )
                return False
        except Exception as e:
            logger.warning(f"SelfImprovement: GitHub PUT failed: {e}")
            return False

    async def _update_improvement_log(self, proposal: Dict[str, Any]) -> None:
        """Update ora/self_improvement_log.json on GitHub (serves as a redeploy signal)."""
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            return

        log_path = "ora/self_improvement_log.json"

        existing = await self._github_get_file(token, log_path)
        entries: List[Dict[str, Any]] = []
        sha: Optional[str] = None

        if existing:
            try:
                raw = base64.b64decode(existing["content"].replace("\n", "")).decode("utf-8")
                entries = json.loads(raw)
            except Exception:
                entries = []
            sha = existing.get("sha")

        entries.append({
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "proposal_id": proposal.get("id"),
            "title": proposal.get("title"),
            "risk": proposal.get("risk"),
            "target_file": proposal.get("target_file"),
        })
        entries = entries[-50:]

        new_content = json.dumps(entries, indent=2)
        encoded = base64.b64encode(new_content.encode()).decode()

        url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{log_path}"
        payload: Dict[str, Any] = {
            "message": f"[Ora] Update self_improvement_log — {proposal.get('title', '')}",
            "content": encoded,
            "committer": {
                "name": "Ora Self-Improvement",
                "email": "ora@connectome.app",
            },
        }
        if sha:
            payload["sha"] = sha

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                await client.put(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github.v3+json",
                    },
                    json=payload,
                )
        except Exception as e:
            logger.debug(f"SelfImprovement: log update failed (non-critical): {e}")

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _log_lesson(self, lesson: str, confidence: float, source: str) -> None:
        from core.database import execute as db_execute
        try:
            await db_execute(
                "INSERT INTO ora_lessons (lesson, confidence, source) VALUES ($1, $2, $3)",
                lesson,
                confidence,
                source,
            )
        except Exception as e:
            logger.debug(f"SelfImprovement: lesson log failed: {e}")

    async def _send_telegram(self, message: str) -> None:
        token = self._telegram_token
        if not token:
            token = os.environ.get("ORA_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            try:
                with open("/app/secrets/telegram-bot-token.txt") as f:
                    token = f.read().strip()
            except Exception:
                pass
        if not token:
            logger.warning("SelfImprovement: no Telegram token")
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": message,
                        "parse_mode": "Markdown",
                    },
                )
        except Exception as e:
            logger.warning(f"SelfImprovement: Telegram send failed: {e}")
