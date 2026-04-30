"""
Community Agent / CHRO — People, DAO, and Contributor Intelligence.

Grows and nurtures the DAO. Knows every contributor's journey and contributions.
Finds new talent. Keeps the community alive and engaged.
"""

import json
import logging
import os
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx

from ora.agents.base_executive_agent import BaseExecutiveAgent

logger = logging.getLogger(__name__)

RECRUITING_LOG = "/tmp/ascension/recruiting/sent_log.json"
GH_REPO = "AvielCarlos/connectome-backend"


class CommunityAgent(BaseExecutiveAgent):
    """
    Ora's Chief Human Resources Officer / Community Lead.
    
    Tracks contributor health, DAO participation, recruiting pipeline,
    and takes autonomous actions to grow and retain the community.
    """

    name = "community"
    display_name = "Community Agent"
    domain = "community"
    personality = (
        "Culture keeper. Ensures revenue strategies protect community trust and feeds "
        "CGO with ethical community monetisation signals."
    )

    async def analyze(self) -> Dict[str, Any]:
        """Analyze community health and contributor activity."""
        await self.compound_context()
        now = datetime.now(timezone.utc)
        cutoff_14d = now - timedelta(days=14)
        cutoff_30d = now - timedelta(days=30)

        metrics: Dict[str, Any] = {
            "analyzed_at": now.isoformat(),
            "total_contributors": 0,
            "active_contributors_30d": 0,
            "new_contributors_30d": 0,
            "inactive_contributors": [],
            "top_cp_earners": [],
            "cp_concentration_pct": 0.0,
            "github_prs_30d": 0,
            "github_issues_30d": 0,
            "recruiting_sent": 0,
            "recruiting_converted": 0,
            "community_health_score": 0,
        }

        # ── DB: contributor data ──────────────────────────────────────
        try:
            from core.database import fetch, fetchrow

            # Total contributors
            row = await fetchrow("SELECT COUNT(*) as n FROM contributors")
            if row:
                metrics["total_contributors"] = row["n"] or 0

            # Active in last 30d
            active_rows = await fetch(
                """
                SELECT user_id, contribution_points, last_contribution_at
                FROM contributors
                WHERE last_contribution_at > $1
                ORDER BY contribution_points DESC
                LIMIT 50
                """,
                cutoff_30d,
            )
            if active_rows:
                metrics["active_contributors_30d"] = len(active_rows)
                metrics["top_cp_earners"] = [
                    {
                        "user_id": str(r["user_id"]),
                        "cp": r["contribution_points"] or 0,
                    }
                    for r in active_rows[:5]
                ]

                # CP concentration: top 3 contributors hold what % of total CP?
                total_cp = sum(r["contribution_points"] or 0 for r in active_rows)
                top3_cp = sum(r["contribution_points"] or 0 for r in active_rows[:3])
                if total_cp > 0:
                    metrics["cp_concentration_pct"] = round(top3_cp / total_cp * 100, 1)

            # New contributors this month
            new_row = await fetchrow(
                "SELECT COUNT(*) as n FROM contributors WHERE joined_at > $1",
                cutoff_30d,
            )
            if new_row:
                metrics["new_contributors_30d"] = new_row["n"] or 0

            # Inactive for 14+ days
            inactive_rows = await fetch(
                """
                SELECT user_id, last_contribution_at
                FROM contributors
                WHERE last_contribution_at < $1
                  AND last_contribution_at > NOW() - INTERVAL '90 days'
                LIMIT 20
                """,
                cutoff_14d,
            )
            if inactive_rows:
                metrics["inactive_contributors"] = [str(r["user_id"]) for r in inactive_rows]

        except Exception as e:
            logger.error(f"Community: DB analysis failed: {e}")

        # ── GitHub activity ───────────────────────────────────────────
        try:
            result = subprocess.run(
                [
                    "gh", "pr", "list",
                    "--repo", GH_REPO,
                    "--state", "all",
                    "--limit", "50",
                    "--json", "createdAt,author",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode == 0 and result.stdout.strip():
                prs = json.loads(result.stdout)
                cutoff_ts = cutoff_30d.isoformat()
                recent_prs = [p for p in prs if p.get("createdAt", "") > cutoff_ts]
                metrics["github_prs_30d"] = len(recent_prs)
        except Exception as e:
            logger.debug(f"Community: GitHub PR check failed: {e}")

        # ── Recruiting pipeline ───────────────────────────────────────
        if os.path.exists(RECRUITING_LOG):
            try:
                with open(RECRUITING_LOG) as f:
                    recruiting_data = json.load(f)
                if isinstance(recruiting_data, list):
                    metrics["recruiting_sent"] = len(recruiting_data)
                    metrics["recruiting_converted"] = sum(
                        1 for r in recruiting_data
                        if r.get("status") == "converted" or r.get("joined")
                    )
                elif isinstance(recruiting_data, dict):
                    metrics["recruiting_sent"] = recruiting_data.get("total_sent", 0)
                    metrics["recruiting_converted"] = recruiting_data.get("converted", 0)
            except Exception:
                pass

        # ── Community health score ────────────────────────────────────
        score = 50  # baseline
        if metrics["active_contributors_30d"] > 5:
            score += 20
        if metrics["new_contributors_30d"] > 2:
            score += 15
        if metrics["cp_concentration_pct"] < 50:
            score += 10  # healthy distribution
        if metrics["cp_concentration_pct"] > 80:
            score -= 20  # too concentrated
        if metrics["github_prs_30d"] > 5:
            score += 10
        if len(metrics["inactive_contributors"]) > 10:
            score -= 15
        metrics["community_health_score"] = max(0, min(100, score))

        return metrics

    async def report(self) -> str:
        data = await self.load_last_report()
        if not data:
            data = await self.analyze()
        return (
            f"👥 *Community Report* — {data.get('analyzed_at', '')[:10]}\n"
            f"Contributors: {data.get('total_contributors', 0)} total | "
            f"{data.get('active_contributors_30d', 0)} active (30d)\n"
            f"New this month: {data.get('new_contributors_30d', 0)} | "
            f"Inactive (14d+): {len(data.get('inactive_contributors', []))}\n"
            f"GitHub PRs (30d): {data.get('github_prs_30d', 0)}\n"
            f"CP concentration: {data.get('cp_concentration_pct', 0)}% in top 3\n"
            f"Recruiting: {data.get('recruiting_sent', 0)} sent, "
            f"{data.get('recruiting_converted', 0)} converted\n"
            f"Health score: {data.get('community_health_score', 0)}/100"
        )

    async def recommend(self) -> List[str]:
        data = await self.analyze()
        recs = []
        if data["active_contributors_30d"] < 3:
            recs.append("Community activity is low — launch a bounty challenge or CP sprint")
        if data["cp_concentration_pct"] > 70:
            recs.append("CP too concentrated in top contributors — create entry-level tasks")
        if len(data["inactive_contributors"]) > 5:
            recs.append(f"Re-engage {len(data['inactive_contributors'])} inactive contributors")
        if data["new_contributors_30d"] == 0:
            recs.append("No new contributors this month — push recruiting harder")
        if not recs:
            recs.append("Community is healthy. Keep the momentum going.")
        return recs

    async def act(self) -> Dict[str, Any]:
        """Weekly community autonomous actions."""
        data = await self.analyze()
        actions_taken = []

        # Save report
        await self.save_report(data, "community_report.json")
        actions_taken.append("Saved community report")

        # Redis
        summary = await self.report()
        await self.set_redis_report(summary)

        # Teach Ora
        insight = (
            f"Community health {data['analyzed_at'][:10]}: "
            f"{data['total_contributors']} contributors, "
            f"{data['active_contributors_30d']} active (30d), "
            f"+{data['new_contributors_30d']} new, "
            f"CP concentration={data['cp_concentration_pct']}%, "
            f"health={data['community_health_score']}/100."
        )
        await self.teach_aura(insight, confidence=0.75)
        actions_taken.append("Taught Ora community state")

        # Re-engagement for inactive contributors
        inactive_count = len(data.get("inactive_contributors", []))
        if inactive_count > 0:
            # Log intent (actual email sending requires email infra)
            logger.info(
                f"Community: {inactive_count} inactive contributors flagged for re-engagement"
            )
            actions_taken.append(
                f"Flagged {inactive_count} inactive contributors for re-engagement"
            )

        # Alert if health is low
        if data["community_health_score"] < 40:
            await self.alert_avi(
                f"⚠️ Community health is low ({data['community_health_score']}/100)\n"
                f"Active contributors: {data['active_contributors_30d']}\n"
                f"Inactive: {inactive_count}\n"
                f"New this month: {data['new_contributors_30d']}"
            )
            actions_taken.append("Alerted Avi: low community health")

        return {"agent": self.name, "actions": actions_taken, "metrics": data}
