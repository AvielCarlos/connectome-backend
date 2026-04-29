"""
CGO Agent — Chief Growth Officer for Ascension Technologies / Ora.

Mandate: aggressively and creatively grow revenue while protecting the
non-profit mission. The CGO never assumes a surface is impossible to monetize;
it finds ethical, legally sound revenue architecture around genuine value.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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
    Ora's Chief Growth Officer for the entire Ascension Technologies ecosystem.

    Thinks like a startup growth hacker plus a mission-driven social enterprise:
    data-driven, commercial, fast-moving, and allergic to fake scarcity or
    deceptive billing. Revenue exists to make the human-flourishing mission
    durable.
    """

    ecosystem_thesis = (
        "We are Ascension Technologies. Connectome/iDo is ONE product. We also "
        "have The Scilence, AI music, community, consulting, grants, corporate "
        "clients, developer API access, Ora Sessions, DAO Founding Stewards, "
        "events/retreats, and white-label Ora licensing. Revenue serves human "
        "flourishing, not the reverse."
    )

    revenue_streams = [
        {
            "stream": "App subscriptions",
            "target": "iDo / Connectome users",
            "vehicle": "Stripe recurring subscriptions",
            "near_term_offer": "Explorer/Premium personal fulfilment plan",
        },
        {
            "stream": "Community membership",
            "target": "Ascension community",
            "vehicle": "Stripe recurring membership",
            "near_term_offer": "Founding Steward membership and reflection circles",
        },
        {
            "stream": "Corporate wellness",
            "target": "HR/wellness teams",
            "vehicle": "Stripe B2B checkout",
            "near_term_offer": "Ora for Teams pilot at $8/seat/month",
        },
        {
            "stream": "Developer API",
            "target": "AI/wellness builders",
            "vehicle": "Stripe API subscription",
            "near_term_offer": "$29/month developer tier",
        },
        {
            "stream": "Ora Sessions",
            "target": "Individuals",
            "vehicle": "Stripe one-off checkout",
            "near_term_offer": "$49 clarity session / $99 deep work session",
        },
        {
            "stream": "The Scilence",
            "target": "Readers, sci-fi fans, publishers, agents",
            "vehicle": "Pre-order page, publisher/agent outreach, self-publishing path",
            "near_term_offer": "Pitch deck + sample pages + preorder waitlist",
        },
        {
            "stream": "AI music",
            "target": "Streaming audiences, sync libraries, wellness creators",
            "vehicle": "DistroKid, sync licensing, creator collaborations",
            "near_term_offer": "Release cadence + playlist/sync outreach",
        },
        {
            "stream": "Grants",
            "target": "Non-profit funders",
            "vehicle": "Wellcome Trust, Google.org, Open Philanthropy, responsible AI grants",
            "near_term_offer": "2-page concept note with safeguards and outcomes",
        },
        {
            "stream": "Consulting/speaking",
            "target": "Organisations and conferences",
            "vehicle": "Direct outreach and paid engagements",
            "near_term_offer": "AI + consciousness keynote/workshop menu",
        },
        {
            "stream": "DAO Founding Stewards",
            "target": "Early believers",
            "vehicle": "Fiat-to-CP fast-track with transparent guardrails",
            "near_term_offer": "Founding Steward pledge/membership",
        },
        {
            "stream": "White-label Ora",
            "target": "Wellness/health/coaching companies",
            "vehicle": "B2B licensing",
            "near_term_offer": "$500-$2,000/month pilot license",
        },
        {
            "stream": "Events/retreats",
            "target": "Community members",
            "vehicle": "Eventbrite / Stripe tickets",
            "near_term_offer": "Small virtual salon before physical retreats",
        },
    ]

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
            "the_scilence": await self._research_the_scilence(metrics),
            "ai_music": await self._research_ai_music(metrics),
            "consulting_speaking": await self._research_consulting_speaking(metrics),
            "dao_founding_stewards": await self._research_dao_founding_stewards(metrics),
            "events_retreats": await self._research_events_retreats(metrics),
        }
        streams = self._prioritize_revenue_streams(metrics, research)
        activation = await self._activate_top_streams(streams)
        growth_plan = self.multi_stream_growth_plan(metrics, research)

        report = {
            "agent": self.name,
            "analyzed_at": now.isoformat(),
            "mandate": self.ecosystem_thesis,
            "ecosystem": self.revenue_streams,
            "metrics": metrics,
            "research": research,
            "prioritized_action_plan": self._build_action_plan(streams, metrics),
            "multi_stream_30_day_growth_plan": growth_plan,
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
            "Ascension Technologies ecosystem mandate active; "
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

    def multi_stream_growth_plan(
        self,
        metrics: Optional[Dict[str, Any]] = None,
        research: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Return a concrete 30-day plan across all Ascension revenue streams."""
        metrics = metrics or {}
        research = research or {}
        return {
            "principle": "Revenue serves human flourishing, not the reverse.",
            "context": self.ecosystem_thesis,
            "success_metrics_by_day_30": [
                "1 corporate wellness pilot in active conversation or checkout-ready",
                "10 qualified community membership leads and a Founding Steward offer drafted",
                "3 grant opportunities qualified with one concept note ready",
                "5 publishers/agents/media targets for The Scilence with personalised pitches",
                "1 consulting/speaking topic menu and 10 targeted outreach drafts",
                "Developer API and Ora Sessions checkout links verified",
            ],
            "week_1_foundation": [
                "Audit all existing Stripe checkout paths: app, sessions, API, corporate.",
                "Create/update target_streams.json for community, corporate, grants, media, consulting, and developer API.",
                "Draft The Scilence one-page pitch and sample-pages request flow.",
                "Draft Aviel's speaking/consulting topic menu: AI, consciousness, human flourishing, practical transformation.",
            ],
            "week_2_outreach": [
                "Send researched community invitations to 5-10 aligned organisers or creators.",
                "Pitch 5 corporate wellness pilot targets with a clear low-friction pilot offer.",
                "Qualify Wellcome Trust, Google.org, Open Philanthropy, and one responsible-AI grant for fit/deadlines.",
                "Contact 3 publishers/agents/journalists for The Scilence only after tailoring each pitch.",
            ],
            "week_3_conversion_assets": [
                "Add a lightweight Ascension membership/preorder waitlist page if not already live.",
                "Prepare corporate pilot FAQ: privacy, data boundaries, outcomes, pricing, cancellation.",
                "Package Ora API docs/examples for wellness/coaching builders.",
                "Define AI music release/sync workflow: DistroKid metadata, playlist targets, sync library list.",
            ],
            "week_4_close_and_learn": [
                "Follow up with every warm lead using one useful new artifact, not pressure.",
                "Convert one channel into money: corporate pilot, paid session, Founding Steward, or consulting discovery call.",
                "Review response rates by stream; cut weak channels and double down on the top two.",
                "Teach Ora the validated growth thesis and update the next 30-day plan.",
            ],
            "active_research_refs": sorted(research.keys()),
            "current_metrics_source": metrics.get("source", "unknown"),
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

    async def _research_the_scilence(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "asset": "The Scilence — sci-fi novel set in 2047 around BCI, human-AI merger, consciousness, and power.",
            "buyers": ["sci-fi readers", "literary agents", "speculative fiction publishers", "tech/culture media"],
            "next_steps": [
                "Create a one-page pitch with comparable titles, core hook, audience, and author platform.",
                "Prepare sample pages and a preorder/waitlist page before broad pitching.",
                "Research 20 agents/publishers that accept speculative fiction touching AI and consciousness.",
            ],
            "expected_impact": "Longer-cycle IP upside; can grow audience and credibility even before a deal.",
        }

    async def _research_ai_music(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "asset": "Avi's AI-generated music catalogue distributed via DistroKid.",
            "buyers": ["streaming listeners", "wellness creators", "sync libraries", "meditation communities"],
            "next_steps": [
                "Establish a consistent release cadence and metadata strategy.",
                "Build a playlist and sync-licensing target list for cinematic, wellness, and consciousness-adjacent use cases.",
                "Bundle selected tracks into Ascension community moments, meditations, and launch content.",
            ],
            "expected_impact": "Small near-term streaming revenue; larger brand/audience and sync optionality.",
        }

    async def _research_consulting_speaking(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "offer": "Aviel as AI + consciousness + human flourishing speaker/consultant.",
            "buyers": ["AI conferences", "future-of-work events", "corporate L&D", "conscious business communities"],
            "next_steps": [
                "Create a short topic menu and bio in Aviel's voice.",
                "Research 25 programme leads with explicit fit before outreach.",
                "Offer one paid workshop format and one keynote format with clear outcomes.",
            ],
            "expected_impact": "High-margin revenue and authority building if positioned selectively.",
        }

    async def _research_dao_founding_stewards(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "offer": "DAO Founding Steward fast-track for early believers who want to fund the mission.",
            "guardrails": [
                "Fiat-to-CP mechanics must be transparent and capped.",
                "No pay-to-win governance or misleading investment framing.",
                "Use plain-language terms before accepting payments.",
            ],
            "next_steps": [
                "Draft Founding Steward tiers around contribution, recognition, and participation.",
                "Invite only aligned early believers first; avoid public hype until governance language is reviewed.",
            ],
            "expected_impact": "Mission-aligned early cash if trust and legal clarity are protected.",
        }

    async def _research_events_retreats(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "offer": "Ascension salons, workshops, and eventually retreats for community members.",
            "sequence": ["free/low-cost virtual salon", "paid workshop", "small in-person retreat only after demand is proven"],
            "next_steps": [
                "Run one virtual salon around AI, consciousness, and designing a flourishing life.",
                "Use Stripe/Eventbrite for paid workshops once attendance is validated.",
                "Collect testimonials and learning signals, not just ticket revenue.",
            ],
            "expected_impact": "Community activation and modest revenue; high mission fit if intimate and sincere.",
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
            {
                "name": "Community membership / Founding Stewards",
                "rank": 5,
                "why_now": "The mission needs believers as well as users; membership can fund early work without pretending the app is finished.",
                "price": "$11-$49/month membership or transparent steward pledge",
                "research": research["community_membership"],
            },
            {
                "name": "Consulting and speaking",
                "rank": 6,
                "why_now": "Aviel's thought leadership can create high-margin revenue and attract aligned partners.",
                "price": "Paid workshops/keynotes/consulting",
                "research": research["consulting_speaking"],
            },
            {
                "name": "The Scilence publishing/media pipeline",
                "rank": 7,
                "why_now": "Narrative IP can build audience, cultural gravity, and future publishing revenue.",
                "price": "Preorders, publishing deal, or self-publish revenue",
                "research": research["the_scilence"],
            },
            {
                "name": "AI music catalogue",
                "rank": 8,
                "why_now": "Music can compound brand atmosphere and small passive revenue while supporting the movement.",
                "price": "Streaming and sync licensing",
                "research": research["ai_music"],
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
            {
                "priority": 5,
                "action": "Open the Ascension community membership / Founding Steward path carefully.",
                "owner": "CGO + Community",
                "next_step": "Draft membership tiers and invite 10 aligned early believers personally.",
                "success_metric": "3 members or stewards expressing paid intent in 30 days.",
            },
            {
                "priority": 6,
                "action": "Package Aviel's AI + consciousness consulting/speaking offer.",
                "owner": "CGO + Avi",
                "next_step": "Create topic menu, bio, and 20 targeted conference/L&D contacts.",
                "success_metric": "2 discovery calls booked.",
            },
            {
                "priority": 7,
                "action": "Create The Scilence pitch pipeline for publishers, agents, and tech-culture media.",
                "owner": "CGO + Editorial",
                "next_step": "Prepare one-page pitch, sample-pages packet, and 20 researched targets.",
                "success_metric": "5 personalised pitches sent and tracked.",
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
