"""
DiscoveryAgent
Generates discovery/exploration screens — new ideas, activities, or concepts
that align with the user's interests and fulfilment goals.
"""

import logging
import json
import random
from typing import Dict, Any, Optional
from datetime import datetime, timezone

from core.config import settings
from ora.content_quality import content_quality_check
from ora.data.activity_repository import get_activity_repository

logger = logging.getLogger(__name__)

# Similarity threshold for surfacing Drive notes as coaching context
DRIVE_SIMILARITY_THRESHOLD = 0.75
# Show from_your_notes cards when user has fewer than this many goals
FROM_NOTES_GOAL_THRESHOLD = 3

# Mock content pool for when OpenAI is unavailable
MOCK_DISCOVERY_CARDS = [
    {
        "title": "Mindful Morning Ritual",
        "body": "Starting your day with 10 minutes of intentional stillness can shift your entire trajectory. Not meditation — just presence. Coffee, window, breath.",
        "image": "https://images.unsplash.com/photo-1506905925346-21bda4d32df4?w=800",
        "cta": "Try it tomorrow",
        "category": "mindfulness",
    },
    {
        "title": "The 2-Minute Rule",
        "body": "If a task takes less than 2 minutes, do it now. This single habit eliminates 40% of mental clutter without any planning system.",
        "image": "https://images.unsplash.com/photo-1484480974693-6ca0a78fb36b?w=800",
        "cta": "Apply it today",
        "category": "productivity",
    },
    {
        "title": "Deep Work Blocks",
        "body": "Shallow work fills time. Deep work creates value. Block 90-minute windows where the phone doesn't exist and magic happens.",
        "image": "https://images.unsplash.com/photo-1517842645767-c639042777db?w=800",
        "cta": "Schedule a block",
        "category": "productivity",
    },
    {
        "title": "Connection Debt",
        "body": "Think of one person you've been meaning to reach out to. The message doesn't need to be perfect. 'Thinking of you' is enough.",
        "image": "https://images.unsplash.com/photo-1529156069898-49953e39b3ac?w=800",
        "cta": "Reach out now",
        "category": "relationships",
    },
    {
        "title": "Strength Inventory",
        "body": "List 3 things you're genuinely good at. Not humble-good. Actually good. Now ask: when did you last use them intentionally?",
        "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=800",
        "cta": "Start the list",
        "category": "self-awareness",
    },
    {
        "title": "Energy Audit",
        "body": "Track which activities drain you vs. charge you over one week. The patterns will surprise you. Design your life around the chargers.",
        "image": "https://images.unsplash.com/photo-1527484800873-b3adac1c7a3b?w=800",
        "cta": "Start tracking",
        "category": "wellness",
    },
]


class DiscoveryAgent:
    """
    Generates exploration/discovery screens to expose users to
    new ideas and opportunities for growth.
    """

    AGENT_NAME = "DiscoveryAgent"

    def __init__(self, openai_client=None):
        self.openai = openai_client
        self._drive_agent = None  # lazily resolved

    def _get_drive_agent(self):
        """Lazy-load DriveAgent from Ora brain to avoid circular imports."""
        if self._drive_agent is None:
            try:
                from ora.brain import get_brain
                brain = get_brain()
                self._drive_agent = getattr(brain, 'drive_agent', None)
            except Exception:
                pass
        return self._drive_agent

    async def generate_screen(
        self,
        user_context: Dict[str, Any],
        variant: str = "A",
    ) -> Dict[str, Any]:
        """
        Generate a discovery card screen spec.
        - If user has < 3 goals, may surface a 'from_your_notes' card from Drive.
        - 50% of the time, pulls from activity repository to ground content in reality.
        - Uses GPT-4o if available, else falls back to mock content.
        """
        # Drive notes card: surface when user has few goals (needs self-context)
        goal_count = len(user_context.get("active_goals", []))
        if goal_count < FROM_NOTES_GOAL_THRESHOLD and random.random() < 0.30:
            notes_card = await self._generate_from_drive_notes(user_context, variant)
            if notes_card:
                return notes_card
        # Part 4: 50% chance to use activity repository as source material
        use_repo = random.random() < 0.50
        if use_repo:
            result = await self._generate_from_activity_repo(user_context, variant)
            if result:
                # Quality check — regenerate if platitudes detected
                for _ in range(3):
                    if content_quality_check(result):
                        return result
                    result = await self._generate_from_activity_repo(user_context, variant)
                    if not result:
                        break

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

    async def _generate_from_drive_notes(
        self,
        user_context: Dict[str, Any],
        variant: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Surface a relevant Drive doc as a 'from_your_notes' discovery card.
        Triggered when the user has few goals — helps Ora understand them better.
        Only shows documents owned by the requesting user (owner_user_id isolation).
        """
        drive = self._get_drive_agent()
        if drive is None:
            return None

        # Privacy: only search docs owned by this user
        owner_user_id = user_context.get("user_id")
        if not owner_user_id:
            return None

        try:
            interests = user_context.get("interests", [])
            goals = user_context.get("active_goals", [])
            goal_titles = [g["title"] for g in goals if isinstance(g, dict)]
            query_parts = interests[:3] + goal_titles[:2]
            if not query_parts:
                query_parts = ["goals", "values", "reflection"]
            query = " ".join(query_parts)

            results = await drive.semantic_search(
                query=query,
                owner_user_id=owner_user_id,
                limit=3,
                min_similarity=DRIVE_SIMILARITY_THRESHOLD,
            )
            if not results:
                return None

            best = results[0]
            doc_name = best["name"]
            excerpt = best["excerpt"]

            if self.openai and settings.has_openai:
                prompt = f"""You are Ora, an AI dedicated to human fulfilment.
A user's own note was found relevant to their current context:

Document: {doc_name}
Excerpt: {excerpt}

Create a short coaching discovery card that references this personal note.
JSON fields:
- title: short, specific insight or question (max 8 words)
- body: 2 sentences referencing their note; start with "From your notes:"
- cta: short action label
- category: one of [self-awareness, reflection, growth, values]

Return ONLY valid JSON."""
                response = await self.openai.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7,
                    max_tokens=250,
                    response_format={"type": "json_object"},
                )
                card_data = json.loads(response.choices[0].message.content)
            else:
                card_data = {
                    "title": doc_name[:60],
                    "body": f"From your notes: {excerpt[:200]}",
                    "cta": "Reflect on this",
                    "category": "self-awareness",
                }

            card_data["drive_id"] = best["drive_id"]
            card_data["card_type"] = "from_your_notes"
            return self._build_notes_spec(card_data, variant)

        except Exception as e:
            logger.warning(f"DiscoveryAgent: from_drive_notes failed: {e}")
            return None

    def _build_notes_spec(
        self,
        card: Dict[str, Any],
        variant: str,
    ) -> Dict[str, Any]:
        """Build a screen spec for a from_your_notes card."""
        components = [
            {
                "type": "category_badge",
                "text": "FROM YOUR NOTES",
                "color": "#8b5cf6",
            },
            {
                "type": "headline",
                "text": card.get("title", "A note worth revisiting"),
                "style": "large_bold",
            },
            {
                "type": "body_text",
                "text": card.get("body", ""),
            },
            {
                "type": "action_button",
                "label": card.get("cta", "Reflect"),
                "action": {"type": "next_screen", "context": "notes_reflection"},
            },
        ]

        return {
            "type": "from_your_notes",
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
                "card_type": "from_your_notes",
                "category": card.get("category", "self-awareness"),
                "drive_id": card.get("drive_id"),
                "domain": "iVive",
                "is_mock": False,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    async def _generate_from_activity_repo(
        self,
        user_context: Dict[str, Any],
        variant: str,
    ) -> Optional[Dict[str, Any]]:
        """Pick activities from the repo and craft a screen around them."""
        try:
            domain = user_context.get("domain", "iVive")
            repo = get_activity_repository()
            activities = repo.get_activities_for_domain(domain, limit=3)
            if not activities:
                return None

            activity = activities[0]

            if self.openai and settings.has_openai:
                # Let LLM craft a compelling screen around the activity
                prompt = f"""You are Ora, an AI dedicated to human fulfilment.
Create a discovery card screen based on this real activity:

Activity: {activity['title']}
Description: {activity['description']}
Domain: {domain}
Difficulty: {activity['difficulty']}
Time: {activity['time_required']}
Cost: {activity['cost']}

Write a JSON discovery card with:
- title: a compelling title (can differ from the activity name, must be specific)
- body: 2-3 sentences expanding on why this specific activity matters and what makes it valuable
- cta: short call-to-action
- category: one relevant category word
- domain: "{domain}"

Be specific, concrete, and practical. No generic motivational language.
Return ONLY valid JSON."""
                response = await self.openai.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.75,
                    max_tokens=350,
                    response_format={"type": "json_object"},
                )
                card_data = json.loads(response.choices[0].message.content)
                card_data["domain"] = domain
                card_data["activity_id"] = activity.get("id", "")
                return self._build_spec(card_data, "", variant)
            else:
                # Use activity directly as mock content
                card_data = {
                    "title": activity["title"],
                    "body": activity["description"],
                    "cta": "Try this",
                    "category": activity["tags"][0] if activity.get("tags") else "activity",
                    "domain": domain,
                    "activity_id": activity.get("id", ""),
                }
                return self._build_spec(card_data, "", variant)
        except Exception as e:
            logger.warning(f"DiscoveryAgent: activity repo generation failed: {e}")
            return None

    async def _generate_with_ai(
        self, user_context: Dict[str, Any], variant: str
    ) -> Dict[str, Any]:
        interests = user_context.get("interests", [])
        goals = user_context.get("active_goals", [])
        fulfilment = user_context.get("fulfilment_score", 0.5)
        recent_ratings = user_context.get("recent_ratings", [])
        domain = user_context.get("domain", "iVive")

        # Domain-specific content direction
        DOMAIN_HINTS = {
            "iVive": "Suggest a personal growth practice, self-care ritual, or inner work the user could explore. Focus on inward becoming, health, identity, healing, and personal transformation.",
            "Eviva": "Suggest a way this person could contribute meaningfully to others or make an impact. Focus on community, legacy, purpose, meaningful work, or collective contribution.",
            "Aventi": "Suggest an experience, adventure, or enjoyable activity this person could do. Focus on joy, culture, play, spontaneity, and memorable moments.",
        }
        domain_hint = DOMAIN_HINTS.get(domain, DOMAIN_HINTS["iVive"])

        # Build a dynamic prompt based on user state
        city = user_context.get("user_city", "")
        country = user_context.get("user_country", "")
        time_of_day = user_context.get("time_of_day", "")
        geo_line = ""
        if city or country:
            geo_line = f"\n- Location: {city}{', ' + country if country else ''}"
        if time_of_day:
            geo_line += f" (it's {time_of_day} there)"

        # Optionally inject relevant Drive notes as personal grounding context.
        # PRIVACY: only inject notes owned by the requesting user.
        drive_context_line = ""
        owner_user_id = user_context.get("user_id")
        drive = self._get_drive_agent()
        if drive is not None and owner_user_id:
            try:
                query_terms = " ".join(
                    (interests or [])[:2]
                    + [g["title"] for g in goals if isinstance(g, dict)][:2]
                ) or "growth goals values"
                drive_hits = await drive.semantic_search(
                    query=query_terms,
                    owner_user_id=owner_user_id,
                    limit=2,
                    min_similarity=DRIVE_SIMILARITY_THRESHOLD,
                )
                if drive_hits:
                    excerpts = " | ".join(
                        f"{h['name']}: {h['excerpt'][:120]}" for h in drive_hits
                    )
                    drive_context_line = f"\n- Relevant personal notes: {excerpts}"
            except Exception as _de:
                logger.debug(f"DiscoveryAgent: drive context injection failed: {_de}")

        prompt = f"""You are Ora, an AI dedicated to human fulfilment.
Generate a discovery card for a user with:
- Interests: {interests or "not yet specified"}
- Active goals: {[g["title"] for g in goals] or "none set"}
- Fulfilment score: {fulfilment:.2f}/1.0
- Recent ratings: {recent_ratings or "no history yet"}
- Domain focus: {domain} — {domain_hint}{geo_line}{drive_context_line}

Create a JSON discovery card with:
- title: compelling, specific (5-8 words)
- body: 2-3 sentences of genuine insight or actionable wisdom
- image_query: descriptive image search phrase
- cta: short call-to-action label
- category: one of [mindfulness, productivity, relationships, wellness, creativity, learning, finance, health]
- affiliate_hint: optional product/service that naturally fits (or null)
- domain: "{domain}"

Return ONLY valid JSON, no markdown."""

        try:
            response = await self.openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.8,
                max_tokens=400,
                response_format={"type": "json_object"},
            )
            card_data = json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.warning(f"OpenAI call failed, using mock: {e}")
            return self._generate_mock(user_context, variant)

        image_url = f"https://images.unsplash.com/photo-1506905925346-21bda4d32df4?w=800"
        # In production you'd call Unsplash API with card_data.get("image_query")

        return self._build_spec(card_data, image_url, variant)

    def _generate_mock(
        self, user_context: Dict[str, Any], variant: str
    ) -> Dict[str, Any]:
        """Pick a mock card based on user context hash for consistency."""
        import hashlib
        uid = user_context.get("user_id", "anon")
        ts = str(int(datetime.now(timezone.utc).timestamp() / 3600))  # changes hourly
        idx = int(hashlib.md5(f"{uid}{ts}".encode()).hexdigest(), 16) % len(
            MOCK_DISCOVERY_CARDS
        )
        card_data = MOCK_DISCOVERY_CARDS[idx]
        return self._build_spec(
            card_data, card_data.get("image", ""), variant, is_mock=True
        )

    def _build_spec(
        self,
        card: Dict[str, Any],
        image_url: str,
        variant: str,
        is_mock: bool = False,
    ) -> Dict[str, Any]:
        import uuid

        affiliate_url = card.get("affiliate_url")
        tracking_id = str(uuid.uuid4()) if affiliate_url else None

        components = [
            {
                "type": "hero_image",
                "source": image_url or "https://images.unsplash.com/photo-1506905925346-21bda4d32df4?w=800",
                "alt": card.get("title", "Discovery"),
            },
            {
                "type": "category_badge",
                "text": card.get("category", "discovery").upper(),
                "color": "#6366f1",
            },
            {
                "type": "headline",
                "text": card.get("title", "Today's Discovery"),
                "style": "large_bold",
            },
            {
                "type": "body_text",
                "text": card.get("body", ""),
            },
            {
                "type": "action_button",
                "label": card.get("cta", "Explore"),
                "action": {"type": "next_screen", "context": "discovery_continue"},
            },
        ]

        # Add affiliate CTA if available
        if affiliate_url and tracking_id:
            components.append(
                {
                    "type": "action_button",
                    "label": card.get("affiliate_label", "Learn More"),
                    "action": {
                        "type": "affiliate_link",
                        "url": affiliate_url,
                        "tracking_id": tracking_id,
                    },
                    "style": "secondary",
                }
            )

        return {
            "type": "discovery_card",
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
                "category": card.get("category", "discovery"),
                "domain": card.get("domain", "iVive"),
                "is_mock": is_mock,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        }

