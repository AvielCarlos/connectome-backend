"""
WorldSignalAgent — Ora's Live World Awareness Layer

Fetches and caches lightweight real-world signals that drive serendipity:
  • Weather        — wttr.in (no API key)
  • Time signals   — time of day, day of week, season
  • Moon phase     — calculated from date (no API)
  • Wikipedia OTD  — On This Day historical events
  • HackerNews     — top 5 trending titles
  • Serendipity    — hourly random seed for variation

Cache key: world_signals:{YYYY-MM-DD-HH}  (1-hour TTL)
"""

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 8.0
CACHE_TTL_SECONDS = 3600  # 1 hour


# ---------------------------------------------------------------------------
# Pure helpers — no I/O
# ---------------------------------------------------------------------------

def get_time_of_day(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    elif 12 <= hour < 17:
        return "afternoon"
    elif 17 <= hour < 21:
        return "evening"
    else:
        return "night"


def get_season(month: int, southern_hemisphere: bool = False) -> str:
    if not southern_hemisphere:
        if month in (12, 1, 2):
            return "winter"
        elif month in (3, 4, 5):
            return "spring"
        elif month in (6, 7, 8):
            return "summer"
        else:
            return "autumn"
    else:
        if month in (12, 1, 2):
            return "summer"
        elif month in (3, 4, 5):
            return "autumn"
        elif month in (6, 7, 8):
            return "winter"
        else:
            return "spring"


def calculate_moon_phase(date: datetime) -> Tuple[str, str]:
    """
    Calculate moon phase from date. Returns (phase_name, emoji).
    No API needed — lunar cycle is ~29.53 days from a known new moon.
    """
    known_new_moon = datetime(2000, 1, 6, tzinfo=timezone.utc)
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    days_since = (date - known_new_moon).days
    cycle_position = days_since % 29.53

    if cycle_position < 1.85:
        return "new_moon", "🌑"
    elif cycle_position < 7.38:
        return "waxing_crescent", "🌒"
    elif cycle_position < 9.22:
        return "first_quarter", "🌓"
    elif cycle_position < 14.77:
        return "waxing_gibbous", "🌔"
    elif cycle_position < 16.61:
        return "full_moon", "🌕"
    elif cycle_position < 22.15:
        return "waning_gibbous", "🌖"
    elif cycle_position < 23.99:
        return "last_quarter", "🌗"
    else:
        return "waning_crescent", "🌘"


def hourly_serendipity_seed(date: datetime) -> float:
    """
    Deterministic but varying float per hour — drives card variation.
    Same hour → same seed (so cached signals produce consistent cards).
    """
    rng = random.Random(f"{date.year}-{date.month:02d}-{date.day:02d}-{date.hour:02d}")
    return round(rng.random(), 4)


# ---------------------------------------------------------------------------
# WorldSignalAgent
# ---------------------------------------------------------------------------

class WorldSignalAgent:
    """
    Lightweight, fast world signal fetcher.
    Designed to be called every screen request — results are Redis-cached hourly.
    """

    CACHE_KEY_TEMPLATE = "world_signals_v2:{year}-{month:02d}-{day:02d}-{hour:02d}"

    def __init__(self):
        self._http: Optional[httpx.AsyncClient] = None

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=HTTP_TIMEOUT,
                follow_redirects=True,
                headers={
                    "User-Agent": "Connectome/1.0 (https://connectome.app; contact@connectome.app)"
                },
            )
        return self._http

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_signals(self, city: str = "Vancouver") -> Dict[str, Any]:
        """
        Return current world signals. Served from cache if fresh.
        Falls back gracefully if external calls fail.
        """
        now = datetime.now(timezone.utc)
        cache_key = self.CACHE_KEY_TEMPLATE.format(
            year=now.year, month=now.month, day=now.day, hour=now.hour
        )

        # Try Redis cache first
        cached = await self._load_from_cache(cache_key)
        if cached:
            logger.debug(f"WorldSignalAgent: cache hit for {cache_key}")
            return cached

        # Build signals fresh
        signals = await self._build_signals(now, city)

        # Store in cache
        await self._save_to_cache(cache_key, signals)

        logger.info(
            f"WorldSignalAgent: fresh signals for {city} at {now.strftime('%Y-%m-%d %H:00')}"
        )
        return signals

    async def _build_signals(self, now: datetime, city: str) -> Dict[str, Any]:
        """Assemble all world signals in parallel."""
        # Time-derived signals (instant, no I/O)
        time_of_day = get_time_of_day(now.hour)
        season = get_season(now.month)
        moon_phase, moon_emoji = calculate_moon_phase(now)
        serendipity_seed = hourly_serendipity_seed(now)
        day_name = now.strftime("%A")  # e.g., "Monday"

        # Async I/O in parallel
        weather_task = self._fetch_weather(city)
        hn_task = self._fetch_hackernews()
        otd_task = self._fetch_on_this_day(now.month, now.day)

        weather, hn_titles, on_this_day = await asyncio.gather(
            weather_task, hn_task, otd_task, return_exceptions=True
        )

        # Safely handle any exceptions
        if isinstance(weather, Exception):
            logger.warning(f"WorldSignalAgent weather error: {weather}")
            weather = {"condition": "unknown", "temp_c": None, "feels_like_c": None, "city": city}

        if isinstance(hn_titles, Exception):
            logger.warning(f"WorldSignalAgent HN error: {hn_titles}")
            hn_titles = []

        if isinstance(on_this_day, Exception):
            logger.warning(f"WorldSignalAgent OTD error: {on_this_day}")
            on_this_day = []

        return {
            "weather": weather,
            "time_of_day": time_of_day,
            "hour": now.hour,
            "day_of_week": day_name,
            "season": season,
            "moon_phase": moon_phase,
            "moon_emoji": moon_emoji,
            "on_this_day": on_this_day[:2],  # pick top 2
            "trending_hn": hn_titles[:5],
            "serendipity_seed": serendipity_seed,
            "generated_at": now.isoformat(),
            "city": city,
        }

    # ------------------------------------------------------------------
    # Signal fetchers
    # ------------------------------------------------------------------

    async def _fetch_weather(self, city: str) -> Dict[str, Any]:
        """Fetch weather from wttr.in — no API key needed."""
        http = self._get_http()
        url = f"https://wttr.in/{city}?format=j1"

        try:
            resp = await http.get(url, headers={"Accept": "application/json"})
            if resp.status_code != 200:
                raise ValueError(f"wttr.in returned {resp.status_code}")

            data = resp.json()
            current = data.get("current_condition", [{}])[0]

            temp_c = float(current.get("temp_C", 15))
            feels_like_c = float(current.get("FeelsLikeC", temp_c))
            condition_list = current.get("weatherDesc", [{}])
            condition = condition_list[0].get("value", "Clear") if condition_list else "Clear"

            # Normalize condition to a simple key
            condition_lower = condition.lower()
            if any(w in condition_lower for w in ["rain", "drizzle", "shower"]):
                condition_key = "rainy"
            elif any(w in condition_lower for w in ["snow", "blizzard", "sleet"]):
                condition_key = "snowy"
            elif any(w in condition_lower for w in ["cloud", "overcast"]):
                condition_key = "cloudy"
            elif any(w in condition_lower for w in ["thunder", "storm"]):
                condition_key = "stormy"
            elif any(w in condition_lower for w in ["fog", "mist", "haze"]):
                condition_key = "foggy"
            elif any(w in condition_lower for w in ["sun", "clear", "bright"]):
                condition_key = "sunny"
            elif any(w in condition_lower for w in ["partly", "partial"]):
                condition_key = "partly_cloudy"
            else:
                condition_key = "clear"

            return {
                "condition": condition_key,
                "condition_raw": condition,
                "temp_c": round(temp_c, 1),
                "feels_like_c": round(feels_like_c, 1),
                "city": city,
            }

        except Exception as e:
            logger.warning(f"wttr.in fetch failed for {city}: {e}")
            return {"condition": "unknown", "temp_c": None, "feels_like_c": None, "city": city}

    async def _fetch_hackernews(self) -> List[str]:
        """Fetch top 5 HN story titles."""
        http = self._get_http()
        LEARNING_KEYWORDS = {
            "learn", "tutorial", "guide", "how", "intro", "tool", "open source",
            "research", "build", "python", "ai", "ml", "productivity", "science",
            "new", "launch", "release", "show hn", "ask hn",
        }

        try:
            resp = await http.get(
                "https://hacker-news.firebaseio.com/v0/topstories.json",
                timeout=5.0,
            )
            if resp.status_code != 200:
                return []

            story_ids: List[int] = resp.json()[:15]
            story_tasks = [
                http.get(f"https://hacker-news.firebaseio.com/v0/item/{sid}.json", timeout=4.0)
                for sid in story_ids[:12]
            ]
            story_responses = await asyncio.gather(*story_tasks, return_exceptions=True)

            titles = []
            for sr in story_responses:
                if isinstance(sr, Exception) or sr.status_code != 200:
                    continue
                story = sr.json()
                if not story or story.get("type") != "story":
                    continue
                title = story.get("title", "").strip()
                if title:
                    title_lower = title.lower()
                    if any(kw in title_lower for kw in LEARNING_KEYWORDS):
                        titles.append(title)
                if len(titles) >= 5:
                    break

            # If filter was too aggressive, grab any titles
            if not titles:
                for sr in story_responses:
                    if isinstance(sr, Exception) or sr.status_code != 200:
                        continue
                    story = sr.json()
                    if story and story.get("type") == "story" and story.get("title"):
                        titles.append(story["title"].strip())
                    if len(titles) >= 5:
                        break

            return titles

        except Exception as e:
            logger.warning(f"HackerNews fetch failed: {e}")
            return []

    async def _fetch_on_this_day(self, month: int, day: int) -> List[Dict[str, Any]]:
        """Fetch Wikipedia 'On This Day' events."""
        http = self._get_http()
        try:
            resp = await http.get(
                f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{month}/{day}",
                headers={"Accept": "application/json"},
                timeout=6.0,
            )
            if resp.status_code != 200:
                return []

            data = resp.json()
            events = data.get("events", [])

            # Filter for interesting events — prefer ones with high year diversity
            # and skip very recent/mundane entries
            results = []
            for evt in events:
                year = evt.get("year", 0)
                text = evt.get("text", "").strip()
                if not text or not year:
                    continue
                # Prefer historical events (not too recent, not too ancient)
                if 1850 <= year <= 2010:
                    results.append({"year": year, "text": text})
                if len(results) >= 5:
                    break

            # If not enough, take anything
            if len(results) < 2:
                for evt in events[:3]:
                    year = evt.get("year", 0)
                    text = evt.get("text", "").strip()
                    if text and year:
                        results.append({"year": year, "text": text})

            return results[:2]

        except Exception as e:
            logger.warning(f"Wikipedia OTD fetch failed for {month}/{day}: {e}")
            return []

    # ------------------------------------------------------------------
    # Redis cache
    # ------------------------------------------------------------------

    async def _load_from_cache(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            raw = await r.get(key)
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.debug(f"WorldSignalAgent cache read failed: {e}")
        return None

    async def _save_to_cache(self, key: str, signals: Dict[str, Any]) -> None:
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            await r.set(key, json.dumps(signals), ex=CACHE_TTL_SECONDS)
        except Exception as e:
            logger.debug(f"WorldSignalAgent cache write failed: {e}")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_world_signal_agent: Optional[WorldSignalAgent] = None


def get_world_signal_agent() -> WorldSignalAgent:
    global _world_signal_agent
    if _world_signal_agent is None:
        _world_signal_agent = WorldSignalAgent()
    return _world_signal_agent
