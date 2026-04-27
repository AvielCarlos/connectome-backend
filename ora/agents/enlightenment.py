"""
EnlightenmentAgent
Generates perspective-shifting, philosophical, and depth content screens.

Helps users see their life and the world differently through:
philosophical prompts, meaningful questions, historical wisdom,
scientific wonder, cross-cultural insights, moments of awe,
and difficult truths delivered with care.

IMPORTANT: Content must NEVER be purely negative. Any discomfort must
serve growth or understanding. Always end with an opening, not a closing.
"""

import logging
import json
import random
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone

from core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Content categories
# ---------------------------------------------------------------------------

CONTENT_CATEGORIES = [
    "philosophical_prompt",
    "wisdom_quote",
    "scientific_wonder",
    "historical_mirror",
    "perspective_flip",
    "meaningful_question",
    "productive_discomfort",
    "awe_moment",
]

# Domain-specific content direction
DOMAIN_ENLIGHTENMENT_HINTS = {
    "iVive": (
        "Focus on inner world, self-knowledge, personal truth, and the journey of becoming. "
        "Questions that open up the user's relationship with themselves."
    ),
    "Eviva": (
        "Focus on collective wisdom, moral philosophy, what it means to contribute, "
        "and how individuals shape history. Questions about our responsibility to others."
    ),
    "Aventi": (
        "Focus on beauty, awe, wonder, and the joy of existence. "
        "Questions that open up appreciation for being alive in this moment."
    ),
}

# Mock enlightenment content by category
MOCK_CONTENT = {
    "philosophical_prompt": [
        {
            "title": "What Would You Do If You Knew You Couldn't Fail?",
            "body": "Most of our hesitation isn't about capability — it's about fear of judgment. Strip away that fear for a moment. What's left?",
            "category": "philosophical_prompt",
            "action_options": ["Write about this", "Sit with this"],
        },
        {
            "title": "Who Are You When No One Is Watching?",
            "body": "Character isn't what you perform. It's what you do when there's no audience, no credit, no consequence. What does that reveal about you?",
            "category": "philosophical_prompt",
            "action_options": ["Sit with this", "Write about this"],
        },
    ],
    "wisdom_quote": [
        {
            "title": "Marcus Aurelius on Time",
            "quote": "You have power over your mind — not outside events. Realize this, and you will find strength.",
            "attribution": "Marcus Aurelius, Meditations",
            "context": "Written by a Roman emperor during a plague, managing an empire, and personal grief. 2,000 years later, it still lands.",
            "body": "What event are you giving too much power over your mind right now?",
            "category": "wisdom_quote",
            "action_options": ["Sit with this", "Write about this"],
        },
        {
            "title": "Rumi on Wounds",
            "quote": "The wound is the place where the Light enters you.",
            "attribution": "Rumi, 13th century Sufi poet",
            "context": "Rumi lost his greatest teacher and friend. From that grief came some of the most transformative poetry humanity has produced.",
            "body": "What wound in your life might actually be an opening?",
            "category": "wisdom_quote",
            "action_options": ["Write about this", "Share this"],
        },
    ],
    "scientific_wonder": [
        {
            "stat": "60%",
            "context": "of your DNA is shared with a banana",
            "implication": "You are more connected to all life on Earth than you might think. The boundary between 'you' and 'nature' is mostly a story.",
            "body": "What does it mean to be a unique individual if we share this much with a banana?",
            "category": "scientific_wonder",
            "action_options": ["Sit with this", "Share this"],
        },
        {
            "stat": "13.8 billion",
            "context": "years — the age of the universe",
            "implication": "Your entire life, the full span of human history, everything you've ever worried about — it all fits inside a blip. And yet, here you are, awake and aware.",
            "body": "In the vastness of time, what feels most worth caring about right now?",
            "category": "scientific_wonder",
            "action_options": ["Sit with this", "Write about this"],
        },
    ],
    "historical_mirror": [
        {
            "title": "The Printing Press Panic of 1439",
            "body": "When Gutenberg invented the printing press, scholars panicked that information would spread too fast and people would lose their ability to think critically. Sound familiar? Every era believes it's uniquely challenged. Every era adapts.",
            "lesson": "What 'crisis' in your life might future generations look back on as the moment everything got better?",
            "category": "historical_mirror",
            "action_options": ["Sit with this", "Write about this"],
        },
    ],
    "perspective_flip": [
        {
            "title": "Your Obstacles Are Someone's Dream",
            "body": "The problem you're stuck on — the difficult conversation, the hard decision, the uncomfortable growth — someone somewhere is praying for the chance to even face that kind of challenge. You are already living someone's dream life.",
            "category": "perspective_flip",
            "action_options": ["Sit with this", "Write about this"],
        },
    ],
    "meaningful_question": [
        {
            "title": "Who Haven't You Thanked?",
            "body": "Think of someone whose influence shaped you in ways you may never have fully acknowledged. A teacher. A parent. A stranger. That call or note you've been meaning to send — what's actually stopping you?",
            "category": "meaningful_question",
            "action_options": ["Write about this", "Sit with this"],
        },
        {
            "title": "What Would Your 80-Year-Old Self Say?",
            "body": "From the vantage point of a full life — most of the worrying done, most of the important decisions already made — what would your future self want you to know right now?",
            "category": "meaningful_question",
            "action_options": ["Write about this", "Sit with this"],
        },
    ],
    "productive_discomfort": [
        {
            "title": "The Conversation You're Avoiding",
            "body": "There's probably a conversation you know you need to have. You've been circling it. The discomfort of having it is real — but so is the cost of not having it. Avoidance isn't free.",
            "note": "This is meant with care, not judgment. What would it take to take one step toward it?",
            "category": "productive_discomfort",
            "action_options": ["Write about this", "Sit with this"],
        },
    ],
    "awe_moment": [
        {
            "title": "Every Atom in Your Body Was Forged in a Star",
            "body": "The carbon in your cells, the iron in your blood — they were created in the nuclear furnaces of stars that exploded billions of years ago. You are, literally, made of stardust. The universe became aware of itself through you.",
            "category": "awe_moment",
            "action_options": ["Sit with this", "Share this"],
        },
        {
            "title": "Right Now, Somewhere, Someone Is Having the Best Day of Their Life",
            "body": "A baby is being born. Someone just got the news they've been waiting for. Two people are falling in love for the first time. The world is simultaneously experiencing its greatest joys right now, alongside its greatest sorrows. You are part of this.",
            "category": "awe_moment",
            "action_options": ["Sit with this", "Share this"],
        },
    ],
}


class EnlightenmentAgent:
    """
    Generates screens that shift perspective, challenge assumptions,
    and help users see their life and the world differently.

    Content includes: philosophical prompts, meaningful questions,
    historical wisdom, scientific wonder, cross-cultural insights,
    moments of awe, difficult truths delivered with care.

    Emotional range: includes productive discomfort (grief, loss, facing hard things)
    but never empty negativity. Discomfort must serve growth or understanding.
    """

    AGENT_NAME = "EnlightenmentAgent"

    def __init__(self, openai_client=None):
        self.openai = openai_client
        # Track recently used categories to avoid repetition
        self._recent_categories: List[str] = []

    def _pick_category(self) -> str:
        """Pick a content category, avoiding recent repeats."""
        available = [c for c in CONTENT_CATEGORIES if c not in self._recent_categories[-3:]]
        if not available:
            available = CONTENT_CATEGORIES
        category = random.choice(available)
        self._recent_categories.append(category)
        if len(self._recent_categories) > 8:
            self._recent_categories = self._recent_categories[-8:]
        return category

    async def generate_screen(
        self,
        user_context: Dict[str, Any],
        variant: str = "A",
    ) -> Dict[str, Any]:
        """Generate an enlightenment screen spec."""
        if self.openai and settings.has_openai:
            return await self._generate_with_ai(user_context, variant)
        return self._generate_mock(user_context, variant)

    async def _generate_with_ai(
        self, user_context: Dict[str, Any], variant: str
    ) -> Dict[str, Any]:
        domain = user_context.get("domain", "iVive")
        category = self._pick_category()
        domain_hint = DOMAIN_ENLIGHTENMENT_HINTS.get(domain, DOMAIN_ENLIGHTENMENT_HINTS["iVive"])
        display_name = user_context.get("display_name", "")
        goals = user_context.get("active_goals", [])

        prompt = f"""You are Ora, generating an enlightenment screen — content designed to shift perspective, open up thinking, and help a user see their life and the world differently.

User: {display_name or 'anonymous'}
Active goals: {[g["title"] for g in goals] or "none"}
Domain: {domain} — {domain_hint}
Content category: {category}

CRITICAL RULES:
- NEVER generate purely negative content. Discomfort must serve growth or understanding.
- Always end with an opening, not a closing — a question, a possibility, a door.
- Be spacious. Don't rush. Don't ask for immediate action.

Generate a JSON enlightenment screen for category "{category}":

If category is "philosophical_prompt":
  {{
    "title": "provocative question as title",
    "body": "2-3 sentences of insight + a question to sit with",
    "category": "philosophical_prompt"
  }}

If category is "wisdom_quote":
  {{
    "title": "short title",
    "quote": "the actual quote",
    "attribution": "Author, Work",
    "context": "1-2 sentences of modern context",
    "body": "a follow-up question or reflection",
    "category": "wisdom_quote"
  }}

If category is "scientific_wonder":
  {{
    "stat": "the striking number or fact",
    "context": "brief explanation of the fact",
    "implication": "what this means for how we see ourselves or the world",
    "body": "a question this opens up",
    "category": "scientific_wonder"
  }}

If category is "historical_mirror":
  {{
    "title": "historical event name",
    "body": "2-3 sentences about what happened and why it mirrors now",
    "lesson": "what we can learn or question from this",
    "category": "historical_mirror"
  }}

If category is "perspective_flip" | "meaningful_question" | "productive_discomfort" | "awe_moment":
  {{
    "title": "compelling title",
    "body": "2-3 sentences of the core content",
    "category": "{category}"
  }}

Always include:
- "action_options": array of 1-2 options from ["Sit with this", "Write about this", "Share this"]
- "domain": "{domain}"

Return ONLY valid JSON, no markdown."""

        try:
            response = await self.openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.85,
                max_tokens=500,
                response_format={"type": "json_object"},
            )
            data = json.loads(response.choices[0].message.content)
            data["category"] = category
        except Exception as e:
            logger.warning(f"EnlightenmentAgent AI failed: {e}")
            return self._generate_mock(user_context, variant)

        return self._build_spec(data, variant, domain)

    def _generate_mock(
        self, user_context: Dict[str, Any], variant: str
    ) -> Dict[str, Any]:
        """Pick a mock enlightenment card."""
        import hashlib
        uid = user_context.get("user_id", "anon")
        domain = user_context.get("domain", "iVive")
        category = self._pick_category()
        ts = str(int(datetime.now(timezone.utc).timestamp() / 3600))

        mock_pool = MOCK_CONTENT.get(category, MOCK_CONTENT["philosophical_prompt"])
        idx = int(hashlib.md5(f"{uid}{ts}{category}".encode()).hexdigest(), 16) % len(mock_pool)
        data = dict(mock_pool[idx])
        data["category"] = category

        return self._build_spec(data, variant, domain, is_mock=True)

    def _build_spec(
        self,
        data: Dict[str, Any],
        variant: str,
        domain: str,
        is_mock: bool = False,
    ) -> Dict[str, Any]:
        """Build the screen spec from content data."""
        category = data.get("category", "philosophical_prompt")
        action_options = data.get("action_options", ["Sit with this"])
        components = []

        # Domain badge at top
        components.append({"type": "domain_badge", "domain": domain})

        # Category-specific layout
        if category == "scientific_wonder":
            components.append({
                "type": "awe_stat",
                "stat": data.get("stat", ""),
                "context": data.get("context", ""),
                "implication": data.get("implication", ""),
            })
            if data.get("body"):
                components.append({
                    "type": "reflection_prompt",
                    "question": data["body"],
                    "allow_text_response": "Write about this" in action_options,
                    "response_placeholder": "What does this open up for you...",
                })

        elif category == "wisdom_quote":
            if data.get("title"):
                components.append({
                    "type": "headline",
                    "text": data["title"],
                    "style": "large_bold",
                })
            if data.get("quote"):
                components.append({"type": "quote", "text": data["quote"]})
            if data.get("attribution"):
                components.append({
                    "type": "body_text",
                    "text": f"— {data['attribution']}",
                    "style": "subtitle",
                })
            if data.get("context"):
                components.append({"type": "body_text", "text": data["context"]})
            if data.get("body"):
                components.append({
                    "type": "reflection_prompt",
                    "question": data["body"],
                    "allow_text_response": "Write about this" in action_options,
                    "response_placeholder": "Write freely...",
                })

        elif category == "historical_mirror":
            components.append({
                "type": "headline",
                "text": data.get("title", "A Mirror from History"),
                "style": "large_bold",
            })
            if data.get("body"):
                components.append({"type": "body_text", "text": data["body"]})
            if data.get("lesson"):
                components.append({
                    "type": "reflection_prompt",
                    "question": data["lesson"],
                    "allow_text_response": "Write about this" in action_options,
                    "response_placeholder": "What does this teach you...",
                })

        else:
            # philosophical_prompt, perspective_flip, meaningful_question,
            # productive_discomfort, awe_moment
            if data.get("title"):
                components.append({
                    "type": "headline",
                    "text": data["title"],
                    "style": "large_bold",
                })
            if data.get("body"):
                components.append({"type": "body_text", "text": data["body"]})
            if data.get("note"):
                components.append({
                    "type": "body_text",
                    "text": data["note"],
                    "style": "subtitle",
                })
            # Add reflection prompt for meaningful questions / productive discomfort
            if category in ("meaningful_question", "productive_discomfort", "perspective_flip"):
                components.append({
                    "type": "reflection_prompt",
                    "question": "What comes up for you right now?",
                    "allow_text_response": "Write about this" in action_options,
                    "response_placeholder": "Write freely...",
                })

        # Spacer for breathing room
        components.append({"type": "spacer", "value": 24})

        # Action buttons — always use the action_options from content
        for action_label in action_options[:2]:
            if action_label == "Sit with this":
                components.append({
                    "type": "action_button",
                    "label": "Sit with this",
                    "style": "ghost",
                    "action": {"type": "next_screen", "context": "enlightenment_continue"},
                })
            elif action_label == "Write about this":
                components.append({
                    "type": "action_button",
                    "label": "Write about this",
                    "action": {"type": "next_screen", "context": "enlightenment_write"},
                })
            elif action_label == "Share this":
                components.append({
                    "type": "action_button",
                    "label": "Share this",
                    "style": "secondary",
                    "action": {"type": "next_screen", "context": "enlightenment_share"},
                })

        return {
            "type": "enlightenment_card",
            "layout": "scroll",
            "layout_style": "deep",
            "components": components,
            "feedback_overlay": {
                "type": "star_rating",
                "position": "bottom_right",
                "always_visible": True,
            },
            "metadata": {
                "agent": self.AGENT_NAME,
                "variant": variant,
                "category": category,
                "domain": domain,
                "is_mock": is_mock,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        }
