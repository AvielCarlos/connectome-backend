"""
CGO Agent — Chief Growth Officer for Connectome/Ora.

Mandate: aggressively and creatively grow revenue while protecting the
non-profit mission. The CGO never assumes a surface is impossible to monetize;
it finds ethical, legally sound revenue architecture around genuine value.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import httpx

from ora.agents.base_executive_agent import API_BASE, BaseExecutiveAgent
from ora.payments.growth_billing import (
    create_api_access_checkout,
    create_corporate_plan_checkout,
    create_ora_session_payment,
)

logger = logging.getLogger(__name__)

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", os.getenv("ADMIN_SECRET", "connectome-admin-secret"))


class CGOAgent(BaseExecutiveAgent):
    """
    Ora's Chief Growth Officer.

    Thinks like a startup growth hacker plus a mission-driven social enterprise:
    data-driven, commercial, fast-moving, and allergic to fake scarcity or
    deceptive billing. Revenue exists to make the human-flourishing mission
    durable.
    """

    name = "cgo"
    display_name = "CGO Agent"
    domain = "growth"
    personality = (
        "Revenue engine: aggressive, creative, and mission-aligned. Uses CFO data, "
        "CMO channels, CPO features, and CTO capacity to find compound growth levers."
    )

    async def analyze(self) -> Dict[str, Any]:
        await self.compound_context()
        now = datetime.now(timezone.utc)
        metrics = await self._get_revenue_metrics()
        research = {
            "freemium_conversion": await self._research_freemium_conversion(metrics),
            "grants": await self._research_grants(metrics),
            "b2b_corporate_wellness": await self._research_corporate_wellness(metrics),
            "api_access_monetization": await self._research_api_access(metrics),
            "community_membership": await self._research_community_membership(metrics),
            "ip_licensing_white_label": await self._research_ip_licensing(metrics),
        }
        streams = self._prioritize_revenue_streams(metrics, research)
        activation = await self._activate_top_streams(streams)

        report = {
            "agent": self.name,
            "analyzed_at": now.isoformat(),
            "mandate": "Revenue serves the mission: fund Ora's non-profit AI OS for human flourishing.",
            "metrics": metrics,
            "research": research,
            "prioritized_action_plan": self._build_action_plan(streams, metrics),
            "activated_revenue_streams": activation,
            "structured_growth_report": {
                "stage": self._stage(metrics),
                "top_3_revenue_streams": [s["name"] for s in streams[:3]],
                "north_star": "First $1,000 MRR without compromising trust or mission alignment.",
                "legal_guardrails": [
                    "No deceptive billing or forced continuity.",
                    "Clear cancellation path for every subscription.",
                    "Revenue claims must be grounded in actual user value and consent.",
                    "Grant and corporate messaging must preserve the non-profit mission.",
                ],
            },
        }
        return report

    async def report(self) -> str:
        data = await self.load_last_report("cgo_report.json") or await self.analyze()
        metrics = data.get("metrics", {})
        top = data.get("prioritized_action_plan", [])[:3]
        actions = "\n".join(f"• {item.get('action', item)}" for item in top)
        return (
            f"📈 *CGO Report* — {data.get('analyzed_at', '')[:10]}\n"
            f"Users: {metrics.get('total_users', 0)} | Paid: {metrics.get('paid_users', 0)} | "
            f"Revenue: ${metrics.get('total_revenue_cents', 0) / 100:.2f}\n"
            f"Stage: {data.get('structured_growth_report', {}).get('stage', 'unknown')}\n"
            f"Top moves:\n{actions}"
        )

    async def recommend(self) -> List[str]:
        data = await self.analyze()
        return [item["action"] for item in data.get("prioritized_action_plan", [])]

    async def act(self) -> Dict[str, Any]:
        data = await self.analyze()
        path = await self.save_report(data, "cgo_report.json")
        summary = await self.report()
        await self.set_redis_report(summary)
        await self.teach_ora(
            "CGO growth state: "
            f"{data['metrics'].get('total_users', 0)} users, "
            f"${data['metrics'].get('total_revenue_cents', 0) / 100:.2f} lifetime revenue, "
            f"top streams={', '.join(data['structured_growth_report']['top_3_revenue_streams'])}.",
            confidence=0.82,
        )
        return {
            "agent": self.name,
            "actions": [
                f"Saved CGO report to {path}",
                "Updated Redis executive summary",
                "Taught Ora the current growth thesis",
                "Prepared Stripe Checkout activation links for top revenue streams",
            ],
            "report": data,
        }

    async def _get_revenue_metrics(self) -> Dict[str, Any]:
        metrics: Dict[str, Any] = {
            "total_users": 17,
            "active_today": 0,
            "active_users_7d": 0,
            "paid_users": 0,
            "premium_users": 0,
            "total_revenue_cents": 0,
            "mrr_cents_est": 0,
            "conversion_rate_pct": 0.0,
            "source": "fallback_pre_revenue_context",
        }

        # Prefer the admin API because that is the source the dashboard already trusts.
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    f"{API_BASE}/api/admin/insights",
                    headers={"X-Admin-Token": ADMIN_TOKEN},
                )
            if resp.status_code == 200:
                data = resp.json()
                metrics.update(
                    total_users=int(data.get("total_users", 0) or 0),
                    active_today=int(data.get("active_today", 0) or 0),
                    premium_users=int(data.get("premium_users", 0) or 0),
                    paid_users=int(data.get("premium_users", 0) or 0),
                    total_revenue_cents=int(data.get("total_revenue_cents", 0) or 0),
                    source="admin_api",
                )
        except Exception as exc:
            logger.warning("CGO admin metrics fetch failed: %s", exc)

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    f"{API_BASE}/api/admin/growth-metrics",
                    headers={"X-Admin-Token": ADMIN_TOKEN},
                )
            if resp.status_code == 200:
                data = resp.json()
                metrics["active_users_7d"] = int(data.get("active_users_7d", 0) or metrics["active_users_7d"])
                metrics["new_users_7d"] = int(data.get("new_users_7d", 0) or 0)
        except Exception as exc:
            logger.debug("CGO growth metrics fetch failed: %s", exc)

        if metrics["total_users"]:
            metrics["conversion_rate_pct"] = round(metrics["paid_users"] / metrics["total_users"] * 100, 2)
        metrics["mrr_cents_est"] = metrics["paid_users"] * 900
        return metrics

    async def _research_freemium_conversion(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        active = metrics.get("active_users_7d") or metrics.get("active_today") or 0
        paid = metrics.get("paid_users", 0)
        return {
            "opportunity": "High-engagement free users are the fastest path to first MRR.",
            "signal": f"{active} recently active users, {paid} paid users.",
            "next_steps": [
                "Query free users with >5 goal/session events in 14 days.",
                "Offer Explorer trial at the exact moment a user hits a limit or completes a meaningful goal.",
                "Add mission-framed upgrade copy: pay to sustain Ora for everyone, not to unlock artificial scarcity.",
            ],
            "expected_impact": "2-5 paid users can validate willingness-to-pay before paid acquisition.",
        }

    async def _research_grants(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "targets": ["Wellcome Trust", "Google.org", "Mozilla Builders", "AI for Good", "social enterprise grants"],
            "angle": "AI OS for mental clarity, fulfilment, personal agency, and public-good flourishing infrastructure.",
            "next_steps": [
                "Create a 2-page grant narrative with outcomes, safeguards, and evaluation metrics.",
                "Package anonymised fulfilment-score deltas once product telemetry supports it.",
                "Build a grant CRM with deadlines and fit score.",
            ],
            "expected_impact": "Non-dilutive runway; slower cycle but mission-aligned.",
        }

    async def _research_corporate_wellness(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "offer": "$8/seat/month, 10-seat minimum for workplace clarity and aligned-action support.",
            "ideal_customers": ["remote startups", "creator teams", "mission-driven non-profits", "conscious business communities"],
            "next_steps": [
                "Create one landing page for teams: clarity, goals, reflection, and burnout prevention.",
                "Offer a 30-day pilot to 3 founder-led teams.",
                "Add team-level onboarding and lightweight admin export before scaling.",
            ],
            "expected_impact": "One 25-seat team at $8/seat/month creates $200 MRR immediately.",
        }

    async def _research_api_access(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "offer": "$29/month developer tier with 10k calls; $99/month scale tier with 100k calls.",
            "buyers": ["indie hackers", "wellness apps", "coaches", "community platforms"],
            "next_steps": [
                "Expose a scoped API key flow and usage dashboard.",
                "Publish docs for goal coaching, reflection prompts, and fulfilment insights endpoints.",
                "Add metered overages only after clear usage notifications exist.",
            ],
            "expected_impact": "Fastest scalable stream once docs/key management exist.",
        }

    async def _research_community_membership(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "offer": "Founding Steward membership: monthly patronage, early roadmap voice, and public mission badge.",
            "guardrail": "No pay-to-win governance; CP acceleration must be capped and transparent.",
            "next_steps": [
                "Survey community willingness-to-pay for founding membership.",
                "Bundle office hours, behind-the-scenes builds, and premium reflection circles.",
            ],
            "expected_impact": "Good for mission believers; validate after core subscriptions and sessions.",
        }

    async def _research_ip_licensing(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "offer": "White-labelled Ora workflows for coaches, communities, and social enterprises.",
            "next_steps": [
                "Identify 10 aligned organisations already serving wellbeing or transformation audiences.",
                "Package a limited pilot license with brand controls and data protection terms.",
                "Price pilots at $500-$2,000/month depending on seat count and customisation.",
            ],
            "expected_impact": "High-ticket but requires stronger onboarding, compliance, and support capacity.",
        }

    def _prioritize_revenue_streams(self, metrics: Dict[str, Any], research: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            {
                "name": "Corporate wellness plan",
                "rank": 1,
                "why_now": "B2B can create meaningful MRR with one relationship while user count is still small.",
                "price": "$8/seat/month, minimum 10 seats",
                "research": research["b2b_corporate_wellness"],
            },
            {
                "name": "One-time Ora Sessions",
                "rank": 2,
                "why_now": "Immediate cash conversion without needing subscription habit formation.",
                "price": "$49 clarity session / $99 deep work session",
                "research": {"offer": "Premium 1:1 coaching session via Stripe Checkout."},
            },
            {
                "name": "Developer API tier",
                "rank": 3,
                "why_now": "Turns Ora's intelligence into a platform revenue stream with low marginal cost.",
                "price": "$29/month developer, $99/month scale",
                "research": research["api_access_monetization"],
            },
            {
                "name": "Grant pipeline",
                "rank": 4,
                "why_now": "High mission fit, but slow sales cycle.",
                "price": "Non-dilutive grants",
                "research": research["grants"],
            },
        ]

    async def _activate_top_streams(self, streams: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        activations: List[Dict[str, Any]] = []
        for stream in streams[:3]:
            try:
                if stream["name"] == "Corporate wellness plan":
                    checkout = await create_corporate_plan_checkout(
                        org_name="Founding Corporate Wellness Pilot",
                        seats=10,
                        contact_email="growth@connectome.app",
                    )
                elif stream["name"] == "One-time Ora Sessions":
                    checkout = await create_ora_session_payment("growth-demo", "clarity")
                elif stream["name"] == "Developer API tier":
                    checkout = await create_api_access_checkout("growth-demo", "developer")
                else:
                    checkout = {"configured": False, "checkout_url": None}
                activations.append({"stream": stream["name"], "checkout": checkout})
            except Exception as exc:
                logger.warning("CGO activation failed for %s: %s", stream["name"], exc)
                activations.append({"stream": stream["name"], "error": str(exc)})
        return activations

    def _build_action_plan(self, streams: List[Dict[str, Any]], metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            {
                "priority": 1,
                "action": "Close one corporate wellness pilot: target 10-25 seats at $8/seat/month.",
                "owner": "CGO + CMO",
                "next_step": "Create the team landing page and outbound list of 20 aligned founders.",
                "success_metric": "$80-$200 MRR from first B2B customer.",
            },
            {
                "priority": 2,
                "action": "Launch Ora Sessions as one-off paid coaching checkout.",
                "owner": "CGO + CPO",
                "next_step": "Add in-app CTA after deep reflective moments and on the upgrade page.",
                "success_metric": "3 paid sessions in 14 days.",
            },
            {
                "priority": 3,
                "action": "Ship Developer API paid tier with keys, docs, and 10k included calls.",
                "owner": "CGO + CTO",
                "next_step": "Add API key issuance, usage tracking, and docs for the first 3 endpoints.",
                "success_metric": "2 developer subscribers in 30 days.",
            },
            {
                "priority": 4,
                "action": "Convert high-engagement free users ethically at natural value moments.",
                "owner": "CGO + CMO",
                "next_step": "Segment free users by engagement and add mission-framed upgrade prompts.",
                "success_metric": "Free-to-paid conversion above 5% among active users.",
            },
        ]

    def _stage(self, metrics: Dict[str, Any]) -> str:
        if metrics.get("total_revenue_cents", 0) <= 0 and metrics.get("paid_users", 0) <= 0:
            return "pre-revenue: validate willingness-to-pay immediately"
        if metrics.get("mrr_cents_est", 0) < 100_000:
            return "early revenue: push to first $1k MRR"
        return "growth: scale validated channels"


async def run_cgo_growth_analysis() -> Dict[str, Any]:
    """Convenience entrypoint for routes, crons, and scripts."""
    agent = CGOAgent()
    return await agent.act()
