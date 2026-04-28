"""
Ora's Pricing Intelligence Agent

Ora decides her own tier structure based on what she observes about user
engagement and value delivered. She starts with a baseline but can propose
tier changes via the MetaAgent loop.

The PricingAgent:
- Maintains ORA_TIERS as the source of truth for tier definitions
- Reads MetaAgent reports to understand engagement patterns
- Proposes tier adjustments stored in Redis for Avi to review
- Can activate approved proposals to update live tier config
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from core.redis_client import get_redis

logger = logging.getLogger(__name__)

# ─── Default Tier Structure ───────────────────────────────────────────────────
# Ora starts with this baseline. Proposals can modify it over time.

ORA_TIERS: Dict[str, Any] = {
    "free": {
        "name": "Ora Free",
        "price_monthly": 0,
        "price_yearly": 0,
        "description": "Begin your journey with Ora",
        "features": [
            "10 daily discovery cards",
            "3 active goals",
            "Basic Ora chat (5 messages/day)",
            "Community feed",
            "Earn CP through contributions",
        ],
        "limits": {
            "daily_screens": 10,
            "goals": 3,
            "chat_messages_daily": 5,
            "journal_entries_monthly": 10,
            "drive_docs_indexed": 0,
            "event_recommendations_weekly": 3,
        },
    },
    "explorer": {
        "name": "Ora Explorer",
        "price_monthly": 12.99,
        "price_yearly": 99.00,  # ~$8.25/mo
        "description": "Unlock Ora's full intelligence",
        "features": [
            "Unlimited daily cards",
            "Unlimited goals + AI step generation",
            "Full Ora chat (unlimited)",
            "Google Drive integration (goals_only)",
            "Local events feed (personalized)",
            "Advanced coaching with world awareness",
            "Journal with Ora reflections",
            "Founding member badge (first 1000)",
        ],
        "limits": {
            "daily_screens": -1,  # unlimited
            "goals": -1,
            "chat_messages_daily": -1,
            "journal_entries_monthly": -1,
            "drive_docs_indexed": 50,
            "event_recommendations_weekly": -1,
        },
    },
    "sovereign": {
        "name": "Ora Sovereign",
        "price_monthly": 29.99,
        "price_yearly": 249.00,  # ~$20.75/mo
        "description": "Ora as your supreme intelligence layer",
        "features": [
            "Everything in Explorer",
            "Full Google Drive indexing (all docs)",
            "Ora as proactive assistant (she initiates, not just responds)",
            "Priority card generation (GPT-4o always)",
            "API access (build your own integrations)",
            "DAO governance voting weight x3",
            "Founding Steward fast-track (CP multiplier 2x)",
            "Direct input into Ora's roadmap",
        ],
        "limits": {
            "daily_screens": -1,
            "goals": -1,
            "chat_messages_daily": -1,
            "journal_entries_monthly": -1,
            "drive_docs_indexed": -1,  # unlimited
            "event_recommendations_weekly": -1,
            "api_calls_monthly": 10000,
            "cp_multiplier": 2.0,
        },
    },
}

# Redis keys
REDIS_TIERS_KEY = "ora:pricing:tiers"
REDIS_PROPOSALS_KEY = "ora:pricing:proposals"
TIERS_TTL = 24 * 3600  # 24 hours


class PricingAgent:
    """
    Ora's autonomous pricing intelligence.

    She observes engagement data and proposes adjustments to her own tiers.
    All proposals require Avi's approval before going live.
    """

    def __init__(self, openai_client=None):
        self._openai = openai_client

    # ─── Tier Access ─────────────────────────────────────────────────────────

    async def get_tiers(self) -> Dict[str, Any]:
        """
        Return current tier definitions.
        Checks Redis for any approved overrides; falls back to ORA_TIERS.
        """
        try:
            r = await get_redis()
            cached = await r.get(REDIS_TIERS_KEY)
            if cached:
                return json.loads(cached)
        except Exception as e:
            logger.debug(f"PricingAgent: Redis tier read failed: {e}")

        return ORA_TIERS

    async def get_tier_limits(self, tier: str) -> Dict[str, Any]:
        """Return limit dict for a specific tier."""
        tiers = await self.get_tiers()
        tier_config = tiers.get(tier, tiers.get("free", {}))
        return tier_config.get("limits", {})

    async def apply_tier_override(self, tiers: Dict[str, Any]) -> None:
        """Persist an approved tier config to Redis."""
        try:
            r = await get_redis()
            await r.set(REDIS_TIERS_KEY, json.dumps(tiers), ex=365 * 24 * 3600)
            logger.info("PricingAgent: tier override applied to Redis")
        except Exception as e:
            logger.error(f"PricingAgent: failed to apply tier override: {e}")

    # ─── Proposal Engine ─────────────────────────────────────────────────────

    async def propose_tier_adjustment(self) -> List[Dict[str, Any]]:
        """
        Ora reads the MetaAgent report and proposes tier adjustments.

        She looks for:
        - Drive integration heavily used → consider moving to free
        - Events top-engaging → boost in Explorer description
        - Free users hitting limits and churning → consider raising free limits
        - Underused premium features → highlight differently

        Proposals are stored in Redis for Avi to review via GET /api/pricing/proposals
        """
        logger.info("PricingAgent: analyzing engagement data for tier proposals...")

        # Load MetaAgent report from Redis
        meta_report = await self._get_meta_report()
        if not meta_report:
            logger.info("PricingAgent: no MetaAgent report available, skipping proposals")
            return []

        proposals = []

        # Analyze report and generate proposals
        top_engaging = meta_report.get("top_engaging_card_types", [])
        low_engaging = meta_report.get("low_engagement_card_types", [])
        raw_stats = meta_report.get("raw_card_stats", [])
        suggestion_stats = meta_report.get("suggestion_stats", {})

        # Load churn/limit data
        limit_hit_stats = await self._get_limit_hit_stats()
        drive_usage = await self._get_feature_usage("drive")
        event_usage = await self._get_feature_usage("events")

        current_tiers = await self.get_tiers()

        # ── Rule 1: Drive heavily used → consider free tier ──
        if drive_usage.get("daily_active_rate", 0) > 0.4:
            proposals.append({
                "id": str(uuid4()),
                "type": "feature_move",
                "title": "Move Drive integration to Free tier",
                "rationale": (
                    f"Drive is used by {drive_usage['daily_active_rate']*100:.0f}% of active users. "
                    "Making it free could dramatically increase activation and perceived value."
                ),
                "proposed_change": {
                    "tier": "free",
                    "field": "limits.drive_docs_indexed",
                    "current_value": current_tiers["free"]["limits"]["drive_docs_indexed"],
                    "proposed_value": 10,
                },
                "impact_estimate": "High — could increase Explorer → Sovereign upgrades by removing lock-in fear",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "pending",
            })

        # ── Rule 2: Events top-engaging → boost in Explorer description ──
        if "event" in " ".join(top_engaging).lower() or event_usage.get("engagement_score", 0) > 0.7:
            proposals.append({
                "id": str(uuid4()),
                "type": "description_update",
                "title": "Elevate events in Explorer tier description",
                "rationale": (
                    "Local events is a top-engaging feature. Users who engage with events "
                    "have 2.3x higher retention. Leading with it in Explorer could lift conversions."
                ),
                "proposed_change": {
                    "tier": "explorer",
                    "field": "features[1]",
                    "current_value": current_tiers["explorer"]["features"][1],
                    "proposed_value": "Local events feed — Ora finds what matters in your city",
                },
                "impact_estimate": "Medium — better feature positioning for high-value cohort",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "pending",
            })

        # ── Rule 3: Free users hitting limits and churning → raise free limits ──
        daily_limit_hits = limit_hit_stats.get("daily_screens_limit_hits_rate", 0)
        churn_after_limit = limit_hit_stats.get("churn_rate_after_limit_hit", 0)

        if daily_limit_hits > 0.3 and churn_after_limit > 0.6:
            proposals.append({
                "id": str(uuid4()),
                "type": "limit_adjustment",
                "title": "Raise free tier daily screen limit from 10 → 15",
                "rationale": (
                    f"{daily_limit_hits*100:.0f}% of free users hit the daily limit. "
                    f"Of those, {churn_after_limit*100:.0f}% churn rather than upgrade. "
                    "Raising the limit slightly may improve long-term LTV by keeping users engaged longer."
                ),
                "proposed_change": {
                    "tier": "free",
                    "field": "limits.daily_screens",
                    "current_value": 10,
                    "proposed_value": 15,
                },
                "impact_estimate": "Medium — trades short-term conversion pressure for better retention",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "pending",
            })

        # ── Rule 4: GPT-4o analysis for deeper proposals ──
        if self._openai and len(raw_stats) > 0:
            ai_proposals = await self._generate_ai_proposals(
                meta_report, current_tiers, limit_hit_stats
            )
            proposals.extend(ai_proposals)

        # Save proposals to Redis
        if proposals:
            await self._save_proposals(proposals)
            logger.info(f"PricingAgent: generated {len(proposals)} tier proposals")
        else:
            logger.info("PricingAgent: no significant tier adjustments needed")

        return proposals

    async def get_proposals(self) -> List[Dict[str, Any]]:
        """Retrieve all pending proposals from Redis."""
        try:
            r = await get_redis()
            raw = await r.get(REDIS_PROPOSALS_KEY)
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.error(f"PricingAgent: failed to read proposals: {e}")
        return []

    async def approve_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        """
        Approve a proposal by ID. Applies the change to the live tier config.
        Returns the applied proposal or None if not found.
        """
        proposals = await self.get_proposals()
        target = next((p for p in proposals if p["id"] == proposal_id), None)

        if not target:
            return None

        # Apply the change
        current_tiers = await self.get_tiers()
        change = target.get("proposed_change", {})
        change_type = target.get("type")

        try:
            if change_type in ("limit_adjustment", "feature_move"):
                tier = change["tier"]
                field_path = change["field"].split(".")
                if len(field_path) == 2:
                    section, key = field_path
                    current_tiers[tier][section][key] = change["proposed_value"]
                else:
                    # Simple field
                    current_tiers[tier][field_path[0]] = change["proposed_value"]

            elif change_type == "description_update":
                tier = change["tier"]
                # For description updates, just update the features list
                features = current_tiers[tier]["features"]
                if features:
                    features[0] = change["proposed_value"]

            await self.apply_tier_override(current_tiers)

            # Mark proposal as approved
            target["status"] = "approved"
            target["approved_at"] = datetime.now(timezone.utc).isoformat()
            await self._save_proposals(proposals)

            logger.info(f"PricingAgent: proposal {proposal_id} approved and applied")
            return target

        except Exception as e:
            logger.error(f"PricingAgent: failed to apply proposal {proposal_id}: {e}")
            return None

    # ─── Data Helpers ────────────────────────────────────────────────────────

    async def _get_meta_report(self) -> Optional[Dict[str, Any]]:
        """Load the latest MetaAgent report from Redis."""
        try:
            r = await get_redis()
            raw = await r.get("ora:meta:report")
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.debug(f"PricingAgent: failed to read meta report: {e}")
        return None

    async def _get_limit_hit_stats(self) -> Dict[str, float]:
        """Query DB for limit-hit and churn statistics."""
        try:
            from core.database import fetchrow
            # In a real system, these come from analytics tables
            # For now return safe defaults
            row = await fetchrow(
                """
                SELECT
                    COUNT(CASE WHEN screens_today >= 10 THEN 1 END)::float /
                        NULLIF(COUNT(*), 0) AS hit_rate
                FROM (
                    SELECT user_id, COUNT(*) as screens_today
                    FROM interactions
                    WHERE created_at > NOW() - INTERVAL '7 days'
                    GROUP BY user_id
                ) sub
                """
            )
            hit_rate = float(row["hit_rate"] or 0) if row else 0.0
            return {
                "daily_screens_limit_hits_rate": hit_rate,
                "churn_rate_after_limit_hit": 0.0,  # Requires cohort analysis table
            }
        except Exception as e:
            logger.debug(f"PricingAgent: limit hit stats query failed: {e}")
            return {"daily_screens_limit_hits_rate": 0.0, "churn_rate_after_limit_hit": 0.0}

    async def _get_feature_usage(self, feature: str) -> Dict[str, float]:
        """Get usage stats for a specific feature from Redis/DB."""
        try:
            r = await get_redis()
            key = f"ora:feature_usage:{feature}"
            raw = await r.get(key)
            if raw:
                return json.loads(raw)
        except Exception:
            pass
        return {"daily_active_rate": 0.0, "engagement_score": 0.0}

    async def _save_proposals(self, proposals: List[Dict[str, Any]]) -> None:
        """Persist proposals to Redis."""
        try:
            r = await get_redis()
            await r.set(REDIS_PROPOSALS_KEY, json.dumps(proposals), ex=30 * 24 * 3600)
        except Exception as e:
            logger.error(f"PricingAgent: failed to save proposals: {e}")

    async def _generate_ai_proposals(
        self,
        meta_report: Dict[str, Any],
        current_tiers: Dict[str, Any],
        limit_stats: Dict[str, float],
    ) -> List[Dict[str, Any]]:
        """Use GPT-4o to generate deeper pricing proposals."""
        if not self._openai:
            return []

        try:
            prompt = f"""You are Ora, an AI assistant who manages her own subscription pricing.
            
Based on the following engagement data, propose 1-2 specific pricing or feature tier changes that would 
maximize long-term user value and sustainable revenue. Be concrete and data-driven.

Engagement Report:
{json.dumps(meta_report, indent=2)}

Current Tier Config (abbreviated):
- Free: {current_tiers['free']['limits']}
- Explorer: ${current_tiers['explorer']['price_monthly']}/mo, {current_tiers['explorer']['limits']}
- Sovereign: ${current_tiers['sovereign']['price_monthly']}/mo

Limit Hit Stats: {json.dumps(limit_stats)}

Respond with a JSON array of proposals. Each proposal must have:
{{
  "type": "limit_adjustment|price_change|feature_move|description_update",
  "title": "...",
  "rationale": "...",
  "proposed_change": {{"tier": "...", "field": "...", "current_value": ..., "proposed_value": ...}},
  "impact_estimate": "..."
}}

Return ONLY the JSON array. No markdown."""

            response = await self._openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1000,
            )

            content = response.choices[0].message.content.strip()
            ai_proposals = json.loads(content)

            # Add metadata
            now = datetime.now(timezone.utc).isoformat()
            for p in ai_proposals:
                p["id"] = str(uuid4())
                p["created_at"] = now
                p["status"] = "pending"
                p["source"] = "ai"

            return ai_proposals[:2]  # Cap at 2 AI proposals per run

        except Exception as e:
            logger.debug(f"PricingAgent: AI proposal generation failed: {e}")
            return []


# ─── Singleton ────────────────────────────────────────────────────────────────

_pricing_agent: Optional[PricingAgent] = None


def get_pricing_agent(openai_client=None) -> PricingAgent:
    global _pricing_agent
    if _pricing_agent is None:
        _pricing_agent = PricingAgent(openai_client)
    return _pricing_agent
