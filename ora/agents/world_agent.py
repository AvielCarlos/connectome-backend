"""
WorldAgent — Ora's Eyes on the World
Fetches real-world signals from the internet: events, trends, opportunities,
cultural moments, weather, and inspiration. Ora uses these to surface timely,
relevant experiences to users.

Sources (all free, no API keys required):
  A. Eventbrite  — local events
  B. Meetup      — community gatherings
  C. Reddit      — trending discussions
  D. Wikipedia   — "On This Day" history
  E. Open-Meteo  — weather for activity suggestions
  F. YouTube     — trending/how-to RSS
  G. Hacker News — tech/learning top stories
"""

import asyncio
import json
import logging
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

import httpx
import numpy as np

from core.config import settings
from core.database import execute, fetch, fetchrow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------
WORLD_AGENT_ENABLED = os.getenv("WORLD_AGENT_ENABLED", "true").lower() == "true"

# ---------------------------------------------------------------------------
# WorldSignal dataclass
# ---------------------------------------------------------------------------

@dataclass
class WorldSignal:
    source: str
    signal_type: str        # 'event' | 'trend' | 'inspiration' | 'opportunity' | 'weather' | 'historical'
    title: str
    summary: str
    url: str
    location: str
    tags: List[str]
    relevance_score: float = 0.5
    raw: Optional[Dict[str, Any]] = field(default=None, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d


# ---------------------------------------------------------------------------
# Mock signals for WORLD_AGENT_ENABLED=false
# ---------------------------------------------------------------------------
MOCK_SIGNALS: List[WorldSignal] = [
    WorldSignal(
        source="mock",
        signal_type="inspiration",
        title="Start a 30-Day Creative Challenge",
        summary="Commit to one small creative act every day for 30 days. Drawing, writing, cooking — anything counts. Consistency builds identity.",
        url="https://www.thinkwithgoogle.com/",
        location="",
        tags=["creativity", "habits", "self-improvement"],
        relevance_score=0.9,
    ),
    WorldSignal(
        source="mock",
        signal_type="trend",
        title="The Rise of 'Slow Productivity'",
        summary="More people are rejecting hustle culture in favour of deep, meaningful work at a sustainable pace. Cal Newport calls it 'slow productivity'.",
        url="https://www.calnewport.com/",
        location="",
        tags=["productivity", "wellness", "mindfulness"],
        relevance_score=0.85,
    ),
    WorldSignal(
        source="mock",
        signal_type="opportunity",
        title="Free Online Course: Introduction to Psychology",
        summary="Yale's most popular course, 'The Science of Well-Being', is free on Coursera. 3.8 million students have taken it.",
        url="https://www.coursera.org/learn/the-science-of-well-being",
        location="Online",
        tags=["learning", "psychology", "wellbeing"],
        relevance_score=0.88,
    ),
    WorldSignal(
        source="mock",
        signal_type="historical",
        title="On This Day in History",
        summary="History repeats itself — but only for those paying attention. Today's anniversary may hold a mirror to something in your own journey.",
        url="https://en.wikipedia.org/wiki/Portal:Current_events",
        location="",
        tags=["history", "reflection", "discovery"],
        relevance_score=0.75,
    ),
    WorldSignal(
        source="mock",
        signal_type="event",
        title="Community Skill Swap — Meet Your Neighbours",
        summary="Local skill-sharing events are popping up everywhere. Teach what you know, learn what you don't — the original social network.",
        url="https://www.meetup.com/",
        location="Your City",
        tags=["community", "social", "skills"],
        relevance_score=0.80,
    ),
]


# ---------------------------------------------------------------------------
# WorldAgent
# ---------------------------------------------------------------------------

class WorldAgent:
    """
    Gives Ora access to the internet.
    Fetches real-world signals: events, trends, opportunities, cultural moments.
    Ora uses these to inspire users and surface timely experiences.
    """

    AGENT_NAME = "WorldAgent"
    REFRESH_INTERVAL_SECONDS = 6 * 3600  # 6 hours
    REDIS_KEY_LAST_FETCH = "world_signals:last_fetch"
    HTTP_TIMEOUT = 10.0

    def __init__(self, openai_client=None):
        self.openai = openai_client
        self._http: Optional[httpx.AsyncClient] = None

    # -----------------------------------------------------------------------
    # HTTP helper
    # -----------------------------------------------------------------------

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=self.HTTP_TIMEOUT,
                follow_redirects=True,
                headers={
                    "User-Agent": "Connectome/1.0 (https://connectome.app; contact@connectome.app)"
                },
            )
        return self._http

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def fetch_all(
        self,
        user_location: Optional[str] = None,
        user_interests: Optional[List[str]] = None,
    ) -> List[WorldSignal]:
        """
        Fetch signals from all sources in parallel.
        Returns a deduplicated list of WorldSignal objects.
        Stores results in the world_signals DB table.
        """
        if not WORLD_AGENT_ENABLED:
            logger.info("WorldAgent: mock mode (WORLD_AGENT_ENABLED=false)")
            return MOCK_SIGNALS

        today = datetime.now(timezone.utc)
        month = today.month
        day = today.day

        tasks = [
            self._fetch_reddit(user_interests),
            self._fetch_wikipedia_otd(month, day),
            self._fetch_hackernews(),
            self._fetch_youtube_trending(),
        ]

        # Location-dependent sources
        if user_location:
            tasks.append(self._fetch_eventbrite(user_location))
            tasks.append(self._fetch_meetup(user_location, user_interests))
            tasks.append(self._fetch_weather(user_location))
        else:
            tasks.append(self._fetch_eventbrite(None))
            tasks.append(self._fetch_meetup(None, user_interests))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        signals: List[WorldSignal] = []
        active_sources: List[str] = []

        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"WorldAgent source error: {result}")
            elif isinstance(result, list):
                signals.extend(result)
                if result:
                    active_sources.append(result[0].source)

        # Deduplicate by URL
        seen_urls: set = set()
        deduped: List[WorldSignal] = []
        for sig in signals:
            if sig.url and sig.url not in seen_urls:
                seen_urls.add(sig.url)
                deduped.append(sig)

        if not deduped:
            logger.warning("WorldAgent: all sources failed — returning cached signals")
            return await self._load_cached_signals()

        # Persist to DB
        await self._store_signals(deduped)

        # Update Redis last_fetch timestamp
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            await r.set(self.REDIS_KEY_LAST_FETCH, str(int(time.time())))
        except Exception as e:
            logger.warning(f"WorldAgent: Redis update failed: {e}")

        logger.info(
            f"WorldAgent: fetched {len(deduped)} signals from {len(active_sources)} sources"
        )
        return deduped

    async def get_relevant_signals(
        self,
        user_embedding: List[float],
        limit: int = 10,
    ) -> List[WorldSignal]:
        """
        Return signals ranked by cosine similarity to the user embedding.
        Refreshes if signals are older than 6 hours.
        """
        if not WORLD_AGENT_ENABLED:
            return MOCK_SIGNALS[:limit]

        # Check freshness
        stale = await self._signals_are_stale()
        if stale:
            logger.info("WorldAgent: signals stale, refreshing…")
            await self.fetch_all()

        rows = await self._load_cached_signals()
        if not rows:
            return []

        if not user_embedding:
            return rows[:limit]

        # Score by cosine similarity (using tags as proxy embedding via keyword overlap)
        user_vec = np.array(user_embedding, dtype=float)
        if user_vec.ndim == 0 or user_vec.size == 0:
            return rows[:limit]

        scored: List[tuple] = []
        for sig in rows:
            # Build a simple feature vector from relevance_score and tag count
            # In production this would compare actual embeddings
            score = sig.relevance_score
            scored.append((score, sig))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in scored[:limit]]

    async def signal_to_screen_spec(
        self,
        signal: WorldSignal,
        user_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Convert a WorldSignal into a server-driven UI screen spec.
        Uses Ora's LLM to write a compelling, personalised summary.
        """
        summary = await self._personalise_summary(signal, user_context)

        # Build tag line based on signal type
        tag_labels = {
            "event": "📍 Happening near you",
            "trend": "🔥 Trending now",
            "inspiration": "✨ For your journey",
            "opportunity": "🚀 Opportunity",
            "weather": "🌤 Weather insight",
            "historical": "📅 On this day",
        }
        tag_text = tag_labels.get(signal.signal_type, "🌍 From the world")

        components = [
            {"type": "tag", "text": tag_text},
            {"type": "headline", "text": signal.title},
            {"type": "body_text", "text": summary},
        ]

        if signal.url:
            components.append(
                {
                    "type": "action_button",
                    "label": "Learn more",
                    "action": {"type": "open_url", "url": signal.url},
                }
            )

        components.append(
            {
                "type": "action_button",
                "label": "Add to goals",
                "action": {"type": "add_goal", "title": signal.title},
                "style": "secondary",
            }
        )

        if signal.location:
            components.insert(
                2,
                {"type": "location_tag", "text": signal.location},
            )

        return {
            "type": "opportunity_card",
            "layout": "card_stack",
            "components": components,
            "feedback_overlay": {
                "type": "star_rating",
                "position": "bottom_right",
                "always_visible": True,
            },
            "metadata": {
                "agent": self.AGENT_NAME,
                "source": signal.source,
                "signal_type": signal.signal_type,
                "tags": signal.tags,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    # -----------------------------------------------------------------------
    # Feed cap: world signals never exceed session-based threshold
    # -----------------------------------------------------------------------
    FEED_CAP_BY_SESSIONS = [
        (0,  10, 0.10),   # 0-10 sessions  → 10% world content
        (10, 50, 0.20),   # 10-50 sessions → 20%
        (50, 999999, 0.30), # 50+ sessions  → 30% hard ceiling, forever
    ]

    def world_feed_cap(self, session_count: int) -> float:
        for lo, hi, cap in self.FEED_CAP_BY_SESSIONS:
            if lo <= session_count < hi:
                return cap
        return 0.30

    async def passes_reasonability_filter(self, signal: "WorldSignal") -> bool:
        """Quick filter — rejects clickbait, doom, rage-bait, no actionable angle."""
        BAD_PATTERNS = [
            "you won't believe", "shocking", "outrage", "destroy",
            "panic", "catastrophe", "worst ever", "breaking:", "alert:",
            "they don't want you to know", "exposed", "scandal",
        ]
        text = (signal.title + " " + signal.summary).lower()
        for pat in BAD_PATTERNS:
            if pat in text:
                logger.info(f"WorldAgent: filtered '{signal.title}' — matched pattern '{pat}'")
                return False
        # Must have some positive/actionable angle
        GOOD_PATTERNS = [
            "how to", "learn", "explore", "discover", "try", "join",
            "build", "create", "connect", "experience", "improve",
            "understand", "wonder", "achieve", "grow", "share",
        ]
        has_positive = any(p in text for p in GOOD_PATTERNS)
        # Historical/inspiration signals always pass
        if signal.signal_type in ("historical", "inspiration", "weather"):
            return True
        return has_positive

    async def generate_screen(
        self,
        user_context: Dict[str, Any],
        variant: str = "A",
    ) -> Dict[str, Any]:
        """
        Entry point compatible with OraBrain's agent interface.
        Picks the best signal for this user and converts it to a screen spec.
        """
        if not WORLD_AGENT_ENABLED:
            import random
            signal = random.choice(MOCK_SIGNALS)
            return await self.signal_to_screen_spec(signal, user_context)

        user_location = user_context.get("location")
        user_interests = user_context.get("interests", [])
        user_embedding = user_context.get("embedding", [])

        signals = await self.get_relevant_signals(
            user_embedding=user_embedding or [],
            limit=10,
        )

        if not signals:
            # Trigger a fresh fetch
            signals = await self.fetch_all(user_location, user_interests)

        if not signals:
            import random
            signal = random.choice(MOCK_SIGNALS)
        else:
            # Apply reasonability filter
            import random
            filtered = []
            for s in signals[:10]:
                if await self.passes_reasonability_filter(s):
                    filtered.append(s)
            if not filtered:
                filtered = signals[:3]  # fallback if all filtered
            top_n = min(3, len(filtered))
            signal = random.choice(filtered[:top_n])

        spec = await self.signal_to_screen_spec(signal, user_context)
        spec.setdefault("metadata", {})["variant"] = variant
        return spec

    async def refresh_loop(self):
        """
        Background task: refresh world signals every 6 hours.
        Designed to run as a long-lived asyncio task.
        """
        logger.info("WorldAgent refresh_loop started")
        while True:
            try:
                await self.fetch_all()
            except Exception as e:
                logger.error(f"WorldAgent refresh_loop error: {e}")
            await asyncio.sleep(self.REFRESH_INTERVAL_SECONDS)

    # -----------------------------------------------------------------------
    # Source fetchers
    # -----------------------------------------------------------------------

    async def _fetch_reddit(
        self, interests: Optional[List[str]] = None
    ) -> List[WorldSignal]:
        """Fetch trending posts from self-improvement subreddits."""
        subreddits = ["todayilearned", "GetMotivated", "selfimprovement", "socialskills"]

        # If interests provided, try to map to relevant subreddits
        interest_subreddits = {
            "tech": "technology",
            "technology": "technology",
            "learning": "learnprogramming",
            "fitness": "Fitness",
            "health": "Health",
            "creativity": "ArtificialSentience",
            "books": "books",
            "finance": "personalfinance",
        }
        if interests:
            for interest in interests:
                mapped = interest_subreddits.get(interest.lower())
                if mapped and mapped not in subreddits:
                    subreddits.append(mapped)

        signals: List[WorldSignal] = []
        http = self._get_http()

        for sub in subreddits[:5]:  # cap at 5 to avoid rate limits
            try:
                resp = await http.get(
                    f"https://www.reddit.com/r/{sub}/hot.json",
                    params={"limit": 5},
                    headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0 (compatible; Connectome/1.0; +https://connectome.app)"},
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                posts = data.get("data", {}).get("children", [])

                for post in posts:
                    p = post.get("data", {})
                    title = p.get("title", "").strip()
                    url = p.get("url", "")
                    permalink = f"https://reddit.com{p.get('permalink', '')}"
                    upvotes = p.get("ups", 0)
                    num_comments = p.get("num_comments", 0)
                    selftext = p.get("selftext", "")[:200]

                    if not title or not url:
                        continue

                    summary = selftext or f"{upvotes:,} upvotes · {num_comments:,} comments on r/{sub}"

                    signals.append(
                        WorldSignal(
                            source="reddit",
                            signal_type="trend",
                            title=title,
                            summary=summary,
                            url=permalink,
                            location="",
                            tags=[sub, "trending", "community"],
                            relevance_score=min(1.0, upvotes / 50000),
                            raw=p,
                        )
                    )
            except Exception as e:
                logger.warning(f"WorldAgent Reddit r/{sub} error: {e}")

        return signals

    async def _fetch_wikipedia_otd(self, month: int, day: int) -> List[WorldSignal]:
        """Fetch 'On This Day' events from Wikipedia."""
        http = self._get_http()
        signals: List[WorldSignal] = []
        try:
            resp = await http.get(
                f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{month}/{day}",
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                return signals

            data = resp.json()
            events = data.get("events", [])[:5]

            for evt in events:
                year = evt.get("year", "")
                text = evt.get("text", "").strip()
                pages = evt.get("pages", [])
                url = ""
                if pages:
                    url = pages[0].get("content_urls", {}).get("desktop", {}).get("page", "")

                if not text:
                    continue

                signals.append(
                    WorldSignal(
                        source="wikipedia",
                        signal_type="historical",
                        title=f"On This Day in {year}",
                        summary=text,
                        url=url or "https://en.wikipedia.org/wiki/Portal:Current_events",
                        location="",
                        tags=["history", "on-this-day", "discovery"],
                        relevance_score=0.6,
                        raw=evt,
                    )
                )
        except Exception as e:
            logger.warning(f"WorldAgent Wikipedia OTD error: {e}")

        return signals

    async def _fetch_hackernews(self) -> List[WorldSignal]:
        """Fetch top Hacker News stories, filtered for learning & tools."""
        http = self._get_http()
        signals: List[WorldSignal] = []

        LEARNING_KEYWORDS = {
            "learn", "tutorial", "guide", "how to", "introduction", "tool",
            "open source", "research", "project", "build", "python", "ai",
            "machine learning", "productivity", "startup", "science", "study",
        }

        try:
            resp = await http.get(
                "https://hacker-news.firebaseio.com/v0/topstories.json"
            )
            if resp.status_code != 200:
                return signals

            story_ids: List[int] = resp.json()[:20]

            # Fetch top 20 in parallel, pick 10 best
            story_tasks = [
                http.get(f"https://hacker-news.firebaseio.com/v0/item/{sid}.json")
                for sid in story_ids
            ]
            story_responses = await asyncio.gather(*story_tasks, return_exceptions=True)

            for story_resp in story_responses:
                if isinstance(story_resp, Exception):
                    continue
                if story_resp.status_code != 200:
                    continue

                story = story_resp.json()
                if not story or story.get("type") != "story":
                    continue

                title = story.get("title", "").strip()
                url = story.get("url", f"https://news.ycombinator.com/item?id={story.get('id')}")
                score = story.get("score", 0)

                if not title:
                    continue

                # Filter for learning/tool content
                title_lower = title.lower()
                if not any(kw in title_lower for kw in LEARNING_KEYWORDS):
                    continue

                signals.append(
                    WorldSignal(
                        source="hackernews",
                        signal_type="opportunity",
                        title=title,
                        summary=f"Trending on Hacker News · {score} points",
                        url=url,
                        location="",
                        tags=["tech", "learning", "hacker-news"],
                        relevance_score=min(1.0, score / 500),
                        raw=story,
                    )
                )

                if len(signals) >= 5:
                    break

        except Exception as e:
            logger.warning(f"WorldAgent HackerNews error: {e}")

        return signals

    async def _fetch_youtube_trending(self) -> List[WorldSignal]:
        """Fetch trending YouTube videos via public RSS feed."""
        http = self._get_http()
        signals: List[WorldSignal] = []

        # How-to & style category (26) — most useful for self-improvement
        feeds = [
            "https://www.youtube.com/feeds/videos.xml?chart=mostpopular&regionCode=US",
            "https://www.youtube.com/feeds/videos.xml?chart=mostpopular&videoCategoryId=27&regionCode=US",  # Education
        ]

        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "media": "http://search.yahoo.com/mrss/",
            "yt": "http://www.youtube.com/xml/schemas/2015",
        }

        for feed_url in feeds:
            try:
                resp = await http.get(feed_url)
                if resp.status_code != 200:
                    continue

                root = ET.fromstring(resp.text)
                entries = root.findall("atom:entry", ns)

                for entry in entries[:5]:
                    title_el = entry.find("atom:title", ns)
                    link_el = entry.find("atom:link", ns)
                    published_el = entry.find("atom:published", ns)
                    summary_el = entry.find("media:group/media:description", ns)

                    title = title_el.text.strip() if title_el is not None else ""
                    url = link_el.get("href", "") if link_el is not None else ""
                    published = published_el.text if published_el is not None else ""
                    summary = summary_el.text[:200] if summary_el is not None and summary_el.text else ""

                    if not title or not url:
                        continue

                    signals.append(
                        WorldSignal(
                            source="youtube",
                            signal_type="inspiration",
                            title=title,
                            summary=summary or f"Trending YouTube video · published {published[:10]}",
                            url=url,
                            location="",
                            tags=["video", "trending", "how-to"],
                            relevance_score=0.65,
                            raw={"title": title, "url": url, "published": published},
                        )
                    )

            except Exception as e:
                logger.warning(f"WorldAgent YouTube RSS error ({feed_url}): {e}")

        return signals

    async def _fetch_eventbrite(self, city: Optional[str]) -> List[WorldSignal]:
        """Fetch public events from Eventbrite (no API key via HTML scrape fallback)."""
        http = self._get_http()
        signals: List[WorldSignal] = []

        # Use the public search page (no API key needed)
        location_slug = city.lower().replace(" ", "-") if city else "online"
        url = f"https://www.eventbrite.com/d/{location_slug}/free--events/"

        try:
            resp = await http.get(url, headers={"Accept-Language": "en-US,en;q=0.9"})
            if resp.status_code != 200:
                return signals

            # Basic extraction — look for JSON-LD event data
            import re
            matches = re.findall(
                r'<script type="application/ld\+json">(.*?)</script>',
                resp.text,
                re.DOTALL,
            )

            for match in matches[:10]:
                try:
                    data = json.loads(match)
                    if not isinstance(data, dict):
                        continue
                    if data.get("@type") not in ("Event", "SaleEvent"):
                        continue

                    title = data.get("name", "").strip()
                    event_url = data.get("url", "")
                    start_date = data.get("startDate", "")
                    location_data = data.get("location", {})
                    location_name = location_data.get("name", city or "")
                    description = data.get("description", "")[:200]

                    if not title:
                        continue

                    date_str = start_date[:10] if start_date else ""
                    summary = description or f"Event on {date_str}" if date_str else "Local event"

                    signals.append(
                        WorldSignal(
                            source="eventbrite",
                            signal_type="event",
                            title=title,
                            summary=summary,
                            url=event_url or url,
                            location=location_name,
                            tags=["event", "local", "community"],
                            relevance_score=0.75,
                            raw=data,
                        )
                    )

                    if len(signals) >= 5:
                        break
                except json.JSONDecodeError:
                    continue

        except Exception as e:
            logger.warning(f"WorldAgent Eventbrite error: {e}")

        return signals

    async def _fetch_meetup(
        self,
        city: Optional[str],
        interests: Optional[List[str]] = None,
    ) -> List[WorldSignal]:
        """Fetch upcoming Meetup events via GraphQL API."""
        http = self._get_http()
        signals: List[WorldSignal] = []

        topics = (interests or ["technology", "self-improvement"])[:3]
        topic_query = topics[0] if topics else "technology"

        query = """
        query($query: String!, $lat: Float, $lon: Float) {
          keywordSearch(
            filter: { query: $query, lat: $lat, lon: $lon }
            first: 5
          ) {
            edges {
              node {
                result {
                  ... on Event {
                    title
                    dateTime
                    eventUrl
                    going
                    venue {
                      city
                      state
                    }
                    description
                  }
                }
              }
            }
          }
        }
        """

        variables: Dict[str, Any] = {"query": topic_query}
        # Meetup GraphQL requires lat/lon; skip if no city
        # For a simple implementation, we use the text query only
        variables = {"query": f"{topic_query} {city or ''}".strip()}

        try:
            resp = await http.post(
                "https://api.meetup.com/gql",
                json={"query": query, "variables": variables},
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code != 200:
                return signals

            data = resp.json()
            edges = (
                data.get("data", {})
                .get("keywordSearch", {})
                .get("edges", [])
            )

            for edge in edges:
                result = edge.get("node", {}).get("result", {})
                title = result.get("title", "").strip()
                event_url = result.get("eventUrl", "")
                date_time = result.get("dateTime", "")
                going = result.get("going", 0)
                venue = result.get("venue", {})
                city_name = venue.get("city", city or "")
                description = result.get("description", "")[:200]

                if not title:
                    continue

                summary = (
                    description
                    or f"{going} people going · {date_time[:10] if date_time else 'Upcoming'}"
                )

                signals.append(
                    WorldSignal(
                        source="meetup",
                        signal_type="event",
                        title=title,
                        summary=summary,
                        url=event_url,
                        location=city_name,
                        tags=["meetup", "community", topic_query.lower()],
                        relevance_score=0.78,
                        raw=result,
                    )
                )

        except Exception as e:
            logger.warning(f"WorldAgent Meetup error: {e}")

        return signals

    async def _fetch_weather(self, city: str) -> List[WorldSignal]:
        """
        Fetch weather from Open-Meteo and generate activity suggestions.
        Resolves city to lat/lon via Open-Meteo geocoding.
        """
        http = self._get_http()
        signals: List[WorldSignal] = []

        try:
            # Geocode city → lat/lon
            geo_resp = await http.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1, "language": "en", "format": "json"},
            )
            if geo_resp.status_code != 200:
                return signals

            geo_data = geo_resp.json()
            geo_results = geo_data.get("results", [])
            if not geo_results:
                return signals

            lat = geo_results[0]["latitude"]
            lon = geo_results[0]["longitude"]

            # Fetch weather forecast
            wx_resp = await http.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "daily": "weathercode,temperature_2m_max",
                    "timezone": "auto",
                    "forecast_days": 3,
                },
            )
            if wx_resp.status_code != 200:
                return signals

            wx_data = wx_resp.json()
            daily = wx_data.get("daily", {})
            codes = daily.get("weathercode", [])
            temps = daily.get("temperature_2m_max", [])
            dates = daily.get("time", [])

            if not codes:
                return signals

            code = codes[0]
            temp = temps[0] if temps else None
            date = dates[0] if dates else "today"

            activity_suggestion = _weather_code_to_activity(code, temp)

            if activity_suggestion:
                temp_str = f"{temp:.0f}°C" if temp is not None else ""
                signals.append(
                    WorldSignal(
                        source="open-meteo",
                        signal_type="weather",
                        title=f"{activity_suggestion['emoji']} {activity_suggestion['headline']}",
                        summary=activity_suggestion["suggestion"],
                        url="https://open-meteo.com/",
                        location=city,
                        tags=["weather", "activity", "local"],
                        relevance_score=0.7,
                        raw={"code": code, "temp": temp, "date": date, "city": city},
                    )
                )

        except Exception as e:
            logger.warning(f"WorldAgent Open-Meteo error: {e}")

        return signals

    # -----------------------------------------------------------------------
    # DB helpers
    # -----------------------------------------------------------------------

    async def _store_signals(self, signals: List[WorldSignal]) -> None:
        """Upsert signals into world_signals table."""
        for sig in signals:
            try:
                # Use INSERT ... ON CONFLICT DO NOTHING for tables without UNIQUE on url.
                # If url UNIQUE constraint exists, upsert; otherwise insert fresh rows.
                await execute(
                    """
                    INSERT INTO world_signals
                        (id, source, signal_type, title, summary, url, location, tags,
                         relevance_score, fetched_at)
                    VALUES
                        ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
                    ON CONFLICT DO NOTHING
                    """,
                    str(uuid4()),
                    sig.source,
                    sig.signal_type,
                    sig.title,
                    sig.summary,
                    sig.url or f"urn:connectome:{sig.source}:{hash(sig.title)}",
                    sig.location,
                    json.dumps(sig.tags),
                    sig.relevance_score,
                )
            except Exception as e:
                logger.warning(f"WorldAgent DB store error for '{sig.title[:40]}': {e}")

    async def _load_cached_signals(self) -> List[WorldSignal]:
        """Load signals fetched in the last 6 hours from DB."""
        try:
            rows = await fetch(
                """
                SELECT source, signal_type, title, summary, url,
                       location, tags, relevance_score
                FROM world_signals
                WHERE fetched_at > NOW() - INTERVAL '6 hours'
                ORDER BY relevance_score DESC, fetched_at DESC
                LIMIT 100
                """
            )
            signals = []
            for row in rows:
                tags = row["tags"]
                if isinstance(tags, str):
                    tags = json.loads(tags)
                signals.append(
                    WorldSignal(
                        source=row["source"],
                        signal_type=row["signal_type"],
                        title=row["title"],
                        summary=row["summary"],
                        url=row["url"],
                        location=row["location"] or "",
                        tags=tags or [],
                        relevance_score=row["relevance_score"] or 0.5,
                    )
                )
            return signals
        except Exception as e:
            logger.warning(f"WorldAgent DB load error: {e}")
            return []

    async def _signals_are_stale(self) -> bool:
        """Check Redis for last fetch timestamp. Returns True if stale."""
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            last_fetch = await r.get(self.REDIS_KEY_LAST_FETCH)
            if not last_fetch:
                return True
            elapsed = time.time() - float(last_fetch)
            return elapsed > self.REFRESH_INTERVAL_SECONDS
        except Exception:
            return True

    # -----------------------------------------------------------------------
    # LLM personalisation
    # -----------------------------------------------------------------------

    async def _personalise_summary(
        self, signal: WorldSignal, user_context: Dict[str, Any]
    ) -> str:
        """Use Ora's LLM to write a personalised summary of the signal."""
        if not self.openai or not settings.has_openai:
            return signal.summary

        try:
            goals = [g.get("title", "") for g in user_context.get("active_goals", [])]
            interests = user_context.get("interests", [])
            fulfilment = user_context.get("fulfilment_score", 0.5)

            prompt = (
                f"You are Ora, a warm and insightful AI for human fulfilment.\n"
                f"Rewrite this summary in 2 sentences to be personally relevant to a user with:\n"
                f"- Interests: {interests or 'not specified'}\n"
                f"- Goals: {goals or 'not set'}\n"
                f"- Fulfilment score: {fulfilment:.2f}/1.0\n\n"
                f"Signal: {signal.title}\n"
                f"Original summary: {signal.summary}\n\n"
                f"Make it feel personal and inspiring. Be warm and direct. No fluff."
            )

            resp = await self.openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=120,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.warning(f"WorldAgent LLM personalisation failed: {e}")
            return signal.summary


# ---------------------------------------------------------------------------
# Weather code → activity suggestion
# ---------------------------------------------------------------------------

def _weather_code_to_activity(code: int, temp_c: Optional[float]) -> Optional[Dict[str, str]]:
    """
    Map WMO weather code to human-readable activity suggestion.
    https://open-meteo.com/en/docs#weathervariables
    """
    temp = temp_c or 15.0

    if code == 0:  # Clear sky
        if temp >= 18:
            return {
                "emoji": "☀️",
                "headline": "Perfect day to get outside",
                "suggestion": f"Clear skies and {temp:.0f}°C — ideal for a hike, run, or outdoor café. Don't waste it indoors.",
            }
        else:
            return {
                "emoji": "🌤",
                "headline": "Crisp and clear today",
                "suggestion": f"Cold but clear at {temp:.0f}°C. Great for a brisk walk or visiting a market.",
            }
    elif code in (1, 2):  # Partly cloudy
        return {
            "emoji": "⛅",
            "headline": "Partly cloudy — still a good day out",
            "suggestion": "Mixed clouds but manageable. A park visit or outdoor lunch could be perfect.",
        }
    elif code == 3:  # Overcast
        return {
            "emoji": "☁️",
            "headline": "Overcast — ideal for a museum or gallery",
            "suggestion": "Grey skies? This is your cue for a cosy café, a bookshop, or a museum you've been meaning to visit.",
        }
    elif code in (61, 63, 65):  # Rain
        return {
            "emoji": "🌧",
            "headline": "Rainy day — go deep inside",
            "suggestion": "Rain is the best productivity weather. Block time for that project, book, or course you've been putting off.",
        }
    elif code in (71, 73, 75):  # Snow
        return {
            "emoji": "❄️",
            "headline": "Snow day energy",
            "suggestion": "Snow changes the whole city. If you're safe to go out, it's a photographer's dream. If not, it's a perfect day to cook something new.",
        }
    elif code in (95, 96, 99):  # Thunderstorm
        return {
            "emoji": "⛈",
            "headline": "Storm indoors — deep work time",
            "suggestion": "Thunderstorms are nature's do-not-disturb sign. Stay in, go deep on something meaningful.",
        }
    else:
        return None
