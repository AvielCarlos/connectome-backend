"""
MetaAgent — Aura reflects on her own performance and queues improvements.

Runs periodically. Looks at:
- Which card types users engage with most (feedback table)
- Which suggestions got accepted/implemented
- Which goals users complete vs abandon
- Low-engagement areas (cards nobody interacts with)

Outputs a JSON report saved to Redis key 'aura:meta:report' with:
{
  "top_engaging_card_types": [...],
  "low_engagement_card_types": [...],
  "common_goal_patterns": [...],
  "suggested_improvements": ["Increase world_pulse cards", "Reduce on_this_day frequency"],
  "generated_at": "..."
}

This report is read by the brain agent to dynamically adjust card weights.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.database import fetch, fetchrow
from core.redis_client import get_redis

logger = logging.getLogger(__name__)

REDIS_KEY = "aura:meta:report"
REPORT_TTL = 24 * 3600  # 1 day


class MetaAgent:
    """
    Aura's self-reflection agent. Analyzes engagement patterns and produces
    improvement recommendations that feed back into card weight decisions.
    """

    def __init__(self, openai_client=None):
        self._openai = openai_client

    # ------------------------------------------------------------------
    # Core: generate and cache report
    # ------------------------------------------------------------------

    async def generate_report(self) -> Dict[str, Any]:
        """
        Query engagement data, use GPT-4o to analyze patterns,
        save report to Redis, and return it.
        """
        logger.info("MetaAgent: generating self-improvement report...")

        # Gather raw engagement data
        card_engagement = await self._get_card_type_engagement()
        goal_patterns = await self._get_goal_patterns()
        suggestion_stats = await self._get_suggestion_stats()

        # Identify top/low performers
        sorted_cards = sorted(card_engagement, key=lambda x: x["avg_rating"], reverse=True)
        top_engaging = [c["agent_type"] for c in sorted_cards[:3] if c["avg_rating"] >= 3.5]
        low_engaging = [c["agent_type"] for c in sorted_cards if c["avg_rating"] < 2.5]

        # Use GPT-4o to generate improvement suggestions (or fallback to heuristic)
        suggested_improvements = await self._generate_suggestions(
            card_engagement, goal_patterns, suggestion_stats
        )

        report = {
            "top_engaging_card_types": top_engaging,
            "low_engagement_card_types": low_engaging,
            "common_goal_patterns": goal_patterns[:5],
            "suggested_improvements": suggested_improvements,
            "raw_card_stats": card_engagement,
            "suggestion_stats": suggestion_stats,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

        # Cache in Redis
        try:
            r = await get_redis()
            await r.set(REDIS_KEY, json.dumps(report), ex=REPORT_TTL)
            logger.info(f"MetaAgent: report cached to Redis ({REDIS_KEY})")
        except Exception as e:
            logger.warning(f"MetaAgent: Redis cache failed: {e}")

        return report

    async def get_cached_report(self) -> Optional[Dict[str, Any]]:
        """Return the most recent cached report from Redis, or None."""
        try:
            r = await get_redis()
            raw = await r.get(REDIS_KEY)
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.debug(f"MetaAgent: cache read failed: {e}")
        return None

    # ------------------------------------------------------------------
    # Data gathering
    # ------------------------------------------------------------------

    async def _get_card_type_engagement(self) -> List[Dict[str, Any]]:
        """
        Query interactions joined with screen_specs to get per-agent-type
        avg rating, completion rate, and count over the last 7 days.
        """
        try:
            rows = await fetch(
                """
                SELECT
                    ss.agent_type,
                    COUNT(i.id) AS interaction_count,
                    ROUND(AVG(i.rating)::numeric, 2) AS avg_rating,
                    ROUND(AVG(CASE WHEN i.completed THEN 1.0 ELSE 0.0 END)::numeric, 2) AS completion_rate,
                    ROUND(AVG(i.time_on_screen_ms)::numeric, 0) AS avg_time_ms
                FROM interactions i
                JOIN screen_specs ss ON ss.id = i.screen_spec_id
                WHERE i.created_at > NOW() - INTERVAL '7 days'
                  AND i.rating IS NOT NULL
                GROUP BY ss.agent_type
                ORDER BY avg_rating DESC
                """
            )
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"MetaAgent: card engagement query failed: {e}")
            return []

    async def _get_goal_patterns(self) -> List[str]:
        """
        Identify common goal patterns — what categories users complete vs abandon.
        """
        try:
            rows = await fetch(
                """
                SELECT
                    CASE
                        WHEN status = 'completed' THEN 'completed'
                        WHEN status = 'active' AND progress > 0.5 THEN 'in_progress_high'
                        WHEN status = 'active' AND progress <= 0.5 THEN 'in_progress_low'
                        ELSE status
                    END AS pattern,
                    COUNT(*) AS count,
                    ROUND(AVG(progress)::numeric, 2) AS avg_progress
                FROM goals
                WHERE created_at > NOW() - INTERVAL '30 days'
                GROUP BY 1
                ORDER BY count DESC
                """
            )
            patterns = []
            for r in rows:
                patterns.append(f"{r['pattern']}: {r['count']} goals (avg_progress={r['avg_progress']})")
            return patterns
        except Exception as e:
            logger.warning(f"MetaAgent: goal patterns query failed: {e}")
            return []

    async def _get_suggestion_stats(self) -> Dict[str, Any]:
        """
        Get stats on suggestions submitted/accepted in last 30 days.
        Tries the contributions table, falls back gracefully.
        """
        stats: Dict[str, Any] = {"submitted": 0, "accepted": 0}
        try:
            row = await fetchrow(
                """
                SELECT COUNT(*) AS submitted,
                       SUM(CASE WHEN status = 'accepted' THEN 1 ELSE 0 END) AS accepted
                FROM contributions
                WHERE created_at > NOW() - INTERVAL '30 days'
                """
            )
            if row:
                stats["submitted"] = row["submitted"] or 0
                stats["accepted"] = row["accepted"] or 0
        except Exception:
            # Table might not exist yet — that's fine
            pass
        return stats

    # ------------------------------------------------------------------
    # GPT-4o analysis
    # ------------------------------------------------------------------

    async def _generate_suggestions(
        self,
        card_engagement: List[Dict],
        goal_patterns: List[str],
        suggestion_stats: Dict,
    ) -> List[str]:
        """Use GPT-4o to synthesize improvement suggestions, or fall back to heuristics."""
        if self._openai:
            try:
                return await self._suggestions_llm(card_engagement, goal_patterns, suggestion_stats)
            except Exception as e:
                logger.warning(f"MetaAgent: LLM suggestions failed: {e}")

        return self._suggestions_heuristic(card_engagement, goal_patterns)

    async def _suggestions_llm(
        self,
        card_engagement: List[Dict],
        goal_patterns: List[str],
        suggestion_stats: Dict,
    ) -> List[str]:
        prompt = f"""You are Aura, an AI system analyzing your own performance data to improve.

Card engagement data (last 7 days):
{json.dumps(card_engagement, default=str)}

Goal patterns (last 30 days):
{goal_patterns}

Suggestion stats: {suggestion_stats}

Based on this data, generate 3-5 concrete, actionable improvement suggestions.
Focus on: which card types to increase/decrease, which goal patterns to address,
and what content areas need improvement.

Return a JSON array of strings, each a short actionable suggestion.
Example: ["Increase EnlightenmentAgent frequency by 20%", "Reduce early-exit rate for WorldAgent cards"]
Return ONLY the JSON array."""

        response = await self._openai.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=300,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        # GPT returns a JSON object wrapping the array
        parsed = json.loads(content)
        if isinstance(parsed, list):
            return parsed
        # Try to extract array from object
        for v in parsed.values():
            if isinstance(v, list):
                return v
        return list(parsed.values())[:5]

    @staticmethod
    def _suggestions_heuristic(
        card_engagement: List[Dict],
        goal_patterns: List[str],
    ) -> List[str]:
        suggestions = []
        if not card_engagement:
            return ["Insufficient engagement data — collect more interactions before tuning"]

        top = card_engagement[0] if card_engagement else None
        bottom = card_engagement[-1] if len(card_engagement) > 1 else None

        if top:
            suggestions.append(f"Boost {top['agent_type']} frequency — highest avg rating ({top['avg_rating']})")
        if bottom and float(bottom.get("avg_rating") or 0) < 2.5:
            suggestions.append(f"Reduce {bottom['agent_type']} frequency — lowest avg rating ({bottom['avg_rating']})")

        # Check for low completion rates
        low_completion = [c for c in card_engagement if float(c.get("completion_rate") or 0) < 0.3]
        if low_completion:
            names = ", ".join(c["agent_type"] for c in low_completion[:2])
            suggestions.append(f"Improve content depth for {names} — completion rate < 30%")

        # Check goal abandonment
        for pattern in goal_patterns:
            if "in_progress_low" in pattern:
                suggestions.append("Add more coaching nudges for stalled goals (progress < 50%)")
                break

        return suggestions or ["Maintain current card mix — engagement data looks healthy"]
