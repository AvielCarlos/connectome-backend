"""
ContextAgent — Calendar & Wearable Signals (Integration B)

Reads time-based signals (and optionally calendar) to inform Aura's content
decisions. Adapts card type and intensity based on the user's current context.

Redis key: user:{user_id}:context  (TTL 15 min)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Time-window constants
# ---------------------------------------------------------------------------

MORNING   = range(6, 10)    # 06:00 – 09:59
AFTERNOON = range(12, 17)   # 12:00 – 16:59
EVENING   = range(18, 22)   # 18:00 – 21:59
NIGHT_LO  = range(22, 24)   # 22:00 – 23:59
NIGHT_HI  = range(0, 3)     # 00:00 – 02:59

WEEKEND_DAYS = {5, 6}  # Saturday, Sunday


class ContextAgent:
    """
    Reads calendar + health signals to inform Aura's content decisions.
    Adapts card type and intensity based on the user's current context.
    """

    REDIS_TTL = 15 * 60  # 15 minutes

    async def get_user_context_signals(self, user_id: str) -> Dict[str, Any]:
        """
        Returns context hints Aura uses to pick appropriate content:
          - time_of_day: morning | afternoon | evening | night
          - day_of_week: weekday | weekend
          - next_event_minutes: minutes until next calendar event (None if unknown)
          - energy_level: high | medium | low  (inferred from time + patterns)
          - recommended_card_type: quick_insight | deep_dive | coaching | reflection
          - recommended_intensity: light | medium | challenging
        """
        # Try Redis cache first
        cached = await self._get_cached(user_id)
        if cached:
            return cached

        signals = await self._compute_signals(user_id)

        await self._cache_signals(user_id, signals)
        return signals

    # -----------------------------------------------------------------------
    # Core computation
    # -----------------------------------------------------------------------

    async def _compute_signals(self, user_id: str) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        hour = now.hour
        weekday = now.weekday()

        is_weekend = weekday in WEEKEND_DAYS
        day_of_week = "weekend" if is_weekend else "weekday"

        # ── Time-of-day bucket ──────────────────────────────────────────
        if hour in MORNING:
            time_of_day = "morning"
            energy_level = "medium"
            recommended_card_type = "reflection"
            recommended_intensity = "medium"

        elif hour in AFTERNOON:
            time_of_day = "afternoon"
            energy_level = "high"
            recommended_card_type = "deep_dive" if not is_weekend else "quick_insight"
            recommended_intensity = "challenging"

        elif hour in EVENING:
            time_of_day = "evening"
            energy_level = "medium"
            recommended_card_type = "coaching"
            recommended_intensity = "medium"

        elif hour in NIGHT_LO or hour in NIGHT_HI:
            time_of_day = "night"
            energy_level = "low"
            recommended_card_type = "reflection"
            recommended_intensity = "light"

        else:
            # mid-morning / late morning  10:00–11:59
            time_of_day = "morning"
            energy_level = "medium"
            recommended_card_type = "quick_insight"
            recommended_intensity = "medium"

        # ── Weekend overrides ───────────────────────────────────────────
        if is_weekend and time_of_day in ("morning", "afternoon"):
            recommended_card_type = "quick_insight"   # adventure + creativity
            recommended_intensity = "medium"

        # ── Calendar integration (optional, time-based only for now) ───
        next_event_minutes: Optional[int] = await self._get_next_event_minutes(user_id)

        # If a meeting is coming up within 30 min → shift to lighter content
        if next_event_minutes is not None and next_event_minutes <= 30:
            recommended_card_type = "quick_insight"
            recommended_intensity = "light"
            energy_level = "low"

        return {
            "time_of_day": time_of_day,
            "day_of_week": day_of_week,
            "next_event_minutes": next_event_minutes,
            "energy_level": energy_level,
            "recommended_card_type": recommended_card_type,
            "recommended_intensity": recommended_intensity,
            "computed_at_hour": hour,
        }

    # -----------------------------------------------------------------------
    # Calendar — optional Google Calendar read
    # -----------------------------------------------------------------------

    async def _get_next_event_minutes(self, user_id: str) -> Optional[int]:
        """
        Return minutes until the user's next calendar event, or None.
        Only reads calendar if the user has google_oauth_tokens with
        drive_connected=true (existing Google OAuth infrastructure).
        """
        try:
            from core.database import fetchrow
            from uuid import UUID
            row = await fetchrow(
                "SELECT tokens FROM google_oauth_tokens WHERE user_id = $1",
                UUID(user_id),
            )
            if not row:
                return None

            tokens_raw = row["tokens"]
            tokens = (
                json.loads(tokens_raw) if isinstance(tokens_raw, str) else (tokens_raw or {})
            )
            if not tokens.get("drive_connected"):
                return None

            # TODO: Implement real Google Calendar read using tokens["access_token"].
            # For now, return None — time-based signals are sufficient.
            return None

        except Exception as e:
            logger.debug(f"ContextAgent: calendar check failed: {e}")
            return None

    # -----------------------------------------------------------------------
    # Redis cache
    # -----------------------------------------------------------------------

    async def _get_cached(self, user_id: str) -> Optional[Dict[str, Any]]:
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            raw = await r.get(f"user:{user_id}:context")
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.debug(f"ContextAgent: cache read failed: {e}")
        return None

    async def _cache_signals(self, user_id: str, signals: Dict[str, Any]) -> None:
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            await r.set(
                f"user:{user_id}:context",
                json.dumps(signals),
                ex=self.REDIS_TTL,
            )
        except Exception as e:
            logger.debug(f"ContextAgent: cache write failed: {e}")
