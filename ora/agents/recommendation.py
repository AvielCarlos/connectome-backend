"""
RecommendationAgent
Generates content/product/action recommendation screens.
Selects proven high-rated content from the DB or generates new.
Uses explore/exploit logic: ~50% exploit proven content, ~50% generate new.
"""

import logging
import json
import random
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from core.config import settings
from core.database import fetch, fetchrow
from ora.content_quality import content_quality_check
from ora.data.activity_repository import get_activity_repository

logger = logging.getLogger(__name__)


MOCK_RECOMMENDATIONS = [
    {
        "title": "Atomic Habits",
        "type": "book",
        "description": "Every system of personal growth comes back to this book. If you haven't read it, read it. If you have — reread chapter 3.",
        "image": "https://images.unsplash.com/photo-1481627834876-b7833e8f5570?w=800",
        "url": "https://jamesclear.com/atomic-habits",
        "label": "Get the book",
        "category": "productivity",
    },
    {
        "title": "Waking Up App",
        "type": "app",
        "description": "Sam Harris built the best secular meditation tool. No mysticism, just science-backed techniques for training your mind.",
        "image": "https://images.unsplash.com/photo-1506905925346-21bda4d32df4?w=800",
        "url": "https://www.wakingup.com",
        "label": "Try free",
        "category": "mindfulness",
    },
    {
        "title": "NSDR / Yoga Nidra",
        "type": "practice",
        "description": "20 minutes of Non-Sleep Deep Rest restores mental performance as effectively as a 90-minute nap. Free on YouTube.",
        "image": "https://images.unsplash.com/photo-1588286840104-8957b019727f?w=800",
        "url": "https://www.youtube.com/watch?v=AKGrmY8ORSE",
        "label": "Try it now",
        "category": "wellness",
    },
    {
        "title": "Notion Templates",
        "type": "tool",
        "description": "A well-designed second brain changes how you think. Start with one template — a weekly review — and build from there.",
        "image": "https://images.unsplash.com/photo-1484480974693-6ca0a78fb36b?w=800",
        "url": "https://www.notion.so/templates",
        "label": "Browse templates",
        "category": "productivity",
    },
]


class RecommendationAgent:
    """
    Recommends content, tools, or practices based on user context.
    Exploits high-rated screen specs from DB or generates fresh ones.
    """

    AGENT_NAME = "RecommendationAgent"

    def __init__(self, openai_client=None):
        self.openai = openai_client

    async def generate_screen(
        self,
        user_context: Dict[str, Any],
        variant: str = "A",
        exploit: bool = False,
    ) -> Dict[str, Any]:
        """
        exploit=True: try to pull a proven screen from DB.
        exploit=False: generate something new, 50% from activity repository.
        """
        if exploit:
            proven = await self._get_proven_screen(user_context)
            if proven:
                return proven

        # Part 4: 50% chance to use activity repository
        use_repo = random.random() < 0.50
        if use_repo:
            result = await self._generate_from_activity_repo(user_context, variant)
            if result:
                for _ in range(3):
                    if content_quality_check(result):
                        return result
                    result = await self._generate_from_activity_repo(user_context, variant)
                    if not result:
                        break

        # Generate new content
        if self.openai and settings.has_openai:
            result = await self._generate_with_ai(user_context, variant)
        else:
            result = self._generate_mock(user_context, variant)

        # Quality filter with retries
        for _ in range(3):
            if content_quality_check(result):
                return result
            if self.openai and settings.has_openai:
                result = await self._generate_with_ai(user_context, variant)
            else:
                result = self._generate_mock(user_context, variant)
        return result

    async def _generate_from_activity_repo(
        self,
        user_context: Dict[str, Any],
        variant: str,
    ) -> Optional[Dict[str, Any]]:
        """Use activity repository as source material. Track shown activities in Redis."""
        try:
            user_id = user_context.get("user_id", "")
            domain = user_context.get("domain", "iVive")

            # Get activities already shown to user (within 30 days)
            exclude_ids = await self._get_shown_activities(user_id)

            repo = get_activity_repository()
            activities = repo.get_activities_for_domain(
                domain, exclude_ids=exclude_ids, limit=5
            )
            if not activities:
                return None

            activity = activities[0]

            # Track this activity as shown
            await self._mark_activity_shown(user_id, activity.get("id", activity["title"]))

            if self.openai and settings.has_openai:
                prompt = f"""You are Ora, recommending something specific and real to a user.
Base this recommendation on this actual activity/resource:

Activity: {activity['title']}
Description: {activity['description']}
Domain: {domain}
Tags: {', '.join(activity.get('tags', []))}
Difficulty: {activity['difficulty']}
Time: {activity['time_required']}
Cost: {activity['cost']}

Create a JSON recommendation card with:
- title: the resource/activity name
- type: one of [activity, practice, experience, resource, book, app, tool]
- description: 2-3 compelling sentences on WHY this specific thing matters
- label: CTA button text  
- category: one category word
- price_hint: "{activity['cost']}"
- domain: "{domain}"

Be specific. No motivational platitudes. Return ONLY valid JSON."""
                response = await self.openai.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7,
                    max_tokens=350,
                    response_format={"type": "json_object"},
                )
                rec = json.loads(response.choices[0].message.content)
                rec["domain"] = domain
                rec["activity_id"] = activity.get("id", "")
            else:
                rec = {
                    "title": activity["title"],
                    "type": "activity",
                    "description": activity["description"],
                    "label": "Try this",
                    "category": activity["tags"][0] if activity.get("tags") else "activity",
                    "price_hint": activity.get("cost", "free"),
                    "domain": domain,
                    "activity_id": activity.get("id", ""),
                }
            return self._build_spec(rec, variant)
        except Exception as e:
            logger.warning(f"RecommendationAgent: activity repo failed: {e}")
            return None

    async def _get_shown_activities(self, user_id: str) -> List[str]:
        """Get list of activity IDs shown to this user in the last 30 days from Redis."""
        if not user_id:
            return []
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            members = await r.smembers(f"user_activities:{user_id}")
            return list(members) if members else []
        except Exception:
            return []

    async def _mark_activity_shown(self, user_id: str, activity_id: str):
        """Mark an activity as shown to the user. Expires after 30 days."""
        if not user_id or not activity_id:
            return
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            key = f"user_activities:{user_id}"
            await r.sadd(key, activity_id)
            await r.expire(key, 30 * 24 * 3600)  # 30 days
        except Exception as e:
            logger.debug(f"_mark_activity_shown failed: {e}")

    async def _get_proven_screen(
        self, user_context: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Pull a top-rated screen spec from DB for this agent type."""
        rows = await fetch(
            """
            SELECT spec FROM screen_specs
            WHERE agent_type = $1
              AND global_rating >= 4.0
              AND impression_count >= 5
            ORDER BY global_rating DESC, impression_count DESC
            LIMIT 10
            """,
            self.AGENT_NAME,
        )
        if not rows:
            return None
        # Pick randomly among top 10 to avoid always showing the same one
        row = random.choice(rows)
        spec = dict(row["spec"])
        spec["metadata"]["variant"] = "exploit"
        spec["metadata"]["exploited"] = True
        return spec

    async def _generate_with_ai(
        self, user_context: Dict[str, Any], variant: str
    ) -> Dict[str, Any]:
        interests = user_context.get("interests", [])
        goals = user_context.get("active_goals", [])
        domain = user_context.get("domain", "iVive")

        DOMAIN_REC = {
            "iVive": "Recommend something that supports personal growth, health, self-improvement, or inner work. Think: supplements, journaling apps, therapy resources, meditation tools, fitness gear.",
            "Eviva": "Recommend something that helps the user contribute to others, advance meaningful work, or build community. Think: skills platforms, nonprofit tools, collaboration apps, social entrepreneurship resources.",
            "Aventi": "Recommend something that enhances experiences, joy, culture, or adventure. Think: travel apps, event finders, creative tools, culture guides, play-focused products.",
        }
        domain_hint = DOMAIN_REC.get(domain, DOMAIN_REC["iVive"])

        prompt = f"""You are Ora, an AI dedicated to human fulfilment.
Recommend one specific resource (book, app, tool, practice, or service) for:
- User interests: {interests or "general self-improvement"}
- Active goals: {[g["title"] for g in goals] or "none yet"}
- Domain focus: {domain} — {domain_hint}

Create a JSON recommendation with:
- title: the resource name
- type: one of [book, app, tool, practice, course, podcast, service]
- description: 2-3 sentences on why this specific resource matters and what makes it exceptional
- url: a real URL for the resource
- label: CTA button text
- category: relevant category
- price_hint: "free", "$", "$$", or "$$$" (optional)
- domain: "{domain}"

Return ONLY valid JSON, no markdown."""

        try:
            response = await self.openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=400,
                response_format={"type": "json_object"},
            )
            rec = json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.warning(f"OpenAI recommendation failed: {e}")
            return self._generate_mock(user_context, variant)

        return self._build_spec(rec, variant)

    def _generate_mock(
        self, user_context: Dict[str, Any], variant: str
    ) -> Dict[str, Any]:
        import hashlib
        uid = user_context.get("user_id", "anon")
        ts = str(int(datetime.now(timezone.utc).timestamp() / 14400))
        idx = int(hashlib.md5(f"{uid}{ts}rec".encode()).hexdigest(), 16) % len(
            MOCK_RECOMMENDATIONS
        )
        return self._build_spec(MOCK_RECOMMENDATIONS[idx], variant, is_mock=True)

    def _build_spec(
        self,
        rec: Dict[str, Any],
        variant: str,
        is_mock: bool = False,
    ) -> Dict[str, Any]:
        import uuid

        tracking_id = str(uuid.uuid4())
        url = rec.get("url", "")

        components = [
            {
                "type": "hero_image",
                "source": rec.get(
                    "image",
                    "https://images.unsplash.com/photo-1481627834876-b7833e8f5570?w=800",
                ),
                "alt": rec.get("title", "Recommendation"),
            },
            {
                "type": "type_badge",
                "text": rec.get("type", "resource").upper(),
                "color": "#10b981",
            },
            {
                "type": "headline",
                "text": rec.get("title", "Recommended for You"),
                "style": "large_bold",
            },
            {
                "type": "body_text",
                "text": rec.get("description", ""),
            },
        ]

        if rec.get("price_hint"):
            components.append(
                {"type": "price_badge", "text": rec["price_hint"], "color": "#64748b"}
            )

        components.append(
            {
                "type": "action_button",
                "label": rec.get("label", "Learn More"),
                "action": {
                    "type": "affiliate_link",
                    "url": url,
                    "tracking_id": tracking_id,
                },
            }
        )
        components.append(
            {
                "type": "action_button",
                "label": "Not for me",
                "style": "ghost",
                "action": {"type": "next_screen", "context": "skip_recommendation"},
            }
        )

        return {
            "type": "recommendation_card",
            "layout": "card_stack",
            "components": components,
            "feedback_overlay": {
                "type": "star_rating",
                "position": "bottom_right",
                "always_visible": True,
            },
            "metadata": {
                "agent": self.AGENT_NAME,
                "variant": variant,
                "rec_type": rec.get("type", "resource"),
                "category": rec.get("category", ""),
                "domain": rec.get("domain", "iVive"),
                "tracking_id": tracking_id,
                "is_mock": is_mock,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        }
