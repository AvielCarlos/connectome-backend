"""
UXResearchAgent — Synthesizes user behavior into UX insights.

Analyzes:
  - Avg time on screen per screen type
  - Drop-off rates per page (via exit_point field)
  - Which screen types lead to goal creation
  - Retention correlation: what session-1 behavior predicts return

Feeds findings to UIEvolutionAgent and Avi's daily report.
Stores results in Redis: aura:ux_insights (24h TTL)
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class UXResearchAgent:
    """Synthesizes user behavior into actionable UX insights."""

    def __init__(self, openai_client=None):
        self._openai = openai_client

    async def analyze(self) -> Dict[str, Any]:
        """
        Run the full UX analysis pipeline.
        Returns insights dict and stores in Redis.
        """
        from core.database import fetch as db_fetch, fetchval
        from core.redis_client import get_redis

        logger.info("UXResearchAgent: starting analysis")

        # 1. Gather behavior patterns from interactions
        behavior = await self._gather_behavior_patterns()

        # 2. Synthesize with GPT-4o
        insights = await self._synthesize_insights(behavior)

        # 3. Store in Redis with 24h TTL
        try:
            r = await get_redis()
            await r.set(
                "aura:ux_insights",
                json.dumps({
                    "insights": insights,
                    "behavior_summary": behavior,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }),
                ex=24 * 3600,
            )
            logger.info(f"UXResearchAgent: stored {len(insights)} insights in Redis")
        except Exception as e:
            logger.warning(f"UXResearchAgent: Redis store failed: {e}")

        return {"insights": insights, "behavior": behavior}

    # ------------------------------------------------------------------
    # Data gathering
    # ------------------------------------------------------------------

    async def _gather_behavior_patterns(self) -> Dict[str, Any]:
        """Query DB for behavior patterns across interaction types."""
        from core.database import fetch as db_fetch, fetchval

        behavior: Dict[str, Any] = {}

        # A. Avg time on screen per agent/screen type
        try:
            rows = await db_fetch(
                """
                SELECT
                    ss.agent_type,
                    AVG(i.time_on_screen_ms) AS avg_time_ms,
                    COUNT(*) AS view_count
                FROM interactions i
                JOIN screen_specs ss ON ss.id = i.screen_spec_id
                WHERE i.time_on_screen_ms IS NOT NULL
                  AND i.created_at >= NOW() - INTERVAL '14 days'
                GROUP BY ss.agent_type
                ORDER BY avg_time_ms DESC
                """
            )
            behavior["time_on_screen"] = [
                {
                    "agent_type": r["agent_type"],
                    "avg_time_seconds": round(float(r["avg_time_ms"] or 0) / 1000, 1),
                    "view_count": int(r["view_count"]),
                }
                for r in (rows or [])
            ]
        except Exception as e:
            logger.warning(f"UXResearchAgent: time_on_screen query failed: {e}")
            behavior["time_on_screen"] = []

        # B. Drop-off rates per exit point
        try:
            rows = await db_fetch(
                """
                SELECT
                    exit_point,
                    COUNT(*) AS exit_count,
                    AVG(CASE WHEN completed THEN 1.0 ELSE 0.0 END) AS completion_rate
                FROM interactions
                WHERE exit_point IS NOT NULL
                  AND created_at >= NOW() - INTERVAL '14 days'
                GROUP BY exit_point
                ORDER BY exit_count DESC
                LIMIT 20
                """
            )
            behavior["drop_off"] = [
                {
                    "exit_point": r["exit_point"],
                    "count": int(r["exit_count"]),
                    "completion_rate": round(float(r["completion_rate"] or 0), 3),
                }
                for r in (rows or [])
            ]
        except Exception as e:
            logger.warning(f"UXResearchAgent: drop_off query failed: {e}")
            behavior["drop_off"] = []

        # C. Which screen types lead to goal creation
        try:
            rows = await db_fetch(
                """
                SELECT
                    ss.agent_type,
                    COUNT(DISTINCT g.user_id) AS goal_creators,
                    COUNT(DISTINCT i.user_id) AS total_viewers
                FROM interactions i
                JOIN screen_specs ss ON ss.id = i.screen_spec_id
                LEFT JOIN goals g ON g.user_id = i.user_id
                    AND g.created_at >= i.created_at
                    AND g.created_at <= i.created_at + INTERVAL '1 hour'
                WHERE i.created_at >= NOW() - INTERVAL '14 days'
                GROUP BY ss.agent_type
                HAVING COUNT(DISTINCT i.user_id) > 5
                ORDER BY (COUNT(DISTINCT g.user_id)::float / NULLIF(COUNT(DISTINCT i.user_id), 0)) DESC
                """
            )
            behavior["goal_conversion"] = [
                {
                    "agent_type": r["agent_type"],
                    "goal_creators": int(r["goal_creators"]),
                    "total_viewers": int(r["total_viewers"]),
                    "conversion_rate": round(
                        float(r["goal_creators"]) / max(int(r["total_viewers"]), 1), 3
                    ),
                }
                for r in (rows or [])
            ]
        except Exception as e:
            logger.warning(f"UXResearchAgent: goal_conversion query failed: {e}")
            behavior["goal_conversion"] = []

        # D. Retention correlation: what session-1 behavior predicts return
        try:
            rows = await db_fetch(
                """
                SELECT
                    ss.agent_type,
                    AVG(CASE
                        WHEN u.last_active >= u.created_at + INTERVAL '1 day'
                        THEN 1.0 ELSE 0.0
                    END) AS d1_retention_rate,
                    COUNT(DISTINCT i.user_id) AS sample_size
                FROM interactions i
                JOIN screen_specs ss ON ss.id = i.screen_spec_id
                JOIN users u ON u.id = i.user_id
                WHERE i.created_at <= u.created_at + INTERVAL '6 hours'
                  AND i.created_at >= NOW() - INTERVAL '30 days'
                GROUP BY ss.agent_type
                HAVING COUNT(DISTINCT i.user_id) > 10
                ORDER BY d1_retention_rate DESC
                """
            )
            behavior["retention_correlation"] = [
                {
                    "agent_type": r["agent_type"],
                    "d1_retention_rate": round(float(r["d1_retention_rate"] or 0), 3),
                    "sample_size": int(r["sample_size"]),
                }
                for r in (rows or [])
            ]
        except Exception as e:
            logger.warning(f"UXResearchAgent: retention_correlation query failed: {e}")
            behavior["retention_correlation"] = []

        # E. Overall rating per agent type
        try:
            rows = await db_fetch(
                """
                SELECT
                    ss.agent_type,
                    AVG(i.rating) AS avg_rating,
                    COUNT(*) AS rated_count
                FROM interactions i
                JOIN screen_specs ss ON ss.id = i.screen_spec_id
                WHERE i.rating IS NOT NULL
                  AND i.created_at >= NOW() - INTERVAL '14 days'
                GROUP BY ss.agent_type
                ORDER BY avg_rating DESC
                """
            )
            behavior["ratings"] = [
                {
                    "agent_type": r["agent_type"],
                    "avg_rating": round(float(r["avg_rating"] or 0), 2),
                    "rated_count": int(r["rated_count"]),
                }
                for r in (rows or [])
            ]
        except Exception as e:
            logger.warning(f"UXResearchAgent: ratings query failed: {e}")
            behavior["ratings"] = []

        return behavior

    async def _synthesize_insights(self, behavior: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Use GPT-4o to synthesize behavior data into 3-5 actionable UX insights."""
        if not self._openai:
            logger.info("UXResearchAgent: no OpenAI client, returning raw summary")
            return self._fallback_insights(behavior)

        behavior_json = json.dumps(behavior, indent=2)[:3000]

        try:
            response = await self._openai.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are Aura's UX research agent. Analyze user behavior data from a "
                            "personalized growth app and extract 3-5 actionable UX insights. "
                            "Be specific and data-driven. Focus on what's driving or blocking engagement."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"""Analyze this user behavior data and return 3-5 UX insights as JSON:

{behavior_json}

Return JSON array: [{{
  "insight": "clear insight statement",
  "evidence": "specific data point",
  "recommendation": "actionable change to make",
  "priority": "high|medium|low",
  "area": "onboarding|feed|goals|navigation|engagement"
}}]""",
                    },
                ],
                temperature=0.3,
                max_tokens=800,
                response_format={"type": "json_object"},
            )

            raw = json.loads(response.choices[0].message.content)
            # Handle both {"insights": [...]} and [...]
            if isinstance(raw, list):
                return raw
            if isinstance(raw, dict):
                for k in ("insights", "items", "results"):
                    if k in raw and isinstance(raw[k], list):
                        return raw[k]
                return list(raw.values())[0] if raw else []

        except Exception as e:
            logger.warning(f"UXResearchAgent: GPT synthesis failed: {e}")
            return self._fallback_insights(behavior)

        return []

    def _fallback_insights(self, behavior: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Generate basic insights without LLM when OpenAI is unavailable."""
        insights = []

        # Best rated content type
        ratings = behavior.get("ratings", [])
        if ratings:
            top = ratings[0]
            insights.append({
                "insight": f"{top['agent_type']} content scores highest (avg {top['avg_rating']:.1f}/5)",
                "evidence": f"{top['rated_count']} ratings",
                "recommendation": f"Increase {top['agent_type']} card frequency",
                "priority": "high",
                "area": "feed",
            })

        # High drop-off points
        drop_off = behavior.get("drop_off", [])
        high_drop = [d for d in drop_off if d.get("completion_rate", 1) < 0.3 and d.get("count", 0) > 10]
        for d in high_drop[:2]:
            insights.append({
                "insight": f"High drop-off at '{d['exit_point']}' ({d['count']} exits, {d['completion_rate']*100:.0f}% completion)",
                "evidence": f"{d['count']} events in last 14 days",
                "recommendation": f"Investigate and improve the '{d['exit_point']}' screen",
                "priority": "medium",
                "area": "navigation",
            })

        return insights
