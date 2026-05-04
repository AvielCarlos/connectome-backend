"""
CMO Agent — Growth & Marketing Intelligence.

Owns acquisition channels, analyzes what's working, adjusts the marketing mix.
Thinks in terms of CAC, conversion rates, viral loops, and growth levers.
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx

from aura.agents.base_executive_agent import BaseExecutiveAgent

logger = logging.getLogger(__name__)

RUNTIME_DIR = os.getenv("CONNECTOME_RUNTIME_DIR", "/tmp/connectome")
TWITTER_LOG = os.path.join(RUNTIME_DIR, "aura_outreach", "twitter_log.json")
OUTREACH_LOG = os.path.join(RUNTIME_DIR, "services", "outreach_log.json")
API_BASE = os.getenv("CONNECTOME_API_BASE", "https://connectome-api-production.up.railway.app")


class CMOAgent(BaseExecutiveAgent):
    """
    Aura's Chief Marketing Officer.
    
    Owns growth strategy. Tracks channels, spots what's working,
    and generates experiments to accelerate user acquisition.
    """

    name = "cmo"
    display_name = "CMO Agent"
    domain = "growth"
    personality = (
        "Growth and narrative architect. Knows CGO's revenue targets, builds campaigns "
        "to hit them, and cross-references CPO product milestones to time launches."
    )

    async def analyze(self) -> Dict[str, Any]:
        """Pull growth metrics from all channels and compute KPIs."""
        await self.compound_context()
        now = datetime.now(timezone.utc)
        metrics: Dict[str, Any] = {
            "analyzed_at": now.isoformat(),
            "total_users": 0,
            "new_users_7d": 0,
            "new_users_30d": 0,
            "active_users_7d": 0,
            "weekly_growth_rate_pct": 0.0,
            "twitter_replies_sent": 0,
            "twitter_clicks_est": 0,
            "email_sent": 0,
            "email_opens_est": 0,
            "email_conversion_est": 0,
            "best_channel": "organic",
            "viral_coefficient_est": 0.0,
            "growth_trend": "unknown",
        }

        # Pull from admin API
        admin_data = await self._api_get("/api/admin/insights")
        if admin_data:
            metrics["total_users"] = admin_data.get("total_users", 0) or 0
            metrics["active_users_7d"] = admin_data.get("active_users_7d", 0) or 0

        # Pull growth metrics
        growth_data = await self._api_get("/api/admin/growth-metrics")
        if growth_data:
            metrics["new_users_7d"] = growth_data.get("new_users_7d", 0) or 0
            metrics["new_users_30d"] = growth_data.get("new_users_30d", 0) or 0
            metrics["weekly_growth_rate_pct"] = growth_data.get("growth_rate_pct", 0) or 0

        # Twitter performance
        twitter_stats = self._load_json(TWITTER_LOG)
        if twitter_stats:
            if isinstance(twitter_stats, list):
                metrics["twitter_replies_sent"] = len(twitter_stats)
                # Estimate: ~2% of replies get a profile visit, 10% of those sign up
                metrics["twitter_clicks_est"] = int(len(twitter_stats) * 0.02)
            elif isinstance(twitter_stats, dict):
                metrics["twitter_replies_sent"] = twitter_stats.get("total_sent", 0)
                metrics["twitter_clicks_est"] = twitter_stats.get("clicks", 0)

        # Email outreach performance
        outreach_stats = self._load_json(OUTREACH_LOG)
        if outreach_stats:
            if isinstance(outreach_stats, list):
                metrics["email_sent"] = len(outreach_stats)
                metrics["email_opens_est"] = int(len(outreach_stats) * 0.25)  # 25% open rate est
                metrics["email_conversion_est"] = int(len(outreach_stats) * 0.03)  # 3% est
            elif isinstance(outreach_stats, dict):
                metrics["email_sent"] = outreach_stats.get("total_sent", 0)

        # Determine best channel
        if metrics["twitter_clicks_est"] > metrics["email_conversion_est"]:
            metrics["best_channel"] = "twitter"
        elif metrics["email_conversion_est"] > 0:
            metrics["best_channel"] = "email"

        # Viral coefficient (K-factor estimate)
        # K = invites_per_user * conversion_rate; estimate at 0.1 for now
        if metrics["total_users"] > 0:
            metrics["viral_coefficient_est"] = round(
                metrics["new_users_7d"] / max(metrics["total_users"], 1), 3
            )

        # Growth trend
        if metrics["weekly_growth_rate_pct"] > 10:
            metrics["growth_trend"] = "accelerating"
        elif metrics["weekly_growth_rate_pct"] > 0:
            metrics["growth_trend"] = "growing"
        elif metrics["weekly_growth_rate_pct"] < -5:
            metrics["growth_trend"] = "declining"
        else:
            metrics["growth_trend"] = "flat"

        return metrics

    def _load_json(self, path: str) -> Any:
        try:
            if os.path.exists(path):
                with open(path) as f:
                    return json.load(f)
        except Exception:
            pass
        return None

    async def report(self) -> str:
        data = await self.load_last_report()
        if not data:
            data = await self.analyze()
        return (
            f"📣 *CMO Report* — {data.get('analyzed_at', '')[:10]}\n"
            f"Users: {data.get('total_users', 0)} total | "
            f"+{data.get('new_users_7d', 0)} this week\n"
            f"Active (7d): {data.get('active_users_7d', 0)} | "
            f"Growth: {data.get('weekly_growth_rate_pct', 0):.1f}%/wk ({data.get('growth_trend', '?')})\n"
            f"Twitter replies: {data.get('twitter_replies_sent', 0)} | "
            f"Email sent: {data.get('email_sent', 0)}\n"
            f"Best channel: {data.get('best_channel', 'organic')} | "
            f"Viral K: {data.get('viral_coefficient_est', 0):.3f}"
        )

    async def recommend(self) -> List[str]:
        data = await self.analyze()
        recs = []
        if data["growth_trend"] in ("declining", "flat"):
            recs.append("Growth stalling — run a new Twitter content experiment this week")
            recs.append("Try a referral incentive: invite 3 friends → unlock Sovereign for 1 month")
        if data["best_channel"] == "twitter":
            recs.append("Twitter outperforming email — double reply volume this week")
        if data["viral_coefficient_est"] < 0.05:
            recs.append("Viral K is low — add in-app sharing moments (share goal milestone, share Aura insight)")
        if data["email_sent"] > 500 and data["email_conversion_est"] < 5:
            recs.append("Email conversion is low — A/B test subject lines and CTAs")
        if not recs:
            recs.append("Growth looks healthy. Keep the current channel mix and monitor weekly.")
        return recs

    async def act(self) -> Dict[str, Any]:
        """Weekly CMO autonomous actions."""
        data = await self.analyze()
        actions_taken = []

        # Save report
        await self.save_report(data, "cmo_report.json")
        actions_taken.append("Saved CMO report")

        # Redis
        summary = await self.report()
        await self.set_redis_report(summary)
        actions_taken.append("Updated Redis summary")

        # Teach Aura
        insight = (
            f"Growth state {data['analyzed_at'][:10]}: "
            f"{data['total_users']} users, "
            f"+{data['new_users_7d']} this week ({data['growth_trend']} trend), "
            f"best channel={data['best_channel']}, "
            f"viral K={data['viral_coefficient_est']:.3f}."
        )
        await self.teach_aura(insight, confidence=0.8)
        actions_taken.append("Taught Aura growth state")

        # Escalate if declining
        if data["growth_trend"] == "declining":
            await self.alert_avi(
                f"⚠️ Growth is declining!\n"
                f"New users this week: {data['new_users_7d']}\n"
                f"Growth rate: {data['weekly_growth_rate_pct']:.1f}%\n"
                f"Action needed: review acquisition channels."
            )
            actions_taken.append("Alerted Avi: growth decline")

        # Generate campaign idea and teach Aura
        campaign = await self.generate_campaign(data["best_channel"])
        await self.teach_aura(
            f"New growth experiment: {campaign}",
            confidence=0.6
        )
        actions_taken.append(f"Generated campaign: {campaign[:60]}")

        return {"agent": self.name, "actions": actions_taken, "metrics": data}

    async def generate_campaign(self, channel: str = "organic") -> str:
        """Generate a growth experiment idea based on what's working."""
        now = datetime.now(timezone.utc)
        week_num = now.isocalendar()[1]

        # Rotate through experiment types
        experiments = [
            "Themed week: 'Clarity Week' — daily Aura prompts about mental clarity shared on Twitter",
            "Challenge: '7-day goal sprint' — users share progress daily with #OraGoals hashtag",
            "Creator collab: DM 5 productivity YouTubers with a personalized Aura demo",
            "Feature spotlight: Thread on X about Aura's AI goal coach — real user success stories",
            "Community launch: Open the DAO to new contributors with a bounty for first contribution",
            "Referral push: Email existing users with 'bring a friend, both get Sovereign for free week'",
            "Niche targeting: Focus Twitter replies on 'quit job to build startup' + 'new year goals' threads",
            "Case study: Write up one power user's transformation using Aura → post as blog + thread",
        ]

        experiment = experiments[week_num % len(experiments)]
        return f"Week {week_num} growth experiment ({channel} focus): {experiment}"
