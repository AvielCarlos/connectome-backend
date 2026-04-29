# Run weekly — see cron job aios-evolution-weekly
"""
AIOS Evolution Agent

Evolves Ora's AIOS launcher and IOO roadmap from aggregate user intent.
All analysis is collective/anonymized: only GROUP BY counts and engagement totals
are used, never individual user records.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.database import execute, fetch, fetchrow, fetchval

logger = logging.getLogger(__name__)

DEFAULT_FEATURED_APPS = ["iDo", "Aventi", "iVive", "Eviva"]
DOMAIN_KEYWORDS = {
    "iVive": [
        "health", "fitness", "mental", "emotional", "sleep", "nutrition",
        "wellbeing", "wellness", "spiritual", "purpose", "longevity", "habit",
    ],
    "Aventi": [
        "event", "adventure", "travel", "experience", "dating", "friend",
        "concert", "festival", "fun", "social", "explore", "discovery",
    ],
    "Eviva": [
        "career", "work", "job", "volunteer", "service", "cause", "mission",
        "contribute", "income", "finance", "community", "dao", "mentor",
    ],
}


class AIOSEvolutionAgent:
    """Compute the current collective direction of the AIOS."""

    def __init__(self, openai_client=None):
        self._openai = openai_client
        self._running = False
        self._check_interval_seconds = 7 * 24 * 3600

    async def start_weekly_evolution_loop(self) -> None:
        """Optional in-process weekly loop; production cron may call run_weekly_evolution directly."""
        if self._running:
            return
        self._running = True
        logger.info("AIOSEvolutionAgent: weekly evolution loop started")
        while self._running:
            try:
                await self.run_weekly_evolution()
            except Exception as exc:
                logger.error("AIOSEvolutionAgent loop failed: %s", exc, exc_info=True)
            await asyncio.sleep(self._check_interval_seconds)

    def stop(self) -> None:
        self._running = False

    async def run_weekly_evolution(self) -> Dict[str, Any]:
        """Analyze collective goals + IOO engagement, store a new aios_state row."""
        top_goals = await self._top_onboarding_goals()
        engagement = await self._top_ioo_nodes_by_engagement()
        user_count = int(await fetchval("SELECT COUNT(*) FROM users") or 0)
        previous = await self._latest_state()

        featured_apps = self._rank_featured_apps(top_goals, engagement)
        mission = await self._mission_statement(top_goals, featured_apps)
        notes = self._evolution_notes(previous, top_goals, engagement, featured_apps)
        proposals = await self._create_ioo_node_proposals(top_goals, engagement)

        await execute(
            """
            INSERT INTO aios_state
                (ruling_goals, featured_apps, ora_mission_statement, evolution_notes, user_count, computed_at)
            VALUES ($1::jsonb, $2::jsonb, $3, $4, $5, NOW())
            """,
            json.dumps(top_goals),
            json.dumps(featured_apps),
            mission,
            notes,
            user_count,
        )

        state = {
            "ruling_goals": top_goals,
            "featured_apps": featured_apps,
            "ora_mission_statement": mission,
            "evolution_notes": notes,
            "user_count": user_count,
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "ioo_engagement_leaders": engagement,
            "node_proposals_created": proposals,
        }
        logger.info("AIOSEvolutionAgent: evolved AIOS state: %s", state)
        return state

    async def _top_onboarding_goals(self) -> List[Dict[str, Any]]:
        rows = await fetch(
            """
            SELECT field_value, COUNT(*)::int AS count
            FROM discovery_profile
            WHERE field_value IS NOT NULL
              AND btrim(field_value) <> ''
            GROUP BY field_value
            ORDER BY count DESC, field_value ASC
            LIMIT 20
            """
        )
        total = sum(int(row["count"] or 0) for row in rows) or 1
        return [
            {
                "goal": row["field_value"],
                "count": int(row["count"] or 0),
                "prevalence_pct": round((int(row["count"] or 0) / total) * 100, 1),
            }
            for row in rows
        ]

    async def _top_ioo_nodes_by_engagement(self) -> List[Dict[str, Any]]:
        rows = await fetch(
            """
            SELECT
                n.id::text,
                n.title,
                n.domain,
                n.tags,
                COUNT(DISTINCT p.id) FILTER (WHERE p.status = 'completed')::int AS completions,
                COALESCE(SUM(s.interaction_count), 0)::int AS feed_interactions,
                COALESCE(SUM(s.completion_count), 0)::int AS surface_completions,
                (
                    COUNT(DISTINCT p.id) FILTER (WHERE p.status = 'completed') * 3
                    + COALESCE(SUM(s.interaction_count), 0)
                    + COALESCE(SUM(s.completion_count), 0) * 2
                )::int AS engagement_score
            FROM ioo_nodes n
            LEFT JOIN ioo_user_progress p ON p.node_id = n.id
            LEFT JOIN ioo_surfaces s ON s.node_id = n.id
            WHERE n.is_active = true
            GROUP BY n.id, n.title, n.domain, n.tags
            ORDER BY engagement_score DESC, n.updated_at DESC
            LIMIT 20
            """
        )
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "domain": row["domain"],
                "tags": list(row["tags"] or []),
                "completions": int(row["completions"] or 0),
                "feed_interactions": int(row["feed_interactions"] or 0),
                "surface_completions": int(row["surface_completions"] or 0),
                "engagement_score": int(row["engagement_score"] or 0),
            }
            for row in rows
        ]

    async def _latest_state(self) -> Optional[Dict[str, Any]]:
        row = await fetchrow("SELECT * FROM aios_state ORDER BY computed_at DESC LIMIT 1")
        if not row:
            return None
        data = dict(row)
        data["ruling_goals"] = self._jsonish(data.get("ruling_goals"), [])
        data["featured_apps"] = self._jsonish(data.get("featured_apps"), DEFAULT_FEATURED_APPS)
        return data

    def _rank_featured_apps(self, goals: List[Dict[str, Any]], engagement: List[Dict[str, Any]]) -> List[str]:
        scores = {app: 0.0 for app in DEFAULT_FEATURED_APPS}
        scores["iDo"] = 1.0  # keep the core feed present unless stronger collective signals outrank it

        for item in goals:
            text = str(item.get("goal", "")).lower()
            weight = float(item.get("count", 1) or 1)
            for app, keywords in DOMAIN_KEYWORDS.items():
                if any(keyword in text for keyword in keywords):
                    scores[app] += weight

        for node in engagement:
            domain = str(node.get("domain") or "")
            score = float(node.get("engagement_score", 0) or 0)
            if domain in scores:
                scores[domain] += max(score, 0) / 5

        ranked = sorted(DEFAULT_FEATURED_APPS, key=lambda app: (-scores.get(app, 0), DEFAULT_FEATURED_APPS.index(app)))
        return ranked

    async def _mission_statement(self, goals: List[Dict[str, Any]], featured_apps: List[str]) -> str:
        top_goal_text = ", ".join(str(goal["goal"]) for goal in goals[:5]) or "clarity, vitality, contribution, and aliveness"
        fallback = (
            f"Ora is evolving this week to help people move toward {top_goal_text} "
            f"through {', '.join(featured_apps[:3])}."
        )
        if not self._openai:
            return fallback

        try:
            response = await self._openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "Write one clear, inspiring sentence for Ora's AIOS mission. No hype, no more than 28 words.",
                    },
                    {
                        "role": "user",
                        "content": f"Top collective goals: {top_goal_text}. Featured apps: {', '.join(featured_apps)}.",
                    },
                ],
                temperature=0.5,
                max_tokens=80,
            )
            statement = response.choices[0].message.content.strip()
            return statement or fallback
        except Exception as exc:
            logger.warning("AIOSEvolutionAgent mission generation failed: %s", exc)
            return fallback

    def _evolution_notes(
        self,
        previous: Optional[Dict[str, Any]],
        goals: List[Dict[str, Any]],
        engagement: List[Dict[str, Any]],
        featured_apps: List[str],
    ) -> str:
        top_goal = goals[0]["goal"] if goals else "no dominant onboarding goal yet"
        top_node = engagement[0]["title"] if engagement else "no IOO engagement leader yet"
        if not previous:
            return (
                f"Initial AIOS evolution baseline: top collective goal is '{top_goal}', "
                f"top IOO node is '{top_node}', and launcher priority is {', '.join(featured_apps[:4])}."
            )

        previous_apps = list(previous.get("featured_apps") or [])
        previous_goal = None
        previous_goals = previous.get("ruling_goals") or []
        if previous_goals:
            previous_goal = previous_goals[0].get("goal") if isinstance(previous_goals[0], dict) else None

        changes = []
        if previous_goal and previous_goal != top_goal:
            changes.append(f"dominant goal shifted from '{previous_goal}' to '{top_goal}'")
        if previous_apps[:2] != featured_apps[:2]:
            changes.append(f"featured launcher focus shifted to {', '.join(featured_apps[:2])}")
        if not changes:
            changes.append(f"collective direction held steady around '{top_goal}'")
        changes.append(f"highest engaged IOO node is '{top_node}'")
        return "; ".join(changes) + "."

    async def _create_ioo_node_proposals(
        self,
        goals: List[Dict[str, Any]],
        engagement: List[Dict[str, Any]],
    ) -> int:
        created = 0
        seeds = goals[:5]
        for item in seeds:
            goal = str(item.get("goal") or "").strip()
            if not goal:
                continue
            domain = self._infer_domain(goal, engagement)
            title = self._proposal_title(goal)
            exists = await fetchval(
                "SELECT 1 FROM ioo_node_proposals WHERE lower(title) = lower($1) LIMIT 1",
                title,
            )
            if exists:
                continue
            await execute(
                """
                INSERT INTO ioo_node_proposals
                    (title, description, goal_category, step_type, domain, tags, confidence, status)
                VALUES ($1, $2, $3, 'hybrid', $4, $5, $6, 'pending')
                """,
                title,
                f"Collective-goal proposal generated from emerging user aspiration: {goal}.",
                goal[:120],
                domain,
                [self._slug(goal), "aios-evolution", domain.lower()],
                min(0.95, 0.55 + (float(item.get("prevalence_pct", 0) or 0) / 100)),
            )
            created += 1
        return created

    def _infer_domain(self, text: str, engagement: List[Dict[str, Any]]) -> str:
        lower = text.lower()
        matches = {
            app: sum(1 for keyword in keywords if keyword in lower)
            for app, keywords in DOMAIN_KEYWORDS.items()
        }
        if any(matches.values()):
            return max(matches, key=matches.get)
        for node in engagement:
            domain = node.get("domain")
            if domain in DOMAIN_KEYWORDS:
                return str(domain)
        return "iVive"

    def _proposal_title(self, goal: str) -> str:
        goal = " ".join(goal.split())[:90]
        return f"Build a real-world path for: {goal}"

    def _slug(self, value: str) -> str:
        return "-".join("".join(ch.lower() if ch.isalnum() else " " for ch in value).split())[:40]

    def _jsonish(self, value: Any, fallback: Any) -> Any:
        if value is None:
            return fallback
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return fallback
        return value


async def run_aios_evolution(openai_client=None) -> Dict[str, Any]:
    """Cron-friendly entry point."""
    return await AIOSEvolutionAgent(openai_client=openai_client).run_weekly_evolution()
