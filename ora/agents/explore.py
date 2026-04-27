"""
ExploreAgent — Ora's real-world connector.
Generates experience cards that connect users to real venues, events,
courses, YouTube videos, and activities tailored to their goals and location.

V2 improvements:
- explore_depth parameter: surface / deep / serendipitous modes
- WorldAgent integration for real-time signals
- Past loved content influences category selection
- Seasonal events awareness
- Web trends via WorldAgent

Categories: adventure, food, learning, events, wellness, creative, social, travel
"""

import logging
import json
import random
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

CATEGORIES = [
    {"id": "adventure", "emoji": "🪂", "label": "Adventure", "description": "Push your limits — skydiving, rock climbing, kayaking, and more"},
    {"id": "food", "emoji": "🍜", "label": "Food", "description": "Discover restaurants, cafes, and culinary experiences near you"},
    {"id": "learning", "emoji": "📚", "label": "Learn", "description": "Courses, workshops, and skill-building experiences"},
    {"id": "events", "emoji": "🎉", "label": "Events", "description": "Concerts, meetups, exhibitions, and local happenings"},
    {"id": "wellness", "emoji": "🧘", "label": "Wellness", "description": "Yoga studios, spas, meditation centres, and health venues"},
    {"id": "creative", "emoji": "🎨", "label": "Create", "description": "Art classes, pottery studios, music lessons, and maker spaces"},
    {"id": "social", "emoji": "👥", "label": "Social", "description": "Community groups, sports leagues, and social clubs"},
    {"id": "travel", "emoji": "✈️", "label": "Travel", "description": "Day trips, hidden gems, and weekend escapes"},
]

# Mock data keyed by city for realistic fallback
MOCK_VENUES: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
    "_default": {
        "adventure": [
            {
                "name": "SkyDive Center",
                "rating": 4.8,
                "address": "123 Airfield Rd, Local Area",
                "distance_hint": "~25 min drive",
                "maps_url": "https://maps.google.com/?q=skydive+center",
                "website_url": "https://www.dropzone.com",
                "phone": "",
                "image_url": "https://images.unsplash.com/photo-1540575467063-178a50c2df87?w=800",
                "ora_note": "Perfect for your goal of seeking adventure",
            },
            {
                "name": "Summit Rock Climbing Gym",
                "rating": 4.6,
                "address": "456 Fitness Ave",
                "distance_hint": "~10 min drive",
                "maps_url": "https://maps.google.com/?q=rock+climbing+gym",
                "website_url": "https://www.summitrockclimbing.com",
                "phone": "",
                "image_url": "https://images.unsplash.com/photo-1522163182402-834f871fd851?w=800",
                "ora_note": "Beginners welcome — they have intro sessions",
            },
            {
                "name": "Urban Kayak Adventures",
                "rating": 4.7,
                "address": "789 Waterfront Dr",
                "distance_hint": "~15 min drive",
                "maps_url": "https://maps.google.com/?q=kayak+adventures",
                "website_url": "https://www.urbankayak.com",
                "phone": "",
                "image_url": "https://images.unsplash.com/photo-1564415315949-7a0ee3dbe261?w=800",
                "ora_note": "Guided tours available on weekends",
            },
        ],
        "food": [
            {
                "name": "The Soulful Kitchen",
                "rating": 4.5,
                "address": "22 Main St",
                "distance_hint": "~5 min walk",
                "maps_url": "https://maps.google.com/?q=soulful+kitchen",
                "website_url": "",
                "phone": "",
                "image_url": "https://images.unsplash.com/photo-1504674900247-0877df9cc836?w=800",
                "ora_note": "Farm-to-table menu that changes weekly",
            },
            {
                "name": "Noodle House",
                "rating": 4.4,
                "address": "88 Dragon Rd",
                "distance_hint": "~8 min walk",
                "maps_url": "https://maps.google.com/?q=noodle+house",
                "website_url": "",
                "phone": "",
                "image_url": "https://images.unsplash.com/photo-1569050467447-ce54b3bbc37d?w=800",
                "ora_note": "Authentic broth, slow-cooked for 12 hours",
            },
        ],
        "wellness": [
            {
                "name": "Serenity Yoga Studio",
                "rating": 4.9,
                "address": "33 Calm Blvd",
                "distance_hint": "~7 min walk",
                "maps_url": "https://maps.google.com/?q=serenity+yoga",
                "website_url": "",
                "phone": "",
                "image_url": "https://images.unsplash.com/photo-1506126613408-eca07ce68773?w=800",
                "ora_note": "First class free for new students",
            },
            {
                "name": "The Float Spa",
                "rating": 4.7,
                "address": "55 Wellness Lane",
                "distance_hint": "~12 min drive",
                "maps_url": "https://maps.google.com/?q=float+spa",
                "website_url": "",
                "phone": "",
                "image_url": "https://images.unsplash.com/photo-1540555700478-4be289fbecef?w=800",
                "ora_note": "60-min float sessions — incredible for clarity",
            },
        ],
        "creative": [
            {
                "name": "Clay Studio Collective",
                "rating": 4.8,
                "address": "77 Arts District",
                "distance_hint": "~15 min drive",
                "maps_url": "https://maps.google.com/?q=clay+studio",
                "website_url": "",
                "phone": "",
                "image_url": "https://images.unsplash.com/photo-1565193566173-7a0ee3dbe261?w=800",
                "ora_note": "Drop-in pottery classes, no experience needed",
            },
        ],
        "learning": [
            {
                "name": "Coursera",
                "rating": 4.7,
                "address": "Online",
                "distance_hint": "Online",
                "maps_url": "",
                "website_url": "https://www.coursera.org",
                "phone": "",
                "image_url": "https://images.unsplash.com/photo-1501504905252-473c47e087f8?w=800",
                "ora_note": "Top-rated courses in tech, business, and creativity",
            },
            {
                "name": "MasterClass",
                "rating": 4.8,
                "address": "Online",
                "distance_hint": "Online",
                "maps_url": "",
                "website_url": "https://www.masterclass.com",
                "phone": "",
                "image_url": "https://images.unsplash.com/photo-1434030216411-0b793f4b4173?w=800",
                "ora_note": "Learn from the world's best in any field",
            },
        ],
        "events": [
            {
                "name": "Eventbrite",
                "rating": 4.3,
                "address": "Various Locations",
                "distance_hint": "Near you",
                "maps_url": "",
                "website_url": "https://www.eventbrite.com",
                "phone": "",
                "image_url": "https://images.unsplash.com/photo-1492684223066-81342ee5ff30?w=800",
                "ora_note": "Thousands of events happening in your city",
            },
            {
                "name": "Meetup",
                "rating": 4.5,
                "address": "Various Locations",
                "distance_hint": "Near you",
                "maps_url": "",
                "website_url": "https://www.meetup.com",
                "phone": "",
                "image_url": "https://images.unsplash.com/photo-1511795409834-ef04bbd61622?w=800",
                "ora_note": "Find your tribe — groups for every interest",
            },
        ],
        "social": [
            {
                "name": "Local Sports League",
                "rating": 4.4,
                "address": "Recreation Centre",
                "distance_hint": "~10 min drive",
                "maps_url": "https://maps.google.com/?q=sports+league",
                "website_url": "",
                "phone": "",
                "image_url": "https://images.unsplash.com/photo-1461896836934-ffe607ba8211?w=800",
                "ora_note": "Join a team, meet people, get active",
            },
        ],
        "travel": [
            {
                "name": "Atlas Obscura",
                "rating": 4.9,
                "address": "Hidden gems near you",
                "distance_hint": "Varies",
                "maps_url": "",
                "website_url": "https://www.atlasobscura.com",
                "phone": "",
                "image_url": "https://images.unsplash.com/photo-1488646953014-85cb44e25828?w=800",
                "ora_note": "Discover the world's most extraordinary places",
            },
        ],
    }
}

CATEGORY_GOAL_MAP = {
    "adventure": "Experience something thrilling — {name}",
    "food": "Explore the culinary scene — visit {name}",
    "learning": "Expand my knowledge at {name}",
    "events": "Attend a local event",
    "wellness": "Prioritise my wellbeing — try {name}",
    "creative": "Explore my creative side at {name}",
    "social": "Build connections through {name}",
    "travel": "Explore new places via {name}",
}

HEADLINES: Dict[str, List[str]] = {
    "adventure": [
        "Your next adrenaline hit awaits",
        "Life's too short for the same routine",
        "Push beyond your comfort zone",
    ],
    "food": [
        "Taste something new today",
        "Food is culture — explore it",
        "Your city's best kept culinary secrets",
    ],
    "learning": [
        "Every expert was once a beginner",
        "The best investment is in yourself",
        "Learn something new this week",
    ],
    "events": [
        "Something worth leaving the house for",
        "Your next great story starts here",
        "Real life is happening nearby",
    ],
    "wellness": [
        "Your body and mind deserve this",
        "Restoration is productive",
        "Invest in yourself today",
    ],
    "creative": [
        "Create something with your hands",
        "Expression is fulfilment",
        "You're more creative than you think",
    ],
    "social": [
        "Connection is the root of fulfilment",
        "Your people are out there",
        "Community changes everything",
    ],
    "travel": [
        "Adventure is closer than you think",
        "There are places you haven't seen yet",
        "Wander with intention",
    ],
}

ORA_NOTES: Dict[str, List[str]] = {
    "adventure": [
        "Based on your active lifestyle goals, I picked experiences that push you forward.",
        "You've been working hard — time to add some adventure to balance it.",
        "Your Aventi domain is calling — here's how to answer it.",
    ],
    "food": [
        "Shared meals create shared memories. Here are places worth the trip.",
        "Your city has more to offer than you've explored. Start here.",
        "Food experiences are often where the best conversations happen.",
    ],
    "learning": [
        "You have goals that require new skills. These will help.",
        "The gap between where you are and where you want to be is mostly knowledge.",
        "Learning compounds. Each thing you learn opens three more doors.",
    ],
    "events": [
        "Real-world experiences are what Ora's feed can't replace. Go.",
        "You haven't had an unplanned evening in a while. This could be it.",
        "Events are where serendipity lives.",
    ],
    "wellness": [
        "You've been pushing. Rest is part of the performance.",
        "Your wellbeing score needs this. Trust the data.",
        "Recovery isn't weakness — it's strategy.",
    ],
    "creative": [
        "Creativity in your hands, not just your screen.",
        "You learn differently when you make things. Try it.",
        "Making something physical produces a kind of satisfaction screens can't touch.",
    ],
    "social": [
        "Your Eviva (contribution) domain shows potential. Community activates it.",
        "Humans are wired for belonging. These are places to find it.",
        "The quality of your connections shapes the quality of your life.",
    ],
    "travel": [
        "New environments produce new thoughts. You're overdue.",
        "Even a day trip changes your perspective. Here are some options.",
        "Distance — even small — creates clarity.",
    ],
}

# ── Serendipitous mode: categories the user hasn't recently visited ──────────
SERENDIPITOUS_WILDCARDS = [
    {"category": "creative", "hook": "Something you haven't tried yet"},
    {"category": "social", "hook": "An unexpected connection point"},
    {"category": "travel", "hook": "A day trip that could change your week"},
    {"category": "adventure", "hook": "A controlled dose of aliveness"},
    {"category": "events", "hook": "Serendipity lives in real rooms"},
]

# ── Seasonal events by month ─────────────────────────────────────────────────
SEASONAL_EVENTS: Dict[int, List[Dict[str, Any]]] = {
    1: [{"name": "New Year Goal Sprint", "category": "learning", "note": "January energy — perfect time to start that course you bookmarked."}],
    2: [{"name": "Valentine's Dinner Experiences", "category": "food", "note": "Curated culinary experiences for February."}],
    3: [{"name": "Spring Outdoor Adventures", "category": "adventure", "note": "Spring thaw means trails, bikes, and open air."}],
    4: [{"name": "Earth Month Wellness", "category": "wellness", "note": "April is Earth Month — outdoor yoga and nature walks."}],
    5: [{"name": "Festival Season Kicks Off", "category": "events", "note": "May marks the start of festival season in most cities."}],
    6: [{"name": "Summer Social Events", "category": "social", "note": "Long days = outdoor community. Make the most of it."}],
    7: [{"name": "Peak Adventure Season", "category": "adventure", "note": "The best month for pushing physical limits outdoors."}],
    8: [{"name": "Creative Summer Projects", "category": "creative", "note": "Summer light is perfect for art, photography, and making."}],
    9: [{"name": "Back-to-Learning Season", "category": "learning", "note": "September energy — courses, workshops, and skill-building."}],
    10: [{"name": "Autumn Cultural Events", "category": "events", "note": "Galleries, theatre, and cultural events peak in October."}],
    11: [{"name": "Gratitude & Community", "category": "social", "note": "November — community gatherings and meaningful connections."}],
    12: [{"name": "Year-End Reflection Retreats", "category": "wellness", "note": "December is for slowing down and going deeper."}],
}


class ExploreAgent:
    """
    Generates rich experience cards connecting users to real-world activities.

    explore_depth modes:
      - surface: fast picks based on top goals/interests (default)
      - deep: multi-source, past loved content + seasonal + WorldAgent
      - serendipitous: intentionally picks something unexpected/wildcard
    """

    def __init__(self, openai_client=None):
        self._openai = openai_client

    async def get_categories(self) -> List[Dict[str, Any]]:
        """Return all available explore categories."""
        return CATEGORIES

    async def generate_cards(
        self,
        user_context: Dict[str, Any],
        category: Optional[str] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        explore_depth: str = "surface",  # surface | deep | serendipitous
    ) -> List[Dict[str, Any]]:
        """
        Generate explore cards for the user.
        Returns a list of explore_card specs.

        explore_depth:
          surface — quick, goal-aligned picks (1 card)
          deep — richer: multiple sources, seasonal, past loved content (2-3 cards)
          serendipitous — picks something outside the user's comfort zone (1-2 cards)
        """
        city = user_context.get("user_city", "")
        country = user_context.get("user_country", "")

        # ── Pick category ────────────────────────────────────────────────
        if explore_depth == "serendipitous":
            category = self._pick_serendipitous_category(user_context)
        elif not category:
            category = self._pick_category(user_context, depth=explore_depth)

        # ── Fetch items ──────────────────────────────────────────────────
        items = []
        if settings.GOOGLE_PLACES_API_KEY and category not in ("learning", "events"):
            try:
                items = await self._search_google_places(category, city, country, lat, lon)
            except Exception as e:
                logger.warning(f"Google Places search failed: {e}")

        if not items:
            items = self._get_mock_items(category, city)
        if not items:
            items = MOCK_VENUES["_default"].get(category, MOCK_VENUES["_default"]["adventure"])

        items = items[:3]

        headline = random.choice(HEADLINES.get(category, ["Explore what's possible"]))
        ora_note = random.choice(ORA_NOTES.get(category, ["Here's something worth exploring."]))
        goal_title_template = CATEGORY_GOAL_MAP.get(category, "Explore {name}")
        first_item_name = items[0]["name"] if items else "this experience"
        save_as_goal_title = goal_title_template.format(name=first_item_name)

        # Depth-specific enrichment
        depth_badge = {"surface": None, "deep": "🔍 Deep", "serendipitous": "🎲 Surprise"}
        depth_tag = depth_badge.get(explore_depth)

        card = {
            "type": "explore_card",
            "category": category,
            "explore_depth": explore_depth,
            "headline": headline,
            "ora_note": ora_note,
            "items": items,
            "save_as_goal_title": save_as_goal_title,
            "depth_tag": depth_tag,
            "actions": [
                {"label": "Get Directions", "type": "directions"},
                {"label": "Visit Website", "type": "website"},
                {"label": "Save as Goal", "type": "save_goal"},
            ],
        }

        cards = [card]

        # ── Deep mode extras ─────────────────────────────────────────────
        if explore_depth == "deep":
            # 1. Seasonal event card if this month has one
            seasonal = self._get_seasonal_card(category)
            if seasonal:
                cards.append(seasonal)

            # 2. WorldAgent signal card
            world_card = await self._get_world_signal_card(user_context, category)
            if world_card:
                cards.append(world_card)

            # 3. Past loved content card
            past_card = await self._get_past_loved_content_card(user_context, category)
            if past_card:
                cards.append(past_card)

        # ── Video card (learning/adventure/wellness) ──────────────────────
        if self._openai and category in ("learning", "adventure", "wellness"):
            video_card = await self._generate_video_card(category, user_context)
            if video_card:
                cards.append(video_card)

        return cards

    def _pick_category(self, user_context: Dict[str, Any], depth: str = "surface") -> str:
        """Pick the most relevant category based on user context."""
        goals_text = user_context.get("goals_text", "").lower()
        interests = str(user_context.get("interests", "")).lower()
        combined = goals_text + " " + interests

        if any(w in combined for w in ["skydiv", "climb", "hike", "adventure", "thrill", "extreme"]):
            return "adventure"
        if any(w in combined for w in ["eat", "food", "cook", "restaurant", "cuisine"]):
            return "food"
        if any(w in combined for w in ["learn", "study", "course", "skill", "teach"]):
            return "learning"
        if any(w in combined for w in ["yoga", "meditat", "wellbeing", "health", "relax", "spa"]):
            return "wellness"
        if any(w in combined for w in ["paint", "draw", "pottery", "craft", "art", "creat"]):
            return "creative"
        if any(w in combined for w in ["friend", "social", "meet", "connect", "community"]):
            return "social"
        if any(w in combined for w in ["travel", "trip", "explore", "wander"]):
            return "travel"

        # Deep mode: bias toward seasonal or neglected domains
        if depth == "deep":
            month = datetime.now(timezone.utc).month
            seasonal = SEASONAL_EVENTS.get(month, [])
            if seasonal:
                return seasonal[0]["category"]

        return random.choice(["adventure", "food", "wellness", "learning", "creative"])

    def _pick_serendipitous_category(self, user_context: Dict[str, Any]) -> str:
        """
        Pick a category the user probably hasn't visited recently.
        Checks recent_categories in user_context to avoid repetition.
        Falls back to a random wildcard from SERENDIPITOUS_WILDCARDS.
        """
        recent = set(user_context.get("recent_explore_categories", []))
        wildcards = [w for w in SERENDIPITOUS_WILDCARDS if w["category"] not in recent]
        if wildcards:
            return random.choice(wildcards)["category"]
        return random.choice(SERENDIPITOUS_WILDCARDS)["category"]

    def _get_mock_items(self, category: str, city: str) -> List[Dict[str, Any]]:
        default_items = MOCK_VENUES["_default"].get(category, [])
        if not city:
            return default_items
        result = []
        for item in default_items:
            personalised = dict(item)
            if item.get("address") and item["address"] != "Online":
                personalised["address"] = f"{item['address'].split(',')[0]}, {city}"
            if item.get("maps_url") and "q=" in item["maps_url"]:
                query = item["maps_url"].split("q=")[1]
                personalised["maps_url"] = f"https://maps.google.com/?q={query}+{city.replace(' ', '+')}"
            result.append(personalised)
        return result

    def _get_seasonal_card(self, category: str) -> Optional[Dict[str, Any]]:
        """Return a seasonal event card if this month has one matching the category."""
        month = datetime.now(timezone.utc).month
        seasonal_events = SEASONAL_EVENTS.get(month, [])
        match = next((e for e in seasonal_events if e["category"] == category), None)
        if not match:
            # Fallback: return any seasonal event for this month
            match = seasonal_events[0] if seasonal_events else None
        if not match:
            return None

        return {
            "type": "seasonal_event_card",
            "category": match["category"],
            "headline": match["name"],
            "ora_note": match["note"],
            "explore_depth": "deep",
            "depth_tag": "🗓️ This Month",
            "items": [],
            "actions": [
                {"label": "Explore Now", "type": "website"},
                {"label": "Save as Goal", "type": "save_goal"},
            ],
        }

    async def _get_world_signal_card(
        self, user_context: Dict[str, Any], category: str
    ) -> Optional[Dict[str, Any]]:
        """
        Pull a world signal from the DB (recently fetched by WorldAgent)
        and format it as an explore card.
        """
        try:
            from core.database import fetch as db_fetch

            # Map category to signal types
            category_signal_map = {
                "events": ["event"],
                "learning": ["inspiration", "trend"],
                "adventure": ["opportunity", "trend"],
                "wellness": ["inspiration"],
                "creative": ["inspiration"],
                "travel": ["event", "opportunity"],
                "social": ["event"],
                "food": ["opportunity"],
            }
            signal_types = category_signal_map.get(category, ["trend", "inspiration"])

            # Fetch a recent signal from DB
            rows = await db_fetch(
                """
                SELECT title, summary, url, source, signal_type, tags
                FROM world_signals
                WHERE signal_type = ANY($1::text[])
                  AND fetched_at > NOW() - INTERVAL '48 hours'
                ORDER BY relevance_score DESC, fetched_at DESC
                LIMIT 3
                """,
                signal_types,
            )

            if not rows:
                return None

            row = random.choice(rows)
            tags = row["tags"] if isinstance(row["tags"], list) else []

            return {
                "type": "world_signal_card",
                "category": category,
                "explore_depth": "deep",
                "depth_tag": "🌍 World Signal",
                "headline": row["title"],
                "ora_note": row["summary"] or "A relevant signal from the wider world.",
                "source": row["source"],
                "url": row["url"],
                "tags": tags,
                "items": [],
                "actions": [
                    {"label": "Read More", "type": "open_url", "url": row["url"]},
                ],
            }
        except Exception as e:
            logger.debug(f"ExploreAgent._get_world_signal_card failed: {e}")
            return None

    async def _get_past_loved_content_card(
        self, user_context: Dict[str, Any], category: str
    ) -> Optional[Dict[str, Any]]:
        """
        Look at the user's highly-rated past screens and suggest revisiting
        a related theme. Returns None if no loved content found.
        """
        try:
            user_id = user_context.get("user_id")
            if not user_id:
                return None

            from core.database import fetch as db_fetch
            import uuid as _uuid

            # Find highly rated interactions from the last 30 days
            rows = await db_fetch(
                """
                SELECT ss.agent_type, ss.spec->>'headline' as headline
                FROM interactions i
                JOIN screen_specs ss ON i.screen_spec_id = ss.id
                WHERE i.user_id = $1
                  AND i.rating >= 4
                  AND i.created_at > NOW() - INTERVAL '30 days'
                ORDER BY i.rating DESC, i.created_at DESC
                LIMIT 5
                """,
                _uuid.UUID(user_id) if isinstance(user_id, str) else user_id,
            )

            if not rows:
                return None

            top = rows[0]
            agent_type = top["agent_type"] or "discovery"
            headline = top["headline"] or "Something you loved before"

            return {
                "type": "revisit_card",
                "category": category,
                "explore_depth": "deep",
                "depth_tag": "💜 You loved this",
                "headline": f"More like: {headline[:60]}",
                "ora_note": (
                    "You gave this a 4+ rating recently. I found more in the same vein — "
                    "worth exploring when you're in the same headspace."
                ),
                "source_agent": agent_type,
                "items": [],
                "actions": [
                    {"label": "Show me more", "type": "next_screen", "context": agent_type},
                ],
            }
        except Exception as e:
            logger.debug(f"ExploreAgent._get_past_loved_content_card failed: {e}")
            return None

    async def _search_google_places(
        self,
        category: str,
        city: str,
        country: str,
        lat: Optional[float],
        lon: Optional[float],
    ) -> List[Dict[str, Any]]:
        """Search Google Places Text Search API for real venues."""
        category_queries = {
            "adventure": f"adventure sports skydiving {city}",
            "food": f"best restaurants {city}",
            "wellness": f"yoga studio spa wellness {city}",
            "creative": f"pottery art studio creative class {city}",
            "social": f"sports club community group {city}",
            "travel": f"tourist attraction day trip near {city}",
        }

        query = category_queries.get(category, f"{category} {city}")

        payload: Dict[str, Any] = {
            "textQuery": query,
            "maxResultCount": 3,
        }
        if lat and lon:
            payload["locationBias"] = {
                "circle": {
                    "center": {"latitude": lat, "longitude": lon},
                    "radius": 50000.0,
                }
            }

        field_mask = ",".join([
            "places.displayName",
            "places.rating",
            "places.formattedAddress",
            "places.websiteUri",
            "places.googleMapsUri",
            "places.nationalPhoneNumber",
            "places.photos",
        ])

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": settings.GOOGLE_PLACES_API_KEY,
            "X-Goog-FieldMask": field_mask,
        }

        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                "https://places.googleapis.com/v1/places:searchText",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        places = data.get("places", [])
        items = []
        for place in places:
            name = place.get("displayName", {}).get("text", "")
            if not name:
                continue

            maps_url = place.get("googleMapsUri", f"https://maps.google.com/?q={name.replace(' ', '+')}")

            image_url = ""
            photos = place.get("photos", [])
            if photos:
                photo_name = photos[0].get("name", "")
                if photo_name:
                    image_url = (
                        f"https://places.googleapis.com/v1/{photo_name}/media"
                        f"?maxHeightPx=800&key={settings.GOOGLE_PLACES_API_KEY}"
                    )

            items.append({
                "name": name,
                "rating": place.get("rating", 0.0),
                "address": place.get("formattedAddress", city),
                "distance_hint": f"In {city}",
                "maps_url": maps_url,
                "website_url": place.get("websiteUri", ""),
                "phone": place.get("nationalPhoneNumber", ""),
                "image_url": image_url,
                "ora_note": "",
            })

        return items

    async def _generate_video_card(
        self,
        category: str,
        user_context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Generate a YouTube video recommendation card."""
        video_mocks = {
            "learning": {
                "title": "How to Learn Anything Faster (The Feynman Technique)",
                "channel": "Thomas Frank",
                "duration": "12:34",
                "thumbnail_url": "https://images.unsplash.com/photo-1497633762265-9d179a990aa6?w=800",
                "youtube_url": "https://www.youtube.com/results?search_query=feynman+technique+learning",
                "ora_note": "This technique alone could change how you approach every skill you want to build.",
            },
            "adventure": {
                "title": "First-Time Skydiving: Complete Beginner Guide",
                "channel": "Skydive TV",
                "duration": "15:22",
                "thumbnail_url": "https://images.unsplash.com/photo-1540575467063-178a50c2df87?w=800",
                "youtube_url": "https://www.youtube.com/results?search_query=first+time+skydiving+guide",
                "ora_note": "Watch this before you book — it'll make you excited, not nervous.",
            },
            "wellness": {
                "title": "10-Minute Morning Yoga for Beginners",
                "channel": "Yoga with Adriene",
                "duration": "10:00",
                "thumbnail_url": "https://images.unsplash.com/photo-1506126613408-eca07ce68773?w=800",
                "youtube_url": "https://www.youtube.com/results?search_query=morning+yoga+beginners+adriene",
                "ora_note": "The most-recommended yoga channel in the world. Start here.",
            },
        }

        mock = video_mocks.get(category)
        if not mock:
            return None

        return {
            "type": "video_card",
            "category": category,
            "title": mock["title"],
            "channel": mock["channel"],
            "duration": mock["duration"],
            "thumbnail_url": mock["thumbnail_url"],
            "youtube_url": mock["youtube_url"],
            "ora_note": mock["ora_note"],
        }
