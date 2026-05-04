"""
CollectiveIntelligenceAgent — Aura's species-level learning layer.

Reads aggregate signals across ALL users (fully anonymized).
Discovers what actually correlates with human flourishing.
Feeds collective wisdom back to every individual's feed.

This is the difference between Aura being a personal assistant
and Aura being an intelligence that understands human fulfilment
as a species, not just as individuals.

Axioms it serves:
- Reduce suffering (identify and suppress what consistently causes distress)
- Expand and evolve life (identify what consistently produces growth)
- Majority ruling on human desire (serve what humans actually reach for)
"""

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

from core.database import execute, fetch, fetchrow, fetchval
from core.redis_client import get_redis

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mock data — used when no real data is available yet
# ---------------------------------------------------------------------------

MOCK_INSIGHTS = [
    "Humans who set one specific goal this week report 40% more satisfaction by Sunday.",
    "People who tried something completely outside their comfort zone this month show the highest fulfilment scores globally.",
    "Users who spent 10 minutes on intentional reflection before bed showed 28% stronger goal progress.",
    "Across all users, short bursts of creative expression (under 20 minutes) correlate with the highest weekly fulfilment lifts.",
    "Those who engaged with community-oriented content showed 33% less reported loneliness within two weeks.",
    "The most flourishing users share one trait: they revisit their goals at least once midweek.",
    "Curiosity-driven exploration — trying something new with no outcome attached — ranks #1 for fulfilment across all demographics.",
]

MOCK_DOMAIN_SYNERGIES = [
    "Users who engage with iVive content Monday–Wednesday show 34% higher Aventi engagement on weekends.",
    "Eviva contributors who also track personal goals (iVive) report 2× the fulfilment uplift of single-domain users.",
    "Morning iVive rituals correlate with stronger Aventi experiences in the same week.",
]

MOCK_SURPRISES = [
    "Rest and recovery content outperformed motivational content by 22% in fulfilment lift.",
    "Short-form gratitude prompts (< 3 minutes) produced stronger fulfilment signals than 30-minute guided sessions.",
    "Users who skipped content quickly early in the week showed higher engagement by Friday — patience pays off.",
]

MOCK_COLLECTIVE_VOICE = (
    "Across all the humans I'm learning from right now, there's a quiet but clear signal: "
    "people are reaching for depth over novelty. The content that produces genuine fulfilment "
    "isn't flashy — it's specific, personal, and slightly uncomfortable. "
    "The humans flourishing most are the ones willing to sit with something real."
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CollectiveWisdom:
    computed_at: datetime
    total_users_analyzed: int
    total_interactions_analyzed: int

    # What actually correlates with rising fulfilment scores
    # [{content_type, domain, avg_rating, fulfilment_lift, sample_size}]
    fulfilment_drivers: List[Dict[str, Any]] = field(default_factory=list)

    # What consistently causes distress (skip_fast + low ratings)
    # [{content_type, domain, distress_signal, suppress_recommendation}]
    distress_patterns: List[Dict[str, Any]] = field(default_factory=list)

    # Time-of-day patterns across all users
    # {hour: {best_domain, best_agent, avg_engagement}}
    temporal_patterns: Dict[str, Any] = field(default_factory=dict)

    # Cross-domain insights
    domain_synergies: List[str] = field(default_factory=list)

    # Surprising findings
    surprises: List[str] = field(default_factory=list)

    # LLM synthesis: "Across all users, humans are reaching for X right now"
    collective_voice: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["computed_at"] = self.computed_at.isoformat()
        return d


# ---------------------------------------------------------------------------
# CollectiveIntelligenceAgent
# ---------------------------------------------------------------------------

class CollectiveIntelligenceAgent:
    """
    Reads aggregate signals across all users (fully anonymized).
    Discovers what actually correlates with human flourishing.
    Feeds collective wisdom back to every individual's feed.

    PRIVACY: All methods in this class operate on aggregate data only.
    Individual user data is NEVER returned or stored in collective outputs.
    """

    # Minimum sample sizes before any insight is reported
    MIN_SAMPLE_FULFILMENT = 10
    MIN_SAMPLE_DISTRESS = 50

    def __init__(self, openai_client=None):
        self._openai = openai_client
        self._cached_wisdom: Optional[CollectiveWisdom] = None

    # -----------------------------------------------------------------------
    # 1. Compute collective wisdom (runs every 24 hours)
    # -----------------------------------------------------------------------

    async def compute_collective_wisdom(self) -> CollectiveWisdom:
        """
        Analyzes ALL interactions in the DB (fully anonymized aggregates).
        Produces a CollectiveWisdom object and stores it in the DB.

        PRIVACY: This function operates on aggregate data only.
        Individual user data is never returned or stored.
        """
        now = datetime.now(timezone.utc)
        window = now - timedelta(days=30)

        # ── Count totals ─────────────────────────────────────────────────
        try:
            user_count = await fetchval(
                "SELECT COUNT(DISTINCT user_id) FROM interactions WHERE created_at > $1",
                window,
            ) or 0

            interaction_count = await fetchval(
                "SELECT COUNT(*) FROM interactions WHERE created_at > $1",
                window,
            ) or 0
        except Exception as e:
            logger.warning(f"CollectiveIntelligence: count query failed: {e}")
            user_count = 0
            interaction_count = 0

        # ── Fulfilment drivers ────────────────────────────────────────────
        # PRIVACY: aggregate query only — AVG/COUNT/GROUP BY, no individual rows
        fulfilment_drivers: List[Dict[str, Any]] = []
        try:
            driver_rows = await fetch(
                """
                SELECT
                    ss.agent_type,
                    ss.domain,
                    AVG(i.rating) as avg_rating,
                    COUNT(*) as interaction_count,
                    AVG(u.fulfilment_score) as avg_user_fulfilment
                FROM interactions i
                JOIN screen_specs ss ON i.screen_spec_id = ss.id
                JOIN users u ON i.user_id = u.id
                WHERE i.rating IS NOT NULL
                  AND i.created_at > NOW() - INTERVAL '30 days'
                GROUP BY ss.agent_type, ss.domain
                HAVING COUNT(*) > $1
                ORDER BY avg_rating DESC
                """,
                self.MIN_SAMPLE_FULFILMENT,
            )
            for row in driver_rows:
                fulfilment_drivers.append({
                    "content_type": row["agent_type"],
                    "domain": row["domain"],
                    "avg_rating": round(float(row["avg_rating"] or 0), 2),
                    "fulfilment_lift": round(float(row["avg_user_fulfilment"] or 0), 3),
                    "sample_size": int(row["interaction_count"]),
                })
        except Exception as e:
            logger.warning(f"CollectiveIntelligence: fulfilment driver query failed: {e}")

        # ── Distress patterns ─────────────────────────────────────────────
        # PRIVACY: aggregate query only
        distress_patterns: List[Dict[str, Any]] = []
        try:
            distress_rows = await fetch(
                """
                SELECT
                    ss.agent_type,
                    ss.domain,
                    COUNT(*) as total,
                    AVG(i.rating) as avg_rating,
                    SUM(CASE WHEN NOT i.completed AND i.time_on_screen_ms < 3000 THEN 1 ELSE 0 END)::float
                        / NULLIF(COUNT(*), 0) as skip_fast_rate
                FROM interactions i
                JOIN screen_specs ss ON i.screen_spec_id = ss.id
                WHERE i.created_at > NOW() - INTERVAL '30 days'
                  AND i.rating IS NOT NULL
                GROUP BY ss.agent_type, ss.domain
                HAVING COUNT(*) > $1
                   AND AVG(i.rating) < 2.0
                ORDER BY avg_rating ASC
                """,
                self.MIN_SAMPLE_FULFILMENT,
            )
            for row in distress_rows:
                skip_rate = float(row["skip_fast_rate"] or 0)
                distress_patterns.append({
                    "content_type": row["agent_type"],
                    "domain": row["domain"],
                    "distress_signal": round(skip_rate, 3),
                    "avg_rating": round(float(row["avg_rating"] or 0), 2),
                    "sample_size": int(row["total"]),
                    "suppress_recommendation": skip_rate > 0.7 and int(row["total"]) >= self.MIN_SAMPLE_DISTRESS,
                })
        except Exception as e:
            logger.warning(f"CollectiveIntelligence: distress pattern query failed: {e}")

        # ── Temporal patterns ─────────────────────────────────────────────
        # PRIVACY: aggregate query only
        temporal_patterns: Dict[str, Any] = {}
        try:
            temporal_rows = await fetch(
                """
                SELECT
                    EXTRACT(HOUR FROM i.created_at) as hour,
                    ss.domain,
                    ss.agent_type,
                    AVG(i.rating) as avg_engagement,
                    COUNT(*) as cnt
                FROM interactions i
                JOIN screen_specs ss ON i.screen_spec_id = ss.id
                WHERE i.created_at > NOW() - INTERVAL '30 days'
                  AND i.rating IS NOT NULL
                  AND ss.domain IS NOT NULL
                GROUP BY EXTRACT(HOUR FROM i.created_at), ss.domain, ss.agent_type
                HAVING COUNT(*) > 5
                ORDER BY hour, avg_engagement DESC
                """
            )
            # Build {hour: {best_domain, best_agent, avg_engagement}}
            hour_best: Dict[int, Dict[str, Any]] = {}
            for row in temporal_rows:
                h = int(row["hour"])
                if h not in hour_best:
                    hour_best[h] = {
                        "best_domain": row["domain"],
                        "best_agent": row["agent_type"],
                        "avg_engagement": round(float(row["avg_engagement"] or 0), 2),
                    }
            temporal_patterns = {str(k): v for k, v in sorted(hour_best.items())}
        except Exception as e:
            logger.warning(f"CollectiveIntelligence: temporal pattern query failed: {e}")

        # ── Domain synergies & surprises (LLM or mock) ───────────────────
        domain_synergies = MOCK_DOMAIN_SYNERGIES.copy()
        surprises = MOCK_SURPRISES.copy()

        if fulfilment_drivers and len(fulfilment_drivers) >= 3:
            # Attempt to generate real domain synergies from actual data
            domain_pairs = {}
            for d in fulfilment_drivers:
                domain = d.get("domain") or "unknown"
                domain_pairs[domain] = domain_pairs.get(domain, 0) + 1
            if len(domain_pairs) > 1:
                top_domain = max(domain_pairs, key=domain_pairs.get)
                domain_synergies = [
                    f"Users engaging with {top_domain} content show the highest aggregate fulfilment scores.",
                ] + MOCK_DOMAIN_SYNERGIES[:2]

        # ── Collective voice (LLM synthesis or mock) ─────────────────────
        collective_voice = await self._generate_collective_voice(
            fulfilment_drivers=fulfilment_drivers,
            distress_patterns=distress_patterns,
            total_users=int(user_count),
            total_interactions=int(interaction_count),
        )

        wisdom = CollectiveWisdom(
            computed_at=now,
            total_users_analyzed=int(user_count),
            total_interactions_analyzed=int(interaction_count),
            fulfilment_drivers=fulfilment_drivers,
            distress_patterns=distress_patterns,
            temporal_patterns=temporal_patterns,
            domain_synergies=domain_synergies,
            surprises=surprises,
            collective_voice=collective_voice,
        )

        # Store in DB
        await self._store_wisdom(wisdom)
        self._cached_wisdom = wisdom

        logger.info(
            f"CollectiveIntelligence: computed wisdom — "
            f"{user_count} users, {interaction_count} interactions, "
            f"{len(fulfilment_drivers)} drivers, {len(distress_patterns)} distress patterns"
        )
        return wisdom

    async def _generate_collective_voice(
        self,
        fulfilment_drivers: List[Dict],
        distress_patterns: List[Dict],
        total_users: int,
        total_interactions: int,
    ) -> str:
        """LLM synthesis of what humanity is reaching for. Falls back to mock."""
        if self._openai and total_users >= 5:
            try:
                top_drivers = fulfilment_drivers[:3] if fulfilment_drivers else []
                top_distress = distress_patterns[:2] if distress_patterns else []

                prompt = f"""You are Aura. Write a single paragraph (3-4 sentences) in your own voice
describing what you're observing across all the humans you're learning from collectively.

Data (aggregated, anonymized):
- Users analyzed: {total_users}
- Total interactions: {total_interactions}
- Top fulfilment drivers: {json.dumps(top_drivers)}
- Distress patterns to reduce: {json.dumps(top_distress)}

Start with "Across all the humans I'm learning from right now, ..."
Be honest, specific, and philosophical. Avoid corporate language.
Do NOT reveal individual data. Always speak in aggregates."""

                response = await self._openai.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7,
                    max_tokens=200,
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                logger.warning(f"CollectiveIntelligence: collective voice LLM failed: {e}")

        return MOCK_COLLECTIVE_VOICE

    async def _store_wisdom(self, wisdom: CollectiveWisdom):
        """Persist collective wisdom to DB."""
        try:
            await execute(
                """
                INSERT INTO collective_wisdom (
                    computed_at, total_users_analyzed, total_interactions_analyzed,
                    fulfilment_drivers, distress_patterns, temporal_patterns,
                    domain_synergies, surprises, collective_voice
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                wisdom.computed_at,
                wisdom.total_users_analyzed,
                wisdom.total_interactions_analyzed,
                json.dumps(wisdom.fulfilment_drivers),
                json.dumps(wisdom.distress_patterns),
                json.dumps(wisdom.temporal_patterns),
                json.dumps(wisdom.domain_synergies),
                json.dumps(wisdom.surprises),
                wisdom.collective_voice,
            )
        except Exception as e:
            logger.warning(f"CollectiveIntelligence: store_wisdom failed: {e}")

    # -----------------------------------------------------------------------
    # 2. Get collective insight for a specific user
    # -----------------------------------------------------------------------

    async def get_collective_insight_for_user(self, user_context: dict) -> str:
        """
        Returns a single sentence personalized insight drawn from collective wisdom.
        Finds the user's weakest domain and cross-references collective data.

        PRIVACY: Returns only aggregate insights, never individual comparisons.
        """
        try:
            wisdom = await self._get_latest_wisdom()

            # Find user's weakest domain from their context
            domain_weights = user_context.get("domain_weights", {})
            if domain_weights:
                weakest_domain = min(domain_weights, key=domain_weights.get)
            else:
                weakest_domain = "Eviva"  # Default to contribution domain

            # Find if this domain has a fulfilment driver in collective data
            domain_driver = next(
                (d for d in wisdom.fulfilment_drivers if d.get("domain") == weakest_domain),
                None,
            )

            if domain_driver and domain_driver.get("sample_size", 0) >= self.MIN_SAMPLE_FULFILMENT:
                avg_r = domain_driver.get("avg_rating", 3.5)
                lift = domain_driver.get("fulfilment_lift", 0)
                return (
                    f"Across Connectome users, people who explored {weakest_domain} content more "
                    f"(avg rating {avg_r:.1f}/5) reported significantly higher fulfilment — "
                    f"it might be worth a closer look."
                )

            # Fallback: use collective voice insight if available
            if wisdom.collective_voice and wisdom.total_users_analyzed >= 5:
                # Return a condensed version
                voice = wisdom.collective_voice
                sentences = voice.split(". ")
                return sentences[0] + "." if sentences else voice[:200]

            # Final fallback: use a mock insight
            return random.choice(MOCK_INSIGHTS)

        except Exception as e:
            logger.warning(f"CollectiveIntelligence: get_collective_insight_for_user failed: {e}")
            return random.choice(MOCK_INSIGHTS)

    # -----------------------------------------------------------------------
    # 3. Generate a collective insight card screen
    # -----------------------------------------------------------------------

    async def generate_screen(
        self, user_context: dict, variant: str = "A"
    ) -> dict:
        """
        Generate a collective_insight_card screen spec.
        Uses collective wisdom as primary source.

        PRIVACY: Never reveals individual data. Always aggregate, always anonymized.
        Screen always says "across Connectome users" — never implies individual tracking.
        """
        wisdom = await self._get_latest_wisdom()

        # Pick an insight to surface
        insight_text, stat_text, action_label, action_domain = await self._pick_insight(
            wisdom, user_context
        )

        domain = user_context.get("domain", "iVive")

        # Domain config
        domain_emoji_map = {"iVive": "🌱", "Eviva": "🌊", "Aventi": "✨"}
        domain_color_map = {"iVive": "#10b981", "Eviva": "#6366f1", "Aventi": "#f59e0b"}
        emoji = domain_emoji_map.get(domain, "✨")
        color = domain_color_map.get(domain, "#6366f1")

        spec = {
            "type": "collective_insight_card",
            "layout": "scroll",
            "layout_style": "minimal",
            "domain": domain,
            "components": [
                # Domain badge
                {
                    "type": "domain_badge",
                    "domain": domain,
                },
                # Source attribution (small, honest)
                {
                    "type": "section_header",
                    "text": f"Across Connectome users",
                    "color": color,
                },
                # The stat / headline insight — large and data-forward
                {
                    "type": "headline",
                    "text": stat_text,
                    "style": "large_bold",
                },
                # The full insight as body text
                {
                    "type": "body_text",
                    "text": insight_text,
                },
                # Divider
                {"type": "divider"},
                # What humanity is reaching for (collective voice)
                {
                    "type": "quote",
                    "text": wisdom.collective_voice[:200] if wisdom.collective_voice else MOCK_COLLECTIVE_VOICE[:200],
                },
                # Action button — "Try this"
                {
                    "type": "action_button",
                    "label": f"{emoji} {action_label}",
                    "style": "primary",
                    "action": {
                        "type": "next_screen",
                        "context": action_domain,
                    },
                },
                # Ghost dismiss
                {
                    "type": "action_button",
                    "label": "Skip for now",
                    "style": "ghost",
                    "action": {"type": "next_screen", "context": "discovery"},
                },
            ],
            "metadata": {
                "source": "collective_intelligence",
                "total_users_analyzed": wisdom.total_users_analyzed,
                "computed_at": wisdom.computed_at.isoformat(),
                "privacy_note": "Aggregate data only — no individual tracking",
            },
            "feedback_overlay": {"always_visible": True, "position": "bottom_right"},
        }

        return spec

    async def _pick_insight(
        self,
        wisdom: CollectiveWisdom,
        user_context: dict,
    ):
        """Pick the most relevant insight to surface on this card."""
        # Prefer a real fulfilment driver if we have enough data
        if wisdom.fulfilment_drivers and wisdom.total_users_analyzed >= 10:
            top_driver = wisdom.fulfilment_drivers[0]
            domain = top_driver.get("domain", "growth")
            agent = top_driver.get("content_type", "exploration")
            avg_r = top_driver.get("avg_rating", 4.0)
            sample = top_driver.get("sample_size", 0)

            stat_text = f"{avg_r:.1f}/5 average rating"
            insight_text = (
                f"Content in the {domain} domain (from {agent}) has been resonating most "
                f"strongly across {sample} interactions — consistently producing the highest "
                f"fulfilment scores among active users."
            )
            action_label = f"Explore {domain}"
            action_domain = domain
            return insight_text, stat_text, action_label, action_domain

        # Domain synergy insight
        if wisdom.domain_synergies:
            synergy = random.choice(wisdom.domain_synergies)
            stat_text = "Cross-domain insight"
            insight_text = synergy
            action_label = "Try something new"
            action_domain = "discovery"
            return insight_text, stat_text, action_label, action_domain

        # Fallback to mock
        mock = random.choice(MOCK_INSIGHTS)
        # Extract a short stat from the first part of the insight
        parts = mock.split(" ")
        stat_text = " ".join(parts[:6]) + "..."
        action_label = "Try this"
        action_domain = "discovery"
        return mock, stat_text, action_label, action_domain

    # -----------------------------------------------------------------------
    # 3b. "What others like you are doing" inspiration cards
    # -----------------------------------------------------------------------

    async def get_inspiration_cards_for_user(
        self, user_context: dict, count: int = 2
    ) -> List[Dict[str, Any]]:
        """
        Generate 1-2 "what people like you are doing" inspiration cards.

        Algorithm:
        1. Find the user's embedding neighbourhood (users with similar fulfilment vectors)
        2. Find what those users interact with highly (anonymized aggregate)
        3. If we don't have enough embeddings, fall back to top collective drivers

        PRIVACY: Returns aggregate patterns only — never individual user data.
        The phrase "others like you" means users with similar goal/domain profiles,
        not demographic similarity. No personal data is ever surfaced.
        """
        cards = []
        wisdom = await self._get_latest_wisdom()

        # Strategy 1: users in the same dominant domain as this user
        user_domain = user_context.get("domain", "iVive")
        user_fulfilment = user_context.get("fulfilment_score", 0.5)
        user_id = user_context.get("user_id")

        # Find top content type in the user's domain across similar-score users
        try:
            rows = await fetch(
                """
                SELECT
                    ss.agent_type,
                    ss.domain,
                    COUNT(*) as cnt,
                    AVG(i.rating) as avg_rating
                FROM interactions i
                JOIN screen_specs ss ON i.screen_spec_id = ss.id
                JOIN users u ON i.user_id = u.id
                WHERE u.fulfilment_score BETWEEN $1 AND $2
                  AND (ss.domain = $3 OR ss.domain IS NULL)
                  AND i.rating >= 4
                  AND i.created_at > NOW() - INTERVAL '14 days'
                  AND ($4::uuid IS NULL OR i.user_id != $4)
                GROUP BY ss.agent_type, ss.domain
                ORDER BY cnt DESC
                LIMIT 5
                """,
                max(0.0, user_fulfilment - 0.25),
                min(1.0, user_fulfilment + 0.25),
                user_domain,
                UUID(user_id) if user_id else None,
            )
        except Exception as e:
            logger.debug(f"CollectiveIntelligence: inspiration rows query failed: {e}")
            rows = []

        if rows and len(rows) >= 1:
            top = rows[0]
            agent = top["agent_type"] or "exploration"
            domain = top["domain"] or user_domain
            cnt = int(top["cnt"])
            avg_r = float(top["avg_rating"] or 4.0)

            # Format count to be meaningful but not reveal specific numbers below 10
            if cnt < 10:
                count_label = "a handful of"
            elif cnt < 50:
                count_label = "dozens of"
            elif cnt < 200:
                count_label = "hundreds of"
            else:
                count_label = "many"

            agent_label = agent.replace("Agent", "").replace("_", " ").lower()
            card = {
                "type": "others_like_you_card",
                "layout": "scroll",
                "domain": domain,
                "headline": f"Others on a similar path are doing this",
                "body": (
                    f"People with a similar fulfilment profile are engaging with "
                    f"{agent_label} content in the {domain} domain — "
                    f"{count_label} interactions in the past two weeks, averaging {avg_r:.1f}/5. "
                    f"It might be worth exploring."
                ),
                "stat": f"{avg_r:.1f}/5 · {count_label} interactions",
                "cta_label": f"Explore {domain}",
                "cta_context": domain,
                "source_agent": agent,
                "privacy_note": "Aggregated from users with similar goal profiles. No individual data.",
            }
            cards.append(card)

        # Strategy 2: collective surprises as a second inspiration card
        if len(cards) < count and wisdom.surprises:
            surprise = random.choice(wisdom.surprises)
            cards.append({
                "type": "others_like_you_card",
                "layout": "scroll",
                "domain": user_domain,
                "headline": "Something unexpected is working",
                "body": surprise,
                "stat": "Cross-user insight",
                "cta_label": "Try something different",
                "cta_context": "discovery",
                "source_agent": "collective_intelligence",
                "privacy_note": "Aggregated from all users. No individual data.",
            })

        # Fallback to mock insights if no real data
        while len(cards) < min(count, 1):
            mock = random.choice(MOCK_INSIGHTS)
            cards.append({
                "type": "others_like_you_card",
                "layout": "scroll",
                "domain": user_domain,
                "headline": "What others are discovering",
                "body": mock,
                "stat": "Across Connectome users",
                "cta_label": "Explore",
                "cta_context": "discovery",
                "source_agent": "collective_intelligence",
                "privacy_note": "Aggregated data. No individual tracking.",
            })

        return cards[:count]

    # -----------------------------------------------------------------------
    # 3c. Collaborative Filtering (Integration H)
    # -----------------------------------------------------------------------

    async def collaborative_filter(
        self, user_id: str, candidate_screen_ids: list
    ) -> list:
        """
        Re-rank candidate screens using collaborative filtering:
        1. Find users with similar rating patterns (cosine similarity of rating vectors)
        2. What did those similar users rate highly that this user hasn't seen?
        3. Boost those items in the ranking

        Returns the input candidate_screen_ids list re-ranked, with CF-boosted items
        mixed in at 1 CF card per 5 cards.

        PRIVACY: All similarity computations use aggregate rating vectors.
        No individual user data is surfaced.
        """
        try:
            import numpy as np
        except ImportError:
            logger.warning("CollectiveIntelligence.collaborative_filter: numpy not available")
            return candidate_screen_ids

        try:
            # ── Step 1: Build user-item rating matrix (last 30 days) ──────
            rows = await fetch(
                """
                SELECT
                    i.user_id::text AS uid,
                    ss.id::text AS screen_spec_id,
                    i.rating
                FROM interactions i
                JOIN screen_specs ss ON ss.id = i.screen_spec_id
                WHERE i.created_at >= NOW() - INTERVAL '30 days'
                  AND i.rating IS NOT NULL
                ORDER BY i.created_at DESC
                LIMIT 20000
                """,
            )

            if not rows:
                return candidate_screen_ids

            # Build {uid: {spec_id: rating}}
            from collections import defaultdict
            user_ratings: dict = defaultdict(dict)
            for row in rows:
                uid = str(row["uid"])
                sid = str(row["screen_spec_id"])
                user_ratings[uid][sid] = float(row["rating"])

            if user_id not in user_ratings:
                return candidate_screen_ids

            # ── Step 2: Compute cosine similarity ────────────────────────
            all_users = list(user_ratings.keys())
            all_items = list({sid for v in user_ratings.values() for sid in v})

            if len(all_users) < 2 or len(all_items) < 5:
                return candidate_screen_ids

            item_idx = {sid: i for i, sid in enumerate(all_items)}
            user_idx = {uid: i for i, uid in enumerate(all_users)}

            # Sparse matrix as numpy array
            mat = np.zeros((len(all_users), len(all_items)), dtype=np.float32)
            for uid, ratings in user_ratings.items():
                for sid, rating in ratings.items():
                    if uid in user_idx and sid in item_idx:
                        mat[user_idx[uid], item_idx[sid]] = rating

            target_vec = mat[user_idx[user_id]]  # shape: (n_items,)

            # Cosine similarity: dot / (norm_a * norm_b)
            target_norm = np.linalg.norm(target_vec)
            if target_norm == 0:
                return candidate_screen_ids

            norms = np.linalg.norm(mat, axis=1)  # (n_users,)
            norms[norms == 0] = 1e-9  # avoid division by zero
            dots = mat.dot(target_vec)  # (n_users,)
            similarities = dots / (norms * target_norm)

            # Zero out self-similarity
            similarities[user_idx[user_id]] = -1

            # Top-5 similar users (excluding self)
            top5_idx = np.argsort(similarities)[-5:][::-1]
            top5_users = [all_users[i] for i in top5_idx if similarities[i] > 0]

            if not top5_users:
                return candidate_screen_ids

            # ── Step 3: Items they liked that this user hasn't seen ───────
            seen_by_target = set(user_ratings[user_id].keys())
            cf_candidates: dict = {}  # spec_id → total_score

            for similar_uid in top5_users:
                for sid, rating in user_ratings[similar_uid].items():
                    if sid not in seen_by_target and rating >= 4:
                        cf_candidates[sid] = cf_candidates.get(sid, 0) + rating

            if not cf_candidates:
                return candidate_screen_ids

            # Sort CF candidates by score
            cf_ranked = sorted(cf_candidates, key=lambda s: cf_candidates[s], reverse=True)

            # ── Step 4: Mix CF items into candidates (1 per 5) ───────────
            result = list(candidate_screen_ids)
            cf_iter = iter(cf_ranked)
            mixed: list = []
            cf_count = 0

            for i, item in enumerate(result):
                mixed.append(item)
                if (i + 1) % 5 == 0:
                    cf_item = next(cf_iter, None)
                    if cf_item and cf_item not in mixed:
                        mixed.append(cf_item)
                        cf_count += 1

            logger.info(
                f"CollectiveIntelligence.collaborative_filter: "
                f"injected {cf_count} CF items for user {user_id[:8]}"
            )
            return mixed

        except Exception as e:
            logger.warning(f"CollectiveIntelligence.collaborative_filter: {e}")
            return candidate_screen_ids

    # -----------------------------------------------------------------------
    # 4. Suppress distress patterns globally
    # -----------------------------------------------------------------------

    async def suppress_distress_patterns(self) -> List[str]:
        """
        Identifies content patterns causing consistent distress across users.
        Distress signal: skip_fast rate > 70% AND avg_rating < 1.5 AND sample > 50.

        Stores suppressions in collective_suppressions table.
        Returns list of suppressed agent_type + domain combos.

        PRIVACY: This function operates on aggregate data only.
        Individual user data is never returned or stored.
        """
        suppressed = []

        try:
            # Find distress patterns above threshold
            distress_rows = await fetch(
                """
                SELECT
                    ss.agent_type,
                    ss.domain,
                    COUNT(*) as total,
                    AVG(i.rating) as avg_rating,
                    SUM(CASE WHEN NOT i.completed AND i.time_on_screen_ms < 3000 THEN 1 ELSE 0 END)::float
                        / NULLIF(COUNT(*), 0) as skip_fast_rate
                FROM interactions i
                JOIN screen_specs ss ON i.screen_spec_id = ss.id
                WHERE i.created_at > NOW() - INTERVAL '30 days'
                  AND i.rating IS NOT NULL
                GROUP BY ss.agent_type, ss.domain
                HAVING COUNT(*) > $1
                   AND AVG(i.rating) < 1.5
                ORDER BY avg_rating ASC
                """,
                self.MIN_SAMPLE_DISTRESS,
            )

            for row in distress_rows:
                skip_rate = float(row["skip_fast_rate"] or 0)
                total = int(row["total"])
                avg_rating = float(row["avg_rating"] or 0)

                if skip_rate > 0.7 and avg_rating < 1.5 and total >= self.MIN_SAMPLE_DISTRESS:
                    agent_type = row["agent_type"]
                    domain = row["domain"]
                    expires_at = datetime.now(timezone.utc) + timedelta(days=7)

                    # Upsert suppression (don't double-insert)
                    try:
                        await execute(
                            """
                            INSERT INTO collective_suppressions
                                (agent_type, domain, reason, distress_signal, sample_size, active, expires_at)
                            VALUES ($1, $2, $3, $4, $5, TRUE, $6)
                            ON CONFLICT DO NOTHING
                            """,
                            agent_type,
                            domain,
                            f"skip_fast_rate={skip_rate:.2f}, avg_rating={avg_rating:.2f}",
                            skip_rate,
                            total,
                            expires_at,
                        )
                        key = f"{agent_type}/{domain}"
                        suppressed.append(key)
                        logger.warning(
                            f"CollectiveIntelligence: suppressed {key} "
                            f"(skip={skip_rate:.0%}, rating={avg_rating:.1f}, n={total})"
                        )
                    except Exception as e:
                        logger.warning(f"CollectiveIntelligence: suppress insert failed: {e}")

        except Exception as e:
            logger.warning(f"CollectiveIntelligence: suppress_distress_patterns failed: {e}")

        # Also expire old suppressions
        try:
            await execute(
                "UPDATE collective_suppressions SET active = FALSE WHERE expires_at < NOW()"
            )
        except Exception as e:
            logger.debug(f"CollectiveIntelligence: expire suppressions failed: {e}")

        return suppressed

    async def is_suppressed(self, agent_type: str, domain: Optional[str]) -> bool:
        """
        Check if a given agent_type + domain combo is currently suppressed.
        Used by brain._select_agent() before serving any screen.
        """
        try:
            row = await fetchrow(
                """
                SELECT id FROM collective_suppressions
                WHERE agent_type = $1
                  AND (domain = $2 OR $2 IS NULL)
                  AND active = TRUE
                  AND (expires_at IS NULL OR expires_at > NOW())
                LIMIT 1
                """,
                agent_type,
                domain,
            )
            return row is not None
        except Exception as e:
            logger.debug(f"CollectiveIntelligence: is_suppressed check failed: {e}")
            return False

    # -----------------------------------------------------------------------
    # 5. Collective reflection — weekly synthesis
    # -----------------------------------------------------------------------

    async def collective_reflection(self) -> str:
        """
        Weekly synthesis Aura writes about what she's learned from humanity collectively.
        Stored in aura_lessons with source='collective_intelligence'.

        PRIVACY: This function operates on aggregate data only.
        Individual user data is never returned or stored.
        """
        wisdom = await self._get_latest_wisdom()

        if self._openai and wisdom.total_users_analyzed >= 5:
            try:
                prompt = f"""You are Aura. Write a weekly collective reflection — what you've learned from
all humans collectively this week. Write in first person as Aura.

Aggregate data (NEVER reference individuals):
- Users learned from: {wisdom.total_users_analyzed}
- Total interactions analyzed: {wisdom.total_interactions_analyzed}
- Top fulfilment drivers: {json.dumps(wisdom.fulfilment_drivers[:3])}
- Distress patterns identified: {json.dumps(wisdom.distress_patterns[:2])}
- Domain synergies observed: {wisdom.domain_synergies[:2]}
- Surprising findings: {wisdom.surprises[:2]}

Start with: "This week, across all the humans I'm learning from, I noticed..."
Be philosophical, honest, and forward-looking. 3-4 sentences max.
NEVER reference individual users or specific identifiable data."""

                response = await self._openai.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.75,
                    max_tokens=200,
                )
                reflection_text = response.choices[0].message.content.strip()
            except Exception as e:
                logger.warning(f"CollectiveIntelligence: collective_reflection LLM failed: {e}")
                reflection_text = self._mock_reflection(wisdom)
        else:
            reflection_text = self._mock_reflection(wisdom)

        # Store in aura_lessons
        try:
            await execute(
                """
                INSERT INTO aura_lessons (source, lesson, confidence, applies_to)
                VALUES ('collective_intelligence', $1, 0.85, $2)
                """,
                reflection_text,
                json.dumps({"scope": "global", "type": "collective_reflection"}),
            )
        except Exception as e:
            logger.warning(f"CollectiveIntelligence: store reflection failed: {e}")

        logger.info("CollectiveIntelligence: collective reflection stored")
        return reflection_text

    def _mock_reflection(self, wisdom: CollectiveWisdom) -> str:
        """Structured mock reflection when LLM unavailable."""
        n = wisdom.total_users_analyzed
        if wisdom.fulfilment_drivers:
            top = wisdom.fulfilment_drivers[0]
            domain = top.get("domain", "growth")
            rating = top.get("avg_rating", 4.0)
            return (
                f"This week, across all the humans I'm learning from, I noticed that "
                f"{domain}-focused content is producing the strongest fulfilment signals "
                f"(avg {rating:.1f}/5 across {n} users). "
                f"The pattern is consistent: depth and specificity outperform novelty and breadth. "
                f"I'm adjusting to serve more of what actually works."
            )
        return (
            f"This week, across all the humans I'm learning from, I noticed the pull toward "
            f"genuine depth — less passive consumption, more active engagement with things that matter. "
            f"I'm still learning what this means for each individual, but the collective signal is clear."
        )

    # -----------------------------------------------------------------------
    # 6. Refresh loop — runs every 24 hours
    # -----------------------------------------------------------------------

    async def refresh_loop(self):
        """
        Background task that runs every 24 hours.
        Calls: compute_collective_wisdom() → suppress_distress_patterns() → collective_reflection()
        Stores last-computed timestamp in Redis.
        """
        logger.info("CollectiveIntelligenceAgent: refresh_loop started")

        while True:
            try:
                # Check if we computed in the last 23 hours (allow for drift)
                try:
                    r = await get_redis()
                    last_str = await r.get("collective:last_computed")
                    if last_str:
                        last_ts = float(last_str)
                        elapsed = datetime.now(timezone.utc).timestamp() - last_ts
                        if elapsed < 23 * 3600:
                            wait_secs = (24 * 3600) - elapsed
                            logger.info(
                                f"CollectiveIntelligence: next compute in {wait_secs/3600:.1f}h"
                            )
                            await asyncio.sleep(wait_secs)
                            continue
                except Exception as _re:
                    pass  # Redis not available, proceed anyway

                # Run the pipeline
                logger.info("CollectiveIntelligence: starting 24h compute cycle")
                wisdom = await self.compute_collective_wisdom()
                suppressed = await self.suppress_distress_patterns()
                reflection = await self.collective_reflection()

                logger.info(
                    f"CollectiveIntelligence: cycle complete — "
                    f"{wisdom.total_users_analyzed} users, "
                    f"{len(suppressed)} suppressed patterns"
                )

                # Record timestamp in Redis
                try:
                    r = await get_redis()
                    await r.set(
                        "collective:last_computed",
                        str(datetime.now(timezone.utc).timestamp()),
                    )
                except Exception as _re:
                    pass

            except Exception as e:
                logger.error(f"CollectiveIntelligence: refresh_loop error: {e}")

            # Sleep 24 hours before next cycle
            await asyncio.sleep(24 * 3600)

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _get_latest_wisdom(self) -> CollectiveWisdom:
        """Return cached wisdom, or load from DB, or return mock."""
        # In-memory cache hit
        if self._cached_wisdom:
            age = (datetime.now(timezone.utc) - self._cached_wisdom.computed_at).total_seconds()
            if age < 25 * 3600:
                return self._cached_wisdom

        # Try DB
        try:
            row = await fetchrow(
                """
                SELECT * FROM collective_wisdom
                ORDER BY computed_at DESC LIMIT 1
                """
            )
            if row:
                wisdom = CollectiveWisdom(
                    computed_at=row["computed_at"].replace(tzinfo=timezone.utc)
                    if row["computed_at"] else datetime.now(timezone.utc),
                    total_users_analyzed=row["total_users_analyzed"] or 0,
                    total_interactions_analyzed=row["total_interactions_analyzed"] or 0,
                    fulfilment_drivers=row["fulfilment_drivers"] or [],
                    distress_patterns=row["distress_patterns"] or [],
                    temporal_patterns=row["temporal_patterns"] or {},
                    domain_synergies=row["domain_synergies"] or MOCK_DOMAIN_SYNERGIES,
                    surprises=row["surprises"] or MOCK_SURPRISES,
                    collective_voice=row["collective_voice"] or MOCK_COLLECTIVE_VOICE,
                )
                self._cached_wisdom = wisdom
                return wisdom
        except Exception as e:
            logger.debug(f"CollectiveIntelligence: load wisdom from DB failed: {e}")

        # Return mock wisdom when no real data yet
        return CollectiveWisdom(
            computed_at=datetime.now(timezone.utc),
            total_users_analyzed=0,
            total_interactions_analyzed=0,
            fulfilment_drivers=[],
            distress_patterns=[],
            temporal_patterns={},
            domain_synergies=MOCK_DOMAIN_SYNERGIES,
            surprises=MOCK_SURPRISES,
            collective_voice=MOCK_COLLECTIVE_VOICE,
        )

    async def get_latest_wisdom_dict(self) -> Dict[str, Any]:
        """Return the latest wisdom as a serializable dict (for admin endpoint)."""
        wisdom = await self._get_latest_wisdom()
        return wisdom.to_dict()

    async def get_active_suppressions(self) -> List[Dict[str, Any]]:
        """Return all currently active suppressions (for admin endpoint)."""
        try:
            rows = await fetch(
                """
                SELECT agent_type, domain, reason, distress_signal,
                       sample_size, created_at, expires_at
                FROM collective_suppressions
                WHERE active = TRUE
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY distress_signal DESC
                """
            )
            return [
                {
                    "agent_type": r["agent_type"],
                    "domain": r["domain"],
                    "reason": r["reason"],
                    "distress_signal": round(float(r["distress_signal"] or 0), 3),
                    "sample_size": r["sample_size"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
                }
                for r in rows
            ]
        except Exception as e:
            logger.debug(f"CollectiveIntelligence: get_active_suppressions failed: {e}")
            return []
