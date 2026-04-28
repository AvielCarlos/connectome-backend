"""
OnboardingOptimizationAgent — A/B tests the first-run experience.

Tracks D1/D7 retention per variant and auto-promotes the winner when one
variant has a 20%+ lead over the average with >20 users.

Variants:
  A — goals_first      (set goals before seeing feed)
  B — feed_first       (jump straight into feed)
  C — ora_chat_first   (meet Ora before anything else)
  D — interests_first  (pick interests/topics first)
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class OnboardingOptimizationAgent:
    """
    A/B tests the first-run onboarding flow.
    Tracks D1/D7 retention per variant.
    Auto-promotes the winner when one variant has 20%+ lead with >20 users.
    """

    ONBOARDING_VARIANTS: Dict[str, str] = {
        "A": "goals_first",
        "B": "feed_first",
        "C": "ora_chat_first",
        "D": "interests_first",
    }

    def __init__(self, openai_client=None):
        # openai_client accepted for API compatibility but not required
        pass

    # ------------------------------------------------------------------
    # Variant assignment
    # ------------------------------------------------------------------

    async def assign_onboarding_variant(self, user_id: str) -> str:
        """
        Assign a consistent onboarding variant for the given user.
        Uses MD5 hash of user_id for deterministic assignment.
        Stores variant in Redis and increments variant count.
        """
        # If a winner has already been promoted, use it
        winner = await self.get_winner()
        if winner:
            return winner

        idx = int(hashlib.md5(user_id.encode()).hexdigest(), 16) % 4
        variant = list(self.ONBOARDING_VARIANTS.keys())[idx]

        try:
            from core.redis_client import get_redis
            r = await get_redis()
            # Store per-user variant (30d TTL — long enough to track D7)
            await r.set(f"onboarding:{user_id}:variant", variant, ex=30 * 24 * 3600)
            # Increment total counts per variant (no TTL — cumulative)
            await r.incr(f"onboarding:counts:{variant}")
        except Exception as e:
            logger.warning(f"OnboardingOptimizationAgent: Redis write failed: {e}")

        return variant

    # ------------------------------------------------------------------
    # Retention tracking
    # ------------------------------------------------------------------

    async def track_d1_retention(self, user_id: str) -> None:
        """
        Called when a user logs in again within 24 h of signup.
        Increments the D1 counter for their assigned variant.
        """
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            raw = await r.get(f"onboarding:{user_id}:variant")
            if not raw:
                return
            variant = raw if isinstance(raw, str) else raw.decode()
            await r.incr(f"onboarding:d1:{variant}")
            logger.debug(f"OnboardingOptimizationAgent: D1 tracked for variant {variant}")
        except Exception as e:
            logger.warning(f"OnboardingOptimizationAgent: track_d1_retention failed: {e}")

    async def track_d7_retention(self, user_id: str) -> None:
        """
        Called when a user logs in again within 7 days of signup.
        Increments the D7 counter for their assigned variant.
        """
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            raw = await r.get(f"onboarding:{user_id}:variant")
            if not raw:
                return
            variant = raw if isinstance(raw, str) else raw.decode()
            await r.incr(f"onboarding:d7:{variant}")
            logger.debug(f"OnboardingOptimizationAgent: D7 tracked for variant {variant}")
        except Exception as e:
            logger.warning(f"OnboardingOptimizationAgent: track_d7_retention failed: {e}")

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    async def analyze_retention(self) -> Dict[str, Any]:
        """
        Compute D1/D7 retention rates per variant.
        If one variant has >20 users and a D7 rate 20%+ higher than the
        average, flag it as the winner and store in Redis.
        """
        try:
            from core.redis_client import get_redis
            r = await get_redis()

            results: Dict[str, Any] = {}
            total_d7_rates = []

            for variant in self.ONBOARDING_VARIANTS:
                count_raw = await r.get(f"onboarding:counts:{variant}")
                d1_raw = await r.get(f"onboarding:d1:{variant}")
                d7_raw = await r.get(f"onboarding:d7:{variant}")

                count = int(count_raw or 0)
                d1 = int(d1_raw or 0)
                d7 = int(d7_raw or 0)

                d1_rate = round(d1 / count, 3) if count > 0 else 0.0
                d7_rate = round(d7 / count, 3) if count > 0 else 0.0

                results[variant] = {
                    "variant": variant,
                    "name": self.ONBOARDING_VARIANTS[variant],
                    "total_users": count,
                    "d1_count": d1,
                    "d7_count": d7,
                    "d1_rate": d1_rate,
                    "d7_rate": d7_rate,
                }
                total_d7_rates.append(d7_rate)

            # Determine winner
            avg_d7 = sum(total_d7_rates) / len(total_d7_rates) if total_d7_rates else 0.0
            winner: Optional[str] = None

            for variant, data in results.items():
                if (
                    data["total_users"] > 20
                    and avg_d7 > 0
                    and data["d7_rate"] >= avg_d7 * 1.20
                ):
                    # Check it's the best
                    if winner is None or data["d7_rate"] > results[winner]["d7_rate"]:
                        winner = variant

            if winner:
                await r.set("onboarding:winner", winner)
                results[winner]["is_winner"] = True
                logger.info(f"OnboardingOptimizationAgent: winner is variant {winner}")

            return {
                "variants": results,
                "avg_d7_rate": round(avg_d7, 3),
                "winner": winner,
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
            }

        except Exception as e:
            logger.warning(f"OnboardingOptimizationAgent: analyze_retention failed: {e}")
            return {"error": str(e)}

    async def get_winner(self) -> Optional[str]:
        """Return the current winning variant key, or None if no winner yet."""
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            raw = await r.get("onboarding:winner")
            if not raw:
                return None
            winner = raw if isinstance(raw, str) else raw.decode()
            # Validate it's a known variant
            return winner if winner in self.ONBOARDING_VARIANTS else None
        except Exception as e:
            logger.warning(f"OnboardingOptimizationAgent: get_winner failed: {e}")
            return None
