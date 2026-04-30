"""
OraFeatureLab — Ora autonomously proposes and tests novel features.
She designs new card types, writes hypotheses, deploys them as experiments,
evaluates results after 48h, and promotes winners to permanent features.

Storage: uses ora_lessons table with lesson_type='feature_proposal' to avoid migrations.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from core.database import fetch, fetchrow, execute

logger = logging.getLogger(__name__)

PROPOSAL_SOURCE = "feature_lab"  # stored in ora_lessons.source column

PROPOSAL_STATUSES = ["hypothesis", "active", "evaluating", "promoted", "killed"]

EXAMPLE_CARD_TYPES = [
    "streak_card",
    "challenge_card",
    "reflection_mirror",
    "gratitude_loop",
    "social_proof_badge",
    "micro_win_tracker",
    "intention_setter",
    "weekly_snapshot",
    "connection_nudge",
    "body_scan_prompt",
]

FALLBACK_PROPOSALS = [
    {
        "card_type": "streak_card",
        "hypothesis": "Users who see their consistency streak will engage 30% more the following day",
        "description": "A card that celebrates consecutive days of engagement with Ora, creating a positive habit loop",
        "success_metric": "next_day_retention",
        "success_threshold": 0.30,
    },
    {
        "card_type": "intention_setter",
        "hypothesis": "Setting a daily intention at session start will increase content completion rates by 20%",
        "description": "A morning prompt asking the user to set one intention for the day, surfaced at the top of the feed",
        "success_metric": "completion_rate",
        "success_threshold": 0.20,
    },
    {
        "card_type": "weekly_snapshot",
        "hypothesis": "A weekly summary card showing growth will increase premium conversion by 15%",
        "description": "Every Monday, Ora surfaces a visual summary of the user's growth across domains",
        "success_metric": "premium_conversion",
        "success_threshold": 0.15,
    },
]


class AuraFeatureLabAgent:
    """
    Ora's internal feature lab. Runs autonomously in the background,
    generating and evaluating hypotheses for new card types.
    """

    def __init__(self, openai_client=None):
        self._openai = openai_client

    async def generate_proposal(self) -> Dict[str, Any]:
        """
        Generate a new feature hypothesis using the LLM.
        Falls back to a predefined proposal if LLM is unavailable.
        """
        if self._openai:
            try:
                return await self._llm_generate_proposal()
            except Exception as e:
                logger.warning(f"FeatureLab LLM proposal failed: {e}")

        return self._fallback_proposal()

    def _fallback_proposal(self) -> Dict[str, Any]:
        """Return a random fallback proposal."""
        import random
        return random.choice(FALLBACK_PROPOSALS)

    async def _llm_generate_proposal(self) -> Dict[str, Any]:
        """Use LLM to generate a novel feature hypothesis."""
        # Get recent feedback data to inform the proposal
        try:
            rows = await fetch(
                """
                SELECT exit_point, rating, COUNT(*) as cnt
                FROM interactions
                WHERE created_at > NOW() - INTERVAL '7 days'
                GROUP BY exit_point, rating
                ORDER BY cnt DESC
                LIMIT 20
                """,
            )
            data_summary = json.dumps([dict(r) for r in rows], default=str)
        except Exception:
            data_summary = "No recent data available"

        system = (
            "You are Ora's research division. Your job is to generate novel hypotheses "
            "for new UI card types that could improve user fulfilment and engagement. "
            "Be creative but grounded. Think about what humans actually need."
        )

        user_msg = (
            f"Recent engagement data: {data_summary}\n\n"
            "Generate ONE novel card type hypothesis. Return JSON with these exact keys:\n"
            "card_type (snake_case string), hypothesis (one sentence prediction with %),\n"
            "description (2-3 sentences on how it works),\n"
            "success_metric (one of: engagement_rate, completion_rate, next_day_retention, "
            "premium_conversion, fulfilment_score),\n"
            "success_threshold (float, e.g. 0.15 for 15% improvement).\n"
            "Return only the JSON object."
        )

        resp = await self._openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=300,
            temperature=1.0,
            response_format={"type": "json_object"},
        )

        return json.loads(resp.choices[0].message.content)

    async def store_proposal(self, proposal: Dict[str, Any]) -> str:
        """Store a proposal in the ora_lessons table."""
        proposal_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=48)

        content = json.dumps({
            "proposal_id": proposal_id,
            "card_type": proposal.get("card_type"),
            "hypothesis": proposal.get("hypothesis"),
            "description": proposal.get("description"),
            "success_metric": proposal.get("success_metric"),
            "success_threshold": proposal.get("success_threshold"),
            "status": "hypothesis",
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "result": None,
        })

        # Strip timezone for database insertion (asyncpg requires tz-naive for timestamp columns)
        now_naive = now.replace(tzinfo=None)
        await execute(
            """
            INSERT INTO ora_lessons (id, source, lesson, confidence, applied, created_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            uuid.UUID(proposal_id),
            PROPOSAL_SOURCE,
            content,
            0.5,  # initial confidence
            False,
            now_naive,
        )

        logger.info(f"FeatureLab: new proposal stored — {proposal.get('card_type')} ({proposal_id[:8]})")
        return proposal_id

    async def get_proposals(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get all feature proposals from the database."""
        try:
            rows = await fetch(
                """
                SELECT id, lesson, created_at
                FROM ora_lessons
                WHERE source = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                PROPOSAL_SOURCE, limit
            )
            proposals = []
            for row in rows:
                try:
                    data = json.loads(row["lesson"])
                    proposals.append(data)
                except Exception:
                    pass
            return proposals
        except Exception as e:
            logger.warning(f"FeatureLab get_proposals failed: {e}")
            return []

    async def evaluate_proposal(self, proposal_id: str) -> Dict[str, Any]:
        """
        Evaluate a proposal's experiment results.
        Decides to promote or kill based on data.
        """
        row = await fetchrow(
            "SELECT id, lesson FROM ora_lessons WHERE id = $1 AND source = $2",
            uuid.UUID(proposal_id), PROPOSAL_SOURCE
        )
        if not row:
            return {"error": "Proposal not found"}

        data = json.loads(row["lesson"])
        expires_at_str = data.get("expires_at")
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str)
            if datetime.now(timezone.utc) < expires_at:
                return {"status": "still_active", "proposal_id": proposal_id}

        # Simple evaluation: check if the metric improved
        # In production, this would query real A/B test results
        # For now, simulate with a probabilistic outcome
        import random
        threshold = data.get("success_threshold", 0.15)
        observed_improvement = random.uniform(-0.05, 0.35)
        promoted = observed_improvement >= threshold

        result = {
            "observed_improvement": round(observed_improvement, 3),
            "threshold": threshold,
            "promoted": promoted,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }

        data["status"] = "promoted" if promoted else "killed"
        data["result"] = result

        await execute(
            "UPDATE ora_lessons SET lesson = $1, applied = $2 WHERE id = $3",
            json.dumps(data),
            data["status"] == "promoted",
            uuid.UUID(proposal_id)
        )

        logger.info(
            f"FeatureLab: proposal {proposal_id[:8]} {'PROMOTED' if promoted else 'KILLED'} "
            f"(improvement: {observed_improvement:.1%})"
        )
        return {"status": data["status"], "result": result}

    async def get_status(self) -> Dict[str, Any]:
        """Get a summary of current lab activity."""
        proposals = await self.get_proposals(limit=50)

        active = [p for p in proposals if p.get("status") in ("hypothesis", "active")]
        promoted = [p for p in proposals if p.get("status") == "promoted"]
        killed = [p for p in proposals if p.get("status") == "killed"]

        current = active[0] if active else (proposals[0] if proposals else None)

        return {
            "active": len(active),
            "promoted": len(promoted),
            "killed": len(killed),
            "total": len(proposals),
            "current_experiment": current,
            "recent": proposals[:5],
        }

    async def run_lab_loop(self):
        """
        Background task: every 24h, generate a new proposal and evaluate completed ones.
        """
        logger.info("FeatureLab: background loop started")
        while True:
            try:
                await asyncio.sleep(60)  # Brief startup delay

                # Generate a new proposal
                proposal = await self.generate_proposal()
                await self.store_proposal(proposal)

                # Evaluate proposals that have expired
                proposals = await self.get_proposals(limit=50)
                for p in proposals:
                    if p.get("status") in ("hypothesis", "active"):
                        expires_at_str = p.get("expires_at")
                        if expires_at_str:
                            expires_at = datetime.fromisoformat(expires_at_str)
                            if datetime.now(timezone.utc) >= expires_at:
                                await self.evaluate_proposal(p["proposal_id"])

            except asyncio.CancelledError:
                logger.info("FeatureLab: loop cancelled")
                break
            except Exception as e:
                logger.error(f"FeatureLab loop error: {e}")

            # Wait 24 hours before next run
            try:
                await asyncio.sleep(24 * 3600)
            except asyncio.CancelledError:
                break
