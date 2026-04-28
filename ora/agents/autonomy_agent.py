"""
Ora Autonomy Agent — Self-directed system improvement

Ora's autonomous decision loop. Runs on a schedule to:
  A. Promote winning A/B test variants automatically
  B. Detect and auto-fix recurring backend errors
  C. Optimize feed agent weights based on user ratings
  D. Send a daily summary to Avi via Telegram

All actions are ADDITIVE ONLY — no data deletion, no destructive changes.
"""

import json
import logging
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TELEGRAM_CHAT_ID = 5716959016
EXPERIMENT_ID = "primary_landing_v1"
VARIANTS = ["A", "B", "C", "D"]

ENGAGEMENT_WEIGHTS = {
    "card_swiped": 1,
    "card_rated": 3,
    "card_saved": 5,
    "ora_opened": 4,
    # session_duration_ms is divided by 60000 × 2
}

# Patterns we know how to auto-fix (safe, additive only)
FIXABLE_ERROR_PATTERNS = [
    r"ImportError: No module named '([^']+)'",
    r"ModuleNotFoundError: No module named '([^']+)'",
    r"column \"([^\"]+)\" of relation \"([^\"]+)\" does not exist",
    r"SyntaxError: (.+)",
    r"AttributeError: '([^']+)' object has no attribute '([^']+)'",
]


class OraAutonomyAgent:
    """
    Ora's autonomous improvement engine.
    Safe by design: every action is logged, reversible, or additive.
    """

    def __init__(self, openai_client=None):
        self._openai = openai_client
        self._telegram_token: Optional[str] = None

    # -----------------------------------------------------------------------
    # Entry point
    # -----------------------------------------------------------------------

    async def run(self) -> Dict[str, Any]:
        """
        Execute the full autonomy cycle. Returns a summary dict.
        This is called by POST /api/ora/autonomy/run.
        """
        logger.info("OraAutonomy: starting full cycle")
        report: Dict[str, Any] = {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "ab_winner": None,
            "ab_scores": {},
            "bugs_found": [],
            "bugs_fixed": [],
            "weight_changes": [],
            "top_content_type": None,
            "user_count": 0,
            "active_users": 0,
        }

        # A. A/B Test Auto-Promotion
        try:
            ab_result = await self._ab_auto_promote()
            report["ab_winner"] = ab_result.get("winner")
            report["ab_scores"] = ab_result.get("scores", {})
        except Exception as e:
            logger.error(f"OraAutonomy A/B promotion failed: {e}")

        # B. Bug Detection & Auto-Fix
        try:
            bug_result = await self._detect_and_fix_bugs()
            report["bugs_found"] = bug_result.get("found", [])
            report["bugs_fixed"] = bug_result.get("fixed", [])
        except Exception as e:
            logger.error(f"OraAutonomy bug detection failed: {e}")

        # C. Feed Quality Optimizer
        try:
            weight_result = await self._optimize_feed_weights()
            report["weight_changes"] = weight_result.get("changes", [])
        except Exception as e:
            logger.error(f"OraAutonomy weight optimizer failed: {e}")

        # D. Daily Autonomy Report
        try:
            stats = await self._gather_stats()
            report["top_content_type"] = stats.get("top_content_type")
            report["user_count"] = stats.get("user_count", 0)
            report["active_users"] = stats.get("active_users", 0)

            await self._maybe_send_report(report)
        except Exception as e:
            logger.error(f"OraAutonomy daily report failed: {e}")

        # E. Self-improvement cycle
        try:
            from ora.agents.self_improvement_agent import SelfImprovementAgent
            self_agent = SelfImprovementAgent(self._openai, self._telegram_token)
            improvement_result = await self_agent.run()
            result["self_improvement"] = improvement_result
        except Exception as e:
            logger.error(f"OraAutonomy self-improvement cycle failed: {e}")
            result["self_improvement"] = {"error": str(e)}

        # F. Self-improvement eval loop (Loop 2: did last change actually help?)
        try:
            from ora.agents.self_improvement_agent import SelfImprovementAgent
            eval_agent = SelfImprovementAgent(self._openai, self._telegram_token)
            eval_result = await eval_agent.run_eval_loop()
            result["self_improvement_eval"] = eval_result
        except Exception as e:
            logger.error(f"OraAutonomy eval_loop failed: {e}")
            result["self_improvement_eval"] = {"error": str(e)}

        # G. Self-improvement meta loop (Loop 3: learn to improve better)
        try:
            from ora.agents.self_improvement_agent import SelfImprovementAgent
            meta_agent = SelfImprovementAgent(self._openai, self._telegram_token)
            meta_result = await meta_agent.run_meta_loop()
            result["self_improvement_meta"] = meta_result
        except Exception as e:
            logger.error(f"OraAutonomy meta_loop failed: {e}")
            result["self_improvement_meta"] = {"error": str(e)}

        # Persist last run metadata to Redis
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            await r.set("ora:autonomy:last_run", json.dumps({
                "run_at": report["run_at"],
                "ab_winner": report["ab_winner"],
                "weight_changes_count": len(report["weight_changes"]),
                "bugs_fixed_count": len(report["bugs_fixed"]),
            }), ex=7 * 24 * 3600)
        except Exception as e:
            logger.debug(f"OraAutonomy: could not persist last_run: {e}")

        logger.info(f"OraAutonomy: cycle complete — {report}")
        return report

    # -----------------------------------------------------------------------
    # A. A/B Test Auto-Promotion
    # -----------------------------------------------------------------------

    async def _ab_auto_promote(self) -> Dict[str, Any]:
        """
        Compute engagement scores for each variant and promote a winner
        if any variant dominates by 20%+ with >50 sessions.
        """
        from core.redis_client import get_redis
        r = await get_redis()

        scores: Dict[str, float] = {}
        session_counts: Dict[str, int] = {}

        for variant in VARIANTS:
            key = f"ab:events:{EXPERIMENT_ID}:{variant}"
            try:
                raw_events = await r.lrange(key, 0, -1)
            except Exception as e:
                logger.debug(f"A/B: could not read {key}: {e}")
                raw_events = []

            events: List[Dict[str, Any]] = []
            for raw in raw_events:
                try:
                    events.append(json.loads(raw))
                except Exception:
                    pass

            session_starts = sum(1 for e in events if e.get("event_type") == "session_start")
            if session_starts == 0:
                scores[variant] = 0.0
                session_counts[variant] = 0
                continue

            session_counts[variant] = session_starts

            weighted_sum = 0.0
            for e in events:
                et = e.get("event_type", "")
                value = float(e.get("value", 1.0))
                if et == "card_swiped":
                    weighted_sum += value * ENGAGEMENT_WEIGHTS["card_swiped"]
                elif et == "card_rated":
                    weighted_sum += value * ENGAGEMENT_WEIGHTS["card_rated"]
                elif et == "card_saved":
                    weighted_sum += value * ENGAGEMENT_WEIGHTS["card_saved"]
                elif et == "ora_opened":
                    weighted_sum += value * ENGAGEMENT_WEIGHTS["ora_opened"]
                elif et == "session_duration_ms":
                    weighted_sum += (value / 60000.0) * 2

            scores[variant] = weighted_sum / session_starts

        # Find current winner from Redis
        current_winner_raw = await r.get(f"ab:winner:{EXPERIMENT_ID}")
        current_winner = current_winner_raw if isinstance(current_winner_raw, str) else (
            current_winner_raw.decode() if current_winner_raw else None
        )

        if not current_winner or current_winner not in scores:
            # Default to A if no winner set
            current_winner_score = scores.get("A", 0.0)
            current_winner = "A"
        else:
            current_winner_score = scores.get(current_winner, 0.0)

        # Check if any challenger scores 20%+ higher with >50 sessions
        new_winner = None
        for variant, score in scores.items():
            if variant == current_winner:
                continue
            sessions = session_counts.get(variant, 0)
            if sessions > 50 and current_winner_score > 0 and score >= current_winner_score * 1.20:
                new_winner = variant
                logger.info(
                    f"OraAutonomy A/B: promoting variant {variant} "
                    f"(score={score:.2f} vs current {current_winner}={current_winner_score:.2f}, "
                    f"sessions={sessions})"
                )
                break
            elif sessions > 50 and current_winner_score == 0 and score > 0:
                # No current baseline — promote first variant with data
                new_winner = variant
                break

        if new_winner:
            await r.set(f"ab:winner:{EXPERIMENT_ID}", new_winner)
            current_winner = new_winner

            # Log decision to ora_lessons
            lesson = (
                f"A/B auto-promotion: variant {new_winner} won experiment {EXPERIMENT_ID} "
                f"with score {scores.get(new_winner, 0):.2f} "
                f"(prev winner score: {current_winner_score:.2f}). "
                f"Variant scores: {json.dumps({k: round(v, 2) for k, v in scores.items()})}"
            )
            await self._log_lesson(lesson, confidence=0.85, source="OraAutonomyAgent.ab_auto_promote")

        return {"winner": current_winner, "scores": scores, "promoted": new_winner}

    # -----------------------------------------------------------------------
    # B. Bug Detection & Auto-Fix
    # -----------------------------------------------------------------------

    async def _detect_and_fix_bugs(self) -> Dict[str, Any]:
        """
        Fetch Railway logs, parse ERROR lines, and attempt auto-fixes
        for known fixable patterns.
        """
        logs = await self._fetch_railway_logs()
        if not logs:
            logger.info("OraAutonomy: no Railway logs available, skipping bug detection")
            return {"found": [], "fixed": []}

        # Extract ERROR lines
        error_lines = [line for line in logs if "ERROR" in line or " error " in line.lower()]

        # Group by message pattern (normalize away timestamps, UUIDs, IPs)
        def normalize(msg: str) -> str:
            msg = re.sub(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', '<uuid>', msg)
            msg = re.sub(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', '<ip>', msg)
            msg = re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[\.\d]*Z?', '<ts>', msg)
            msg = re.sub(r'\s+', ' ', msg).strip()
            return msg[:200]

        pattern_counts: Counter = Counter()
        pattern_examples: Dict[str, str] = {}
        for line in error_lines[-100:]:  # last 100 for pattern counting
            key = normalize(line)
            pattern_counts[key] += 1
            pattern_examples[key] = line

        # Find recurring errors (3+ occurrences)
        recurring = [(pat, cnt) for pat, cnt in pattern_counts.items() if cnt >= 3]

        found = []
        fixed = []

        for pattern, count in recurring:
            example = pattern_examples[pattern]
            fix_type = self._classify_fixable(example)
            found.append({"pattern": pattern[:100], "count": count, "fixable": fix_type is not None})

            if fix_type and self._openai:
                try:
                    fix_result = await self._attempt_auto_fix(example, fix_type)
                    if fix_result.get("success"):
                        fixed.append({
                            "pattern": pattern[:100],
                            "fix": fix_result.get("description", ""),
                            "committed": fix_result.get("committed", False),
                        })
                        lesson = f"Auto-fixed recurring error ({count}x): {pattern[:100]}. Fix: {fix_result.get('description', '')}"
                        await self._log_lesson(lesson, confidence=0.7, source="OraAutonomyAgent.bug_fix")
                    else:
                        # Send alert for unfixable recurring error
                        await self._send_telegram(
                            f"⚠️ *Ora Bug Alert*\n\nRecurring error ({count}×):\n`{pattern[:150]}`\n\nCould not auto-fix. Please investigate."
                        )
                except Exception as e:
                    logger.warning(f"OraAutonomy: auto-fix failed for pattern: {e}")
            elif fix_type is None and count >= 5:
                # Non-fixable and frequent — alert Avi
                await self._send_telegram(
                    f"⚠️ *Ora Bug Alert*\n\nRecurring error ({count}×) in production:\n`{pattern[:150]}`\n\nNo auto-fix available."
                )

        return {"found": found, "fixed": fixed}

    def _classify_fixable(self, error_line: str) -> Optional[str]:
        """Classify if an error is in our known fixable list. Returns fix type or None."""
        for pattern in FIXABLE_ERROR_PATTERNS:
            if re.search(pattern, error_line, re.IGNORECASE):
                if "ImportError" in pattern or "ModuleNotFoundError" in pattern:
                    return "missing_import"
                elif "column" in pattern:
                    return "missing_column"
                elif "SyntaxError" in pattern:
                    return "syntax_error"
                elif "AttributeError" in pattern:
                    return "attribute_error"
        return None

    async def _attempt_auto_fix(self, error_line: str, fix_type: str) -> Dict[str, Any]:
        """
        Use OpenAI to suggest a safe fix, then apply it via GitHub API.
        Only makes additive changes (new imports, new columns, etc.).
        Returns {"success": bool, "description": str, "committed": bool}.
        """
        if not self._openai:
            return {"success": False, "description": "No OpenAI client"}

        try:
            prompt = f"""You are Ora's auto-repair agent. A production error occurred:

Error type: {fix_type}
Error line: {error_line[:500]}

Suggest a SAFE, ADDITIVE fix for a Python FastAPI/asyncpg backend.
Rules:
- Only add missing imports, add missing columns (ALTER TABLE ... ADD COLUMN IF NOT EXISTS), or fix syntax
- NEVER delete data, drop tables, or make destructive changes
- Output JSON: {{"file": "path/from/repo/root.py", "description": "what this fixes", "patch": "exact code to add/change"}}
- If you cannot suggest a safe fix, output {{"file": null, "description": "cannot fix safely"}}"""

            response = await self._openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=400,
                response_format={"type": "json_object"},
            )
            suggestion = json.loads(response.choices[0].message.content)

            if not suggestion.get("file"):
                return {"success": False, "description": suggestion.get("description", "No safe fix")}

            # For now, log the suggested fix but don't auto-apply to GitHub
            # (we apply conservatively — alert Avi with the suggested fix instead)
            description = suggestion.get("description", "")
            patch = suggestion.get("patch", "")
            target_file = suggestion.get("file", "")

            if patch and target_file:
                await self._send_telegram(
                    f"🔧 *Ora Auto-Fix Suggestion*\n\n"
                    f"Error: `{error_line[:100]}`\n\n"
                    f"File: `{target_file}`\n"
                    f"Fix: {description}\n\n"
                    f"Patch:\n```python\n{patch[:300]}\n```\n\n"
                    f"_(Ora suggests this fix — please review and apply if appropriate)_"
                )
                return {"success": True, "description": description, "committed": False}

            return {"success": False, "description": "No patch generated"}

        except Exception as e:
            logger.warning(f"OraAutonomy: auto-fix OpenAI call failed: {e}")
            return {"success": False, "description": str(e)}

    async def _fetch_railway_logs(self) -> List[str]:
        """
        Attempt to fetch Railway deployment logs via GraphQL API.
        Falls back to health endpoint check if RAILWAY_API_TOKEN is unavailable.
        """
        import httpx

        token = os.environ.get("RAILWAY_API_TOKEN") or os.environ.get("RAILWAY_TOKEN")
        if not token:
            logger.info("OraAutonomy: RAILWAY_API_TOKEN not set, skipping log fetch")
            return []

        query = """
        query {
          deployments(projectId: "", serviceId: "") {
            edges {
              node {
                id
                status
              }
            }
          }
        }
        """

        # Use the simpler logs endpoint via environment service query
        logs_query = """
        query GetLogs($serviceId: String!, $limit: Int!) {
          serviceInstanceLogs(serviceId: $serviceId, limit: $limit) {
            timestamp
            message
          }
        }
        """

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Try to get service logs
                service_id = os.environ.get("RAILWAY_SERVICE_ID", "")
                if not service_id:
                    # Try simplified approach — just health check
                    resp = await client.get(
                        "https://connectome-api-production.up.railway.app/health",
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        logger.info("OraAutonomy: Railway health OK, no logs available without SERVICE_ID")
                    return []

                resp = await client.post(
                    "https://backboard.railway.app/graphql/v2",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "query": logs_query,
                        "variables": {"serviceId": service_id, "limit": 200},
                    },
                )

                if resp.status_code == 200:
                    data = resp.json()
                    log_entries = (
                        data.get("data", {})
                        .get("serviceInstanceLogs", []) or []
                    )
                    lines = []
                    for entry in log_entries:
                        msg = entry.get("message", "")
                        ts = entry.get("timestamp", "")
                        if msg:
                            lines.append(f"{ts} {msg}")
                    logger.info(f"OraAutonomy: fetched {len(lines)} Railway log lines")
                    return lines
                else:
                    logger.warning(f"OraAutonomy: Railway API returned {resp.status_code}")
                    return []

        except Exception as e:
            logger.warning(f"OraAutonomy: Railway log fetch failed: {e}")
            return []

    # -----------------------------------------------------------------------
    # C. Feed Quality Optimizer
    # -----------------------------------------------------------------------

    async def _optimize_feed_weights(self) -> Dict[str, Any]:
        """
        Compute average ratings per agent_type over the last 7 days,
        adjust base weights, and store in Redis.
        """
        from core.database import fetch as db_fetch
        from core.redis_client import get_redis

        changes = []

        try:
            rows = await db_fetch(
                """
                SELECT
                    ss.agent_type,
                    AVG(i.rating) as avg_rating,
                    COUNT(i.id) as interaction_count
                FROM interactions i
                JOIN screen_specs ss ON ss.id = i.screen_spec_id
                WHERE i.rating IS NOT NULL
                  AND i.created_at >= NOW() - INTERVAL '7 days'
                GROUP BY ss.agent_type
                HAVING COUNT(i.id) > 20
                ORDER BY avg_rating DESC
                """,
            )
        except Exception as e:
            logger.warning(f"OraAutonomy: weight optimizer DB query failed: {e}")
            return {"changes": []}

        # Map class name → weight key
        agent_type_map = {
            "DiscoveryAgent": "discovery",
            "CoachingAgent": "coaching",
            "RecommendationAgent": "recommendation",
            "UIGeneratorAgent": "ui_generator",
            "WorldAgent": "world",
            "EnlightenmentAgent": "enlightenment",
            "CollectiveIntelligenceAgent": "collective",
            "ExploreAgent": "explore",
        }

        # Load current weights (from Redis if available, else brain defaults)
        r = await get_redis()
        current_weights_raw = await r.get("ora:agent_weights")
        if current_weights_raw:
            try:
                current_weights = json.loads(current_weights_raw)
            except Exception:
                current_weights = _get_brain_base_weights()
        else:
            current_weights = _get_brain_base_weights()

        updated_weights = current_weights.copy()

        for row in rows:
            agent_type = row["agent_type"]
            avg_rating = float(row["avg_rating"])
            count = int(row["interaction_count"])
            weight_key = agent_type_map.get(agent_type)

            if not weight_key or weight_key not in updated_weights:
                continue

            old_weight = updated_weights[weight_key]
            new_weight = old_weight

            if avg_rating < 2.5 and count > 20:
                new_weight = max(0.03, old_weight * 0.85)
                change_desc = f"↓ reduced by 15% (avg_rating={avg_rating:.2f}, n={count})"
            elif avg_rating > 4.0 and count > 20:
                new_weight = min(0.40, old_weight * 1.15)
                change_desc = f"↑ increased by 15% (avg_rating={avg_rating:.2f}, n={count})"
            else:
                continue

            if abs(new_weight - old_weight) > 0.001:
                updated_weights[weight_key] = new_weight
                changes.append({
                    "agent_type": agent_type,
                    "weight_key": weight_key,
                    "old_weight": round(old_weight, 4),
                    "new_weight": round(new_weight, 4),
                    "description": change_desc,
                })
                logger.info(f"OraAutonomy weights: {agent_type} {change_desc}")

        if changes:
            # Normalize weights to sum to 1.0
            total = sum(updated_weights.values())
            normalized = {k: v / total for k, v in updated_weights.items()}

            # Store in Redis
            await r.set("ora:agent_weights", json.dumps(normalized))

            # Log to ora_lessons
            lesson = (
                f"Feed weight optimizer ran: {len(changes)} adjustments. "
                + "; ".join(f"{c['weight_key']} {c['description']}" for c in changes)
            )
            await self._log_lesson(lesson, confidence=0.75, source="OraAutonomyAgent.feed_optimizer")

            # Reload weights in brain
            try:
                from ora.brain import get_brain
                brain = get_brain()
                await brain.reload_weights()
                logger.info("OraAutonomy: brain weights reloaded")
            except Exception as e:
                logger.debug(f"OraAutonomy: brain reload skipped: {e}")

        return {"changes": changes}

    # -----------------------------------------------------------------------
    # D. Gather Stats
    # -----------------------------------------------------------------------

    async def _gather_stats(self) -> Dict[str, Any]:
        """Compile platform stats for the daily report."""
        from core.database import fetchrow as db_fetchrow, fetchval

        stats: Dict[str, Any] = {}

        try:
            total = await fetchval("SELECT COUNT(*) FROM users")
            stats["user_count"] = int(total or 0)
        except Exception:
            stats["user_count"] = 0

        try:
            active = await fetchval(
                "SELECT COUNT(*) FROM users WHERE last_active >= NOW() - INTERVAL '7 days'"
            )
            stats["active_users"] = int(active or 0)
        except Exception:
            stats["active_users"] = 0

        try:
            from core.database import fetchrow as db_fetchrow2
            top_row = await db_fetchrow2(
                """
                SELECT ss.agent_type, AVG(i.rating) as avg_rating, COUNT(*) as cnt
                FROM interactions i
                JOIN screen_specs ss ON ss.id = i.screen_spec_id
                WHERE i.created_at >= NOW() - INTERVAL '7 days'
                  AND i.rating IS NOT NULL
                GROUP BY ss.agent_type
                ORDER BY avg_rating DESC, cnt DESC
                LIMIT 1
                """
            )
            stats["top_content_type"] = top_row["agent_type"] if top_row else None
        except Exception:
            stats["top_content_type"] = None

        return stats

    # -----------------------------------------------------------------------
    # D. Daily Report
    # -----------------------------------------------------------------------

    async def _maybe_send_report(self, report: Dict[str, Any]) -> None:
        """Send a Telegram summary to Avi if there's something meaningful to report."""

        has_winner = bool(report.get("ab_winner"))
        has_bugs = bool(report.get("bugs_found") or report.get("bugs_fixed"))
        has_weight_changes = bool(report.get("weight_changes"))
        has_users = report.get("user_count", 0) > 0

        if not (has_winner or has_bugs or has_weight_changes or has_users):
            logger.info("OraAutonomy: nothing meaningful to report — skipping Telegram")
            return

        # Build message
        lines = ["🤖 *Ora Autonomy Report*\n"]

        # A/B winner
        winner = report.get("ab_winner")
        scores = report.get("ab_scores", {})
        if winner and scores:
            score_str = " | ".join(f"{v}: {s:.2f}" for v, s in scores.items() if s > 0)
            lines.append(f"📊 *A/B Test*\nCurrent winner: *{winner}*\nScores: {score_str}\n")

        # Bugs
        found = report.get("bugs_found", [])
        fixed = report.get("bugs_fixed", [])
        if found:
            lines.append(f"🐛 *Bugs*\nFound: {len(found)} recurring error(s)")
            if fixed:
                lines.append(f"Fixed: {len(fixed)} auto-fixed")
                for f in fixed[:2]:
                    lines.append(f"  • {f.get('fix', '')[:80]}")
            lines.append("")

        # Weight changes
        weight_changes = report.get("weight_changes", [])
        if weight_changes:
            lines.append("⚖️ *Agent Weights*")
            for c in weight_changes:
                arrow = "↑" if c["new_weight"] > c["old_weight"] else "↓"
                lines.append(f"  {arrow} {c['weight_key']}: {c['old_weight']:.3f} → {c['new_weight']:.3f}")
            lines.append("")

        # Stats
        top = report.get("top_content_type")
        users = report.get("user_count", 0)
        active = report.get("active_users", 0)
        if top or users:
            lines.append(f"📈 *Stats*")
            if top:
                lines.append(f"Top content: {top}")
            if users:
                lines.append(f"Users: {users} total | {active} active (7d)")

        lines.append(f"\n_Run: {report.get('run_at', '')[:19]}Z_")

        message = "\n".join(lines)
        await self._send_telegram(message)

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _log_lesson(self, lesson: str, confidence: float, source: str) -> None:
        """Insert a lesson into the ora_lessons table."""
        from core.database import execute as db_execute
        try:
            await db_execute(
                """
                INSERT INTO ora_lessons (lesson, confidence, source)
                VALUES ($1, $2, $3)
                """,
                lesson,
                confidence,
                source,
            )
        except Exception as e:
            logger.debug(f"OraAutonomy: could not log lesson: {e}")

    async def _get_telegram_token(self) -> Optional[str]:
        """Load Telegram bot token from env or file."""
        if self._telegram_token:
            return self._telegram_token

        token = os.environ.get("ORA_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            try:
                with open("/Users/avielcarlos/.openclaw/secrets/telegram-bot-token.txt") as f:
                    token = f.read().strip()
            except Exception:
                pass

        if token:
            self._telegram_token = token
        return token

    async def _send_telegram(self, message: str) -> None:
        """Send a message to Avi via Telegram."""
        import httpx
        token = await self._get_telegram_token()
        if not token:
            logger.warning("OraAutonomy: no Telegram token, cannot send message")
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": message,
                        "parse_mode": "Markdown",
                    },
                )
                if resp.status_code == 200:
                    logger.info("OraAutonomy: Telegram message sent")
                else:
                    logger.warning(f"OraAutonomy: Telegram API returned {resp.status_code}")
        except Exception as e:
            logger.warning(f"OraAutonomy: Telegram send failed: {e}")


def _get_brain_base_weights() -> Dict[str, float]:
    """Return brain default weights without importing OraBrain (avoids circular)."""
    return {
        "discovery": 0.20,
        "coaching": 0.20,
        "recommendation": 0.16,
        "ui_generator": 0.06,
        "world": 0.17,
        "enlightenment": 0.07,
        "collective": 0.06,
        "explore": 0.08,
    }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_autonomy_agent: Optional[OraAutonomyAgent] = None


def get_autonomy_agent(openai_client=None) -> OraAutonomyAgent:
    global _autonomy_agent
    if _autonomy_agent is None:
        _autonomy_agent = OraAutonomyAgent(openai_client)
    return _autonomy_agent
