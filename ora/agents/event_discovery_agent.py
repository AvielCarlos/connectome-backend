"""
EventDiscoveryAgent — Ora's Local Event Card Generator
======================================================
Injects `local_event` cards into the Ora brain feed based on:
  • User's city (from user profile)
  • User's active goals (semantic alignment)
  • User's event_preferences (explicit categories)
  • Upcoming events in the 7-day serving window

Card format:
  {
    "type": "local_event",
    "title": "Breathwork & Sound Bath — Vancouver",
    "body": "This Saturday at Roundhouse Community Centre. Free entry. ...",
    "action": "Learn more",
    "action_url": "https://...",
    "meta": {
      "event_id": 42,
      "starts_at": "2026-05-03T18:00:00+00:00",
      "price": "free",
      "category": "wellness",
      "venue": "Roundhouse Community Centre",
      "city": "Vancouver"
    }
  }

Injection rules:
  • 1–2 event cards per session (never spam)
  • User must have city set
  • Filter by goal alignment when active goals exist
  • Prioritize events soonest first (ascending starts_at)
  • Never show past events (starts_at > NOW() enforced at DB level)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from core.config import settings
from ora.agents.event_agent import EventAgent, SERVE_WINDOW_DAYS

logger = logging.getLogger(__name__)


# Goal-text → event categories heuristic map
GOAL_CATEGORY_MAP: Dict[str, List[str]] = {
    "stress":       ["wellness", "community"],
    "anxiety":      ["wellness", "arts"],
    "fitness":      ["sports", "wellness"],
    "social":       ["community", "networking", "arts", "music"],
    "connect":      ["community", "networking"],
    "creative":     ["arts", "music", "community"],
    "learn":        ["tech", "networking", "arts"],
    "career":       ["tech", "networking"],
    "mindful":      ["wellness"],
    "health":       ["wellness", "sports", "food"],
    "fun":          ["music", "arts", "food", "community"],
    "nature":       ["sports", "wellness"],
    "music":        ["music", "arts"],
    "food":         ["food", "community"],
}


def _goal_to_categories(goals: List[Dict]) -> List[str]:
    """Map active goal titles/descriptions → event category preferences."""
    categories: set = set()
    for goal in goals:
        text = f"{goal.get('title', '')} {goal.get('description', '')}".lower()
        for keyword, cats in GOAL_CATEGORY_MAP.items():
            if keyword in text:
                categories.update(cats)
    return list(categories)


def _format_event_time(starts_at: datetime) -> str:
    """Human-friendly time string: 'This Saturday at 6 PM', 'Tomorrow at 2 PM', etc."""
    now = datetime.now(timezone.utc)
    delta = starts_at - now

    # Normalize to local-ish display (UTC for now; city-level TZ is a v2 feature)
    day_name = starts_at.strftime("%A")   # e.g. 'Saturday'
    time_str = starts_at.strftime("%-I %p").lstrip("0")  # e.g. '6 PM'

    if delta.days == 0:
        hour_diff = int(delta.total_seconds() / 3600)
        if hour_diff <= 3:
            return f"in {hour_diff} hour{'s' if hour_diff != 1 else ''}"
        return f"today at {time_str}"
    elif delta.days == 1:
        return f"tomorrow at {time_str}"
    elif delta.days < 7:
        return f"this {day_name} at {time_str}"
    else:
        date_str = starts_at.strftime("%b %-d")  # e.g. 'May 3'
        return f"{date_str} at {time_str}"


def _build_event_body(event: Dict, goal_context: str = "") -> str:
    """
    Compose a concise, human-friendly card body.
    ~2 sentences max. Mentions time, venue, price, and goal alignment if relevant.
    """
    parts: List[str] = []

    # Time + venue
    starts_at = event.get("starts_at")
    if starts_at:
        time_str = _format_event_time(starts_at)
        venue = event.get("venue_name", "")
        city = event.get("city", "")
        if venue:
            parts.append(f"{time_str.capitalize()} at {venue}.")
        else:
            parts.append(f"{time_str.capitalize()} in {city}.")

    # Price
    price = event.get("price_range", "")
    if price == "free":
        parts.append("Free entry.")
    elif price and price not in ("unknown", "paid"):
        parts.append(f"{price}.")

    # Goal alignment nudge
    if goal_context:
        parts.append(f"Aligns with your {goal_context} goal.")

    # Fallback: use description snippet
    if len(parts) < 2 and event.get("description"):
        snippet = event["description"][:120].strip()
        if snippet and not snippet.endswith("."):
            snippet += "."
        parts.append(snippet)

    return " ".join(parts[:3])


def _find_goal_alignment(event: Dict, goals: List[Dict]) -> str:
    """Return a short goal label if this event aligns with any active goal."""
    event_cats = set(event.get("relevance_tags") or [event.get("category", "")])
    for goal in goals:
        goal_text = f"{goal.get('title', '')} {goal.get('description', '')}".lower()
        for keyword, cats in GOAL_CATEGORY_MAP.items():
            if keyword in goal_text and event_cats.intersection(cats):
                return goal.get("title", keyword)
    return ""


def _build_card(event: Dict, goals: List[Dict]) -> Dict[str, Any]:
    """Build a complete local_event card dict."""
    goal_label = _find_goal_alignment(event, goals)
    body = _build_event_body(event, goal_context=goal_label)
    title = event.get("title", "Local Event")
    city = event.get("city", "")
    if city and city.lower() not in title.lower():
        title = f"{title} — {city}"

    starts_at = event.get("starts_at")

    return {
        "type":       "local_event",
        "title":      title,
        "body":       body,
        "action":     "Learn more",
        "action_url": event.get("url", ""),
        "meta": {
            "event_id":  event.get("id"),
            "starts_at": starts_at.isoformat() if starts_at else None,
            "price":     event.get("price_range", "unknown"),
            "category":  event.get("category", "general"),
            "venue":     event.get("venue_name", ""),
            "city":      city,
            "source":    event.get("source", ""),
            "image_url": event.get("image_url", ""),
        },
        "metadata": {
            "agent":          "EventDiscoveryAgent",
            "is_local_event": True,
            "goal_aligned":   bool(goal_label),
            "goal_label":     goal_label,
        },
    }


class EventDiscoveryAgent:
    """
    Generates local_event cards for injection into Ora's brain feed.

    Inject 1–2 cards per session for users who have a city set.
    The injected cards slot into the feed alongside world-aware cards.
    """

    AGENT_NAME = "EventDiscoveryAgent"
    MAX_CARDS_PER_SESSION = 2

    def __init__(self, openai_client=None):
        self.openai = openai_client
        self._event_agent = EventAgent(openai_client=openai_client)

    async def get_event_cards(
        self,
        user_context: Dict[str, Any],
        max_cards: int = MAX_CARDS_PER_SESSION,
    ) -> List[Dict[str, Any]]:
        """
        Return up to max_cards local_event cards for this user.
        Returns empty list if user has no city or no events found.
        """
        city = user_context.get("user_city", "")
        if not city:
            return []

        active_goals = user_context.get("active_goals", [])
        event_preferences = user_context.get("event_preferences", [])

        # Determine category filter from goals or explicit preferences
        preferred_categories = list(set(_goal_to_categories(active_goals) + event_preferences))

        # Try to auto-sync if city has no recent events
        await self._maybe_trigger_sync(city)

        # Fetch from DB
        try:
            if preferred_categories:
                # Try preference-filtered first
                events = await self._fetch_matching_events(
                    city, preferred_categories, limit=20
                )
                if not events:
                    # Fall back to all upcoming
                    events = await self._event_agent.get_events_for_city(
                        city, days_ahead=SERVE_WINDOW_DAYS, limit=20
                    )
            else:
                events = await self._event_agent.get_events_for_city(
                    city, days_ahead=SERVE_WINDOW_DAYS, limit=20
                )
        except Exception as e:
            logger.warning(f"EventDiscoveryAgent: DB fetch failed for {city}: {e}")
            return []

        if not events:
            return []

        # Pick the most relevant events (goal-aligned first, then soonest)
        scored = self._score_events(events, active_goals, preferred_categories)
        top = scored[:max_cards]

        cards = [_build_card(ev, active_goals) for ev in top]
        logger.info(
            f"EventDiscoveryAgent: returning {len(cards)} card(s) for {city} "
            f"(goals={len(active_goals)}, prefs={preferred_categories})"
        )
        return cards

    def _score_events(
        self,
        events: List[Dict],
        goals: List[Dict],
        preferred_categories: List[str],
    ) -> List[Dict]:
        """
        Score events by:
          +3  — category matches goal alignment
          +2  — category matches explicit preference
          +1  — soonest (inverse days away, normalized)
        Sort descending.
        """
        now = datetime.now(timezone.utc)

        def score(ev: Dict) -> float:
            s = 0.0
            cats = set(ev.get("relevance_tags") or [ev.get("category", "")])

            goal_cats = set(_goal_to_categories(goals))
            if cats & goal_cats:
                s += 3.0

            if preferred_categories and cats & set(preferred_categories):
                s += 2.0

            starts_at = ev.get("starts_at")
            if starts_at:
                days_out = max(0.01, (starts_at - now).total_seconds() / 86400)
                s += 1.0 / days_out  # soonest = higher score

            return s

        return sorted(events, key=score, reverse=True)

    async def _fetch_matching_events(
        self, city: str, categories: List[str], limit: int = 20
    ) -> List[Dict]:
        """Fetch events matching any of the given categories."""
        from core.database import fetch as _fetch
        until = datetime.now(timezone.utc) + timedelta(days=SERVE_WINDOW_DAYS)

        rows = await _fetch(
            """
            SELECT id, external_id, title, description, category,
                   venue_name, address, city, latitude, longitude,
                   starts_at, ends_at, url, image_url, price_range,
                   source, relevance_tags
            FROM events
            WHERE city = $1
              AND starts_at > NOW()
              AND starts_at < $2
              AND (relevance_tags && $3 OR category = ANY($3))
            ORDER BY starts_at ASC
            LIMIT $4
            """,
            city,
            until,
            categories,
            limit,
        )
        return [dict(r) for r in rows]

    async def _maybe_trigger_sync(self, city: str) -> None:
        """
        Background-trigger a city sync if no events in DB for this city.
        Fire-and-forget; never blocks card generation.
        """
        try:
            from core.database import fetchval as _fetchval
            count = await _fetchval(
                "SELECT COUNT(*) FROM events WHERE city = $1 AND starts_at > NOW()",
                city,
            )
            if not count:
                logger.info(
                    f"EventDiscoveryAgent: no events for {city}, triggering background sync"
                )
                import asyncio
                asyncio.create_task(self._event_agent.sync_city(city))
        except Exception as e:
            logger.debug(f"EventDiscoveryAgent background sync check failed: {e}")

    async def generate_screen(
        self,
        user_context: Dict[str, Any],
        variant: str = "A",
    ) -> Optional[Dict[str, Any]]:
        """
        generate_screen-compatible interface for direct use as an Ora agent.
        Returns a single event card as a ScreenSpec-like dict, or None.
        """
        cards = await self.get_event_cards(user_context, max_cards=1)
        if not cards:
            return None
        card = cards[0]

        # Wrap in ScreenSpec envelope
        return {
            "type":   "local_event",
            "layout": "card_stack",
            "components": [
                {
                    "type":  "category_badge",
                    "text":  f"📍 LOCAL EVENT — {(card['meta'].get('category') or 'event').upper()}",
                    "color": "#10b981",
                },
                {
                    "type":  "headline",
                    "text":  card["title"],
                    "style": "large_bold",
                },
                {
                    "type": "body_text",
                    "text": card["body"],
                },
                {
                    "type":   "action_button",
                    "label":  card["action"],
                    "action": {
                        "type": "open_url",
                        "url":  card.get("action_url", ""),
                    },
                },
            ],
            "feedback_overlay": {
                "type":           "star_rating",
                "position":       "bottom_right",
                "always_visible": True,
            },
            "metadata": {
                **card.get("metadata", {}),
                "card_data":    card,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        }
