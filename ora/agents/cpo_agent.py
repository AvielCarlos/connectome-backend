"""
CPO Agent — Product Intelligence.

Owns the product. Understands what users want, what's broken,
what to build next. Makes product decisions backed by data.
"""

import logging
import os
import subprocess
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ora.agents.base_executive_agent import BaseExecutiveAgent

logger = logging.getLogger(__name__)

GH_REPO = "AvielCarlos/connectome-backend"


class CPOAgent(BaseExecutiveAgent):
    """
    Ora's Chief Product Officer.
    
    Analyzes goal patterns, card ratings, conversation topics, and
    onboarding flows to identify what to build next.
    """

    name = "cpo"
    display_name = "CPO Agent"
    domain = "product"
    personality = (
        "Product philosopher balancing user flourishing with revenue potential; "
        "cross-references CGO pricing learnings and CTO delivery velocity."
    )

    async def analyze(self) -> Dict[str, Any]:
        """Pull product signals from the DB and identify patterns."""
        await self.compound_context()
        now = datetime.now(timezone.utc)
        metrics: Dict[str, Any] = {
            "analyzed_at": now.isoformat(),
            "top_goal_themes": [],
            "avg_card_rating": 0.0,
            "lowest_rated_card_types": [],
            "highest_rated_card_types": [],
            "top_ora_topics": [],
            "onboarding_completion_rate_pct": 0.0,
            "dao_task_completion_rate_pct": 0.0,
            "pain_points": [],
            "wins": [],
        }

        try:
            from core.database import fetch, fetchrow

            # ── Goal themes ────────────────────────────────────────────
            goal_rows = await fetch(
                """
                SELECT content, COUNT(*) as n
                FROM goals
                WHERE created_at > NOW() - INTERVAL '30 days'
                GROUP BY content
                ORDER BY n DESC
                LIMIT 20
                """
            )
            if goal_rows:
                # Cluster by simple keyword
                theme_counter: Counter = Counter()
                for row in goal_rows:
                    content = (row.get("content") or "").lower()
                    for keyword in ["health", "fitness", "money", "finance", "career", "business",
                                    "relationships", "mindset", "productivity", "learning", "creative"]:
                        if keyword in content:
                            theme_counter[keyword] += row.get("n", 1)
                metrics["top_goal_themes"] = [k for k, _ in theme_counter.most_common(5)]

            # ── Card ratings ───────────────────────────────────────────
            rating_rows = await fetch(
                """
                SELECT agent_type, AVG(rating) as avg_rating, COUNT(*) as n
                FROM screen_feedback
                WHERE created_at > NOW() - INTERVAL '30 days'
                GROUP BY agent_type
                ORDER BY avg_rating ASC
                """
            )
            if rating_rows:
                all_ratings = [r for r in rating_rows if r.get("n", 0) >= 3]
                if all_ratings:
                    overall_sum = sum(r.get("avg_rating", 0) for r in all_ratings)
                    metrics["avg_card_rating"] = round(overall_sum / len(all_ratings), 2)
                    metrics["lowest_rated_card_types"] = [
                        {"type": r["agent_type"], "avg": round(r["avg_rating"], 2)}
                        for r in all_ratings[:3]
                    ]
                    metrics["highest_rated_card_types"] = [
                        {"type": r["agent_type"], "avg": round(r["avg_rating"], 2)}
                        for r in sorted(all_ratings, key=lambda x: x["avg_rating"], reverse=True)[:3]
                    ]

            # ── Ora conversation topics ────────────────────────────────
            topic_rows = await fetch(
                """
                SELECT message, COUNT(*) as n
                FROM ora_messages
                WHERE role = 'user'
                  AND created_at > NOW() - INTERVAL '14 days'
                ORDER BY n DESC
                LIMIT 100
                """
            )
            if topic_rows:
                topic_counter: Counter = Counter()
                for row in topic_rows:
                    msg = (row.get("message") or "").lower()
                    for topic in ["goal", "habit", "motivation", "anxiety", "focus", "career",
                                  "relationship", "money", "health", "plan", "help", "stuck"]:
                        if topic in msg:
                            topic_counter[topic] += 1
                metrics["top_ora_topics"] = [k for k, _ in topic_counter.most_common(5)]

            # ── Onboarding completion ─────────────────────────────────
            onboarding_row = await fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE onboarding_completed = true)::float /
                    NULLIF(COUNT(*), 0) * 100 as completion_pct
                FROM users
                WHERE created_at > NOW() - INTERVAL '30 days'
                """
            )
            if onboarding_row:
                metrics["onboarding_completion_rate_pct"] = round(
                    onboarding_row.get("completion_pct") or 0, 1
                )

            # ── DAO task completion ───────────────────────────────────
            dao_data = await self._api_get("/api/dao/tasks")
            if dao_data:
                tasks = dao_data.get("tasks", [])
                if tasks:
                    completed = sum(1 for t in tasks if t.get("status") == "completed")
                    metrics["dao_task_completion_rate_pct"] = round(completed / len(tasks) * 100, 1)

        except Exception as e:
            logger.error(f"CPO: analyze failed: {e}")

        # ── Identify pain points and wins ──────────────────────────────
        if metrics["lowest_rated_card_types"]:
            for item in metrics["lowest_rated_card_types"]:
                if item["avg"] < 3.0:
                    metrics["pain_points"].append(
                        f"{item['type']} cards have low avg rating ({item['avg']})"
                    )

        if metrics["onboarding_completion_rate_pct"] < 50:
            metrics["pain_points"].append(
                f"Onboarding completion is only {metrics['onboarding_completion_rate_pct']}% "
                f"— users are dropping off"
            )

        if metrics["highest_rated_card_types"]:
            for item in metrics["highest_rated_card_types"]:
                if item["avg"] >= 4.0:
                    metrics["wins"].append(
                        f"{item['type']} cards are well-loved (avg {item['avg']})"
                    )

        return metrics

    async def report(self) -> str:
        data = await self.load_last_report()
        if not data:
            data = await self.analyze()
        themes = ", ".join(data.get("top_goal_themes", [])) or "N/A"
        topics = ", ".join(data.get("top_ora_topics", [])) or "N/A"
        pain = "; ".join(data.get("pain_points", [])) or "None detected"
        return (
            f"📦 *CPO Report* — {data.get('analyzed_at', '')[:10]}\n"
            f"Top goal themes: {themes}\n"
            f"Top Ora topics: {topics}\n"
            f"Avg card rating: {data.get('avg_card_rating', 0)}\n"
            f"Onboarding completion: {data.get('onboarding_completion_rate_pct', 0)}%\n"
            f"DAO task completion: {data.get('dao_task_completion_rate_pct', 0)}%\n"
            f"Pain points: {pain}"
        )

    async def recommend(self) -> List[str]:
        data = await self.analyze()
        recs = list(data.get("pain_points", []))
        if data["onboarding_completion_rate_pct"] < 60:
            recs.append("Simplify onboarding — remove steps, add progress bar, front-load aha moment")
        if data["top_ora_topics"]:
            recs.append(
                f"Users keep asking about: {', '.join(data['top_ora_topics'][:3])} — "
                f"build dedicated flows for these topics"
            )
        if not recs:
            recs.append("Product is performing well. Focus on polish and speed.")
        return recs

    async def act(self) -> Dict[str, Any]:
        """Weekly CPO autonomous actions."""
        data = await self.analyze()
        actions_taken = []

        # Save report
        await self.save_report(data, "cpo_report.json")
        actions_taken.append("Saved CPO report")

        # Redis
        summary = await self.report()
        await self.set_redis_report(summary)

        # Teach Ora
        themes = ", ".join(data.get("top_goal_themes", []))
        topics = ", ".join(data.get("top_ora_topics", []))
        insight = (
            f"Product intelligence {data['analyzed_at'][:10]}: "
            f"top goal themes=[{themes}], "
            f"top Ora topics=[{topics}], "
            f"avg card rating={data['avg_card_rating']}, "
            f"onboarding completion={data['onboarding_completion_rate_pct']}%."
        )
        await self.teach_ora(insight, confidence=0.85)
        actions_taken.append("Taught Ora product state")

        # Create GitHub issues for pain points
        for pain_point in data.get("pain_points", [])[:3]:
            issue_created = await self.propose_feature(pain_point)
            if issue_created:
                actions_taken.append(f"Created GH issue: {pain_point[:60]}")

        # Alert if critical UX issue
        critical = [p for p in data.get("pain_points", []) if "onboarding" in p.lower()]
        if critical:
            await self.alert_avi(
                f"⚠️ Critical UX issue detected:\n" + "\n".join(critical)
            )
            actions_taken.append("Alerted Avi: critical UX issue")

        return {"agent": self.name, "actions": actions_taken, "metrics": data}

    async def propose_feature(self, description: str) -> bool:
        """Create a GitHub issue for a product improvement."""
        try:
            result = subprocess.run(
                [
                    "gh", "issue", "create",
                    "--repo", GH_REPO,
                    "--title", f"[CPO] {description[:80]}",
                    "--body", (
                        f"**Source:** CPO Agent automated analysis\n"
                        f"**Date:** {datetime.now(timezone.utc).date()}\n\n"
                        f"**Issue:** {description}\n\n"
                        f"**Priority:** Medium\n"
                        f"**Labels:** product, automated\n\n"
                        f"_This issue was automatically created by Ora's CPO Agent._"
                    ),
                    "--label", "product",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                logger.info(f"CPO: GitHub issue created: {result.stdout.strip()}")
                return True
            else:
                logger.debug(f"CPO: gh issue create failed: {result.stderr[:200]}")
        except Exception as e:
            logger.debug(f"CPO: propose_feature failed: {e}")
        return False
