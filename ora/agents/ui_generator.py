"""
UIGeneratorAgent
Free-form screen generator — Ora's wild card.
Generates novel screen layouts that don't fit other agent types,
like onboarding, goal-setting wizards, celebrations, and summaries.
"""

import logging
import json
import random
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone

from core.config import settings
from ora.content_quality import content_quality_check

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Design Principles — Ora's visual design memory
# ---------------------------------------------------------------------------

DESIGN_PRINCIPLES = {
    "tiktok": [
        "Full bleed — content fills the entire screen, zero chrome",
        "Single action — one thing to do, make it obvious",
        "Momentum — the next thing is already loading",
        "Sound off — visuals must work without audio",
    ],
    "tinder": [
        "Decisive interaction — one gesture, clear binary",
        "Instant feedback — response is immediate and satisfying",
        "Stack depth — sense that there's always more",
        "Show don't tell — image > text",
    ],
    "minimal": [
        "Restraint — every element earns its place",
        "Typography-first — words as design",
        "Breathing room — whitespace is content",
        "Calm — no urgency, no pressure",
    ],
    "social_proof": [
        "Community signal — show what others are doing",
        "FOMO is a tool, use sparingly",
        "Numbers create credibility",
        "Relatability — real people doing real things",
    ],
    "enlightenment": [
        "Spaciousness — give the thought room to land",
        "No rush — don't ask for anything immediately",
        "Beauty — the container matters as much as the content",
        "Productive discomfort — depth over comfort",
    ],
}

# Domain-specific UI generation hints
DOMAIN_UI_HINTS = {
    "iVive": "Design for inner stillness and personal transformation. Soft, warm palette. Introspective tone. The user is on a journey inward.",
    "Eviva": "Design for purpose and contribution. Strong, purposeful visual weight. Tone of meaningful work. The user is making a difference.",
    "Aventi": "Design for joy and aliveness. Vibrant, energetic. Playful typography. The user is in the world, experiencing it.",
}


# CTA verb pool — randomly sampled per generation
_CTA_VERBS = [
    "Explore", "Try this", "Start now", "Reflect", "Schedule it",
    "Connect", "Begin", "Discover", "Do it", "Go deeper",
]


class UIGeneratorAgent:
    """
    Generates custom screens: onboarding, celebrations, summaries,
    weekly reviews, and any screen type Ora deems relevant.
    """

    AGENT_NAME = "UIGeneratorAgent"

    def __init__(self, openai_client=None):
        self.openai = openai_client

    async def generate_screen(
        self,
        user_context: Dict[str, Any],
        screen_type: str = "summary",
        variant: str = "A",
    ) -> Dict[str, Any]:
        if self.openai and settings.has_openai:
            result = await self._generate_with_ai(user_context, screen_type, variant)
        else:
            result = self._generate_mock(user_context, screen_type, variant)

        # Quality filter: reject platitudes, retry up to 3 times
        for _ in range(3):
            if content_quality_check(result):
                return result
            if self.openai and settings.has_openai:
                result = await self._generate_with_ai(user_context, screen_type, variant)
            else:
                result = self._generate_mock(user_context, screen_type, variant)
        return result

    def _get_recent_screen_themes(self, user_context: Dict[str, Any]) -> List[str]:
        """Return last 5 agent_type values from recent_interactions."""
        recent = user_context.get("recent_interactions", [])
        return [
            str(i.get("agent_type", ""))
            for i in recent[-5:]
            if i.get("agent_type")
        ]

    async def _generate_with_ai(
        self, user_context: Dict[str, Any], screen_type: str, variant: str
    ) -> Dict[str, Any]:
        goals = user_context.get("active_goals", [])
        fulfilment = user_context.get("fulfilment_score", 0.0)
        recent_ratings = user_context.get("recent_ratings", [])
        display_name = user_context.get("display_name", "")
        domain = user_context.get("domain", "iVive")
        improvement_hint = user_context.get("improvement_hint", "")

        domain_hint = DOMAIN_UI_HINTS.get(domain, DOMAIN_UI_HINTS["iVive"])
        # Include layout principles for the default (minimal) layout
        principles = DESIGN_PRINCIPLES.get("minimal", [])
        principles_text = "\n".join(f"  - {p}" for p in principles)

        # Recent themes to avoid
        recent_themes = self._get_recent_screen_themes(user_context)
        avoid_themes_line = (
            f"Do NOT repeat these recently shown themes: {recent_themes}"
            if recent_themes else ""
        )

        # CTA verb options
        cta_verbs = random.sample(_CTA_VERBS, min(3, len(_CTA_VERBS)))
        cta_verbs_line = f"Choose one of these CTA verbs: {cta_verbs}"

        city = user_context.get("user_city", "")
        country = user_context.get("user_country", "")
        time_of_day = user_context.get("time_of_day", "")
        geo_line = ""
        if city or country:
            geo_line = f"\nLocation: {city}{', ' + country if country else ''}"
        if time_of_day:
            geo_line += f" ({time_of_day})"

        prompt = f"""You are Ora, an AI generating a {screen_type} screen.
User: {display_name or "anonymous"}
Fulfilment score: {fulfilment:.2f}
Active goals: {[g["title"] for g in goals]}
Recent ratings: {recent_ratings}
Domain: {domain} — {domain_hint}{chr(10) + 'Improvement hint: ' + improvement_hint if improvement_hint else ""}{geo_line}
{chr(10) + avoid_themes_line if avoid_themes_line else ""}
{cta_verbs_line}

Design principles to follow:
{principles_text}

Generate a {screen_type} screen as JSON with:
- headline: compelling title
- subheadline: optional subtitle
- body: main content text
- components: array of component objects with type, text, etc.
- primary_cta: {{label, action_type, context}}
- screen_type: "{screen_type}"
- domain: "{domain}"

Components can be: headline, subheadline, body_text, stat_card,
progress_summary, goal_list, celebration_animation, quote, divider,
spacer, action_button, checklist.

For app-control actions (calendar, tasks, focus), use ora_action_button:
  {{"type": "ora_action_button", "label": "Book focus block", "icon": "📅",
    "tool": "calendar.schedule", "args": {{"title": "Focus block", "duration_minutes": 30}}}}
Available tools: calendar.schedule, tasks.add, focus.start, music.play, open_url

Return ONLY valid JSON, no markdown."""

        try:
            response = await self.openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.75,
                max_tokens=600,
                response_format={"type": "json_object"},
            )
            data = json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.warning(f"UIGeneratorAgent AI failed: {e}")
            return self._generate_mock(user_context, screen_type, variant)

        return self._build_spec(data, screen_type, variant)

    def _generate_mock(
        self,
        user_context: Dict[str, Any],
        screen_type: str,
        variant: str,
    ) -> Dict[str, Any]:
        fulfilment = user_context.get("fulfilment_score", 0.0)
        goals = user_context.get("active_goals", [])
        name = user_context.get("display_name", "")

        MOCK_SCREENS = {
            "onboarding": {
                "headline": f"Welcome{' ' + name if name else ''}. Let's begin.",
                "body": "Connectome learns what fuels you and surfaces it daily. The more you engage, the smarter it gets. Let's start by understanding what matters to you.",
                "primary_cta": {"label": "Set my first goal", "action_type": "navigate", "context": "goal_setup"},
            },
            "summary": {
                "headline": "Your Week in Review",
                "body": f"Fulfilment score: {fulfilment:.0%}. You've been showing up. Here's what Ora noticed.",
                "primary_cta": {"label": "Next week's intention", "action_type": "next_screen", "context": "intention_set"},
            },
            "celebration": {
                "headline": "🎉 Goal Achieved!",
                "body": f"You completed a goal. That's not nothing — that's everything. Ora noticed you showed up consistently. What's next?",
                "primary_cta": {"label": "Set a new goal", "action_type": "navigate", "context": "goal_setup"},
            },
            "goal_setup": {
                "headline": "What do you want to build?",
                "body": "Great goals are specific, meaningful, and time-bound. Don't write what you think you should want — write what you actually want.",
                "primary_cta": {"label": "Create my goal", "action_type": "navigate", "context": "goals"},
            },
        }

        data = MOCK_SCREENS.get(screen_type, MOCK_SCREENS["summary"])

        # Add active goals to summary screen
        extra_components = []
        if screen_type == "summary" and goals:
            for g in goals[:3]:
                extra_components.append({
                    "type": "progress_bar",
                    "label": g["title"],
                    "value": g.get("progress", 0.0),
                    "color": "#6366f1",
                })

        return self._build_spec(data, screen_type, variant, extra_components, is_mock=True)

    async def generate_tournament(
        self,
        screen_type: str,
        domain: str,
        n_variants: int = 3,
        context: Dict[str, Any] = None,
    ) -> list:
        """
        Generate N screen variants for the same content type, each using
        a different layout philosophy. Used by tournament mode.
        Returns a list of screen specs, each tagged with layout_style.
        """
        context = context or {}
        layout_styles = ["tiktok", "tinder", "minimal", "social_proof", "deep"]
        selected_styles = layout_styles[:n_variants]

        variants = []
        for style in selected_styles:
            try:
                spec = await self._generate_tournament_variant(
                    screen_type=screen_type,
                    domain=domain,
                    layout_style=style,
                    user_context=context,
                )
                spec["layout_style"] = style
                variants.append(spec)
            except Exception as e:
                logger.warning(f"Tournament variant {style} failed: {e}")
                # Generate a minimal fallback variant
                fallback = self._generate_mock(context, screen_type, "A")
                fallback["layout_style"] = style
                variants.append(fallback)

        return variants

    async def _generate_tournament_variant(
        self,
        screen_type: str,
        domain: str,
        layout_style: str,
        user_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Generate one tournament variant with a specific layout style."""
        if not (self.openai and settings.has_openai):
            spec = self._generate_mock(user_context, screen_type, layout_style)
            spec["layout_style"] = layout_style
            return spec

        principles = DESIGN_PRINCIPLES.get(layout_style, DESIGN_PRINCIPLES["minimal"])
        principles_text = "\n".join(f"  - {p}" for p in principles)
        domain_hint = DOMAIN_UI_HINTS.get(domain, DOMAIN_UI_HINTS["iVive"])

        goals = user_context.get("active_goals", [])
        display_name = user_context.get("display_name", "")

        # Recent themes and CTA verbs
        recent_themes = self._get_recent_screen_themes(user_context)
        avoid_themes_line = (
            f"Do NOT repeat these recently shown themes: {recent_themes}"
            if recent_themes else ""
        )
        cta_verbs = random.sample(_CTA_VERBS, min(3, len(_CTA_VERBS)))
        cta_verbs_line = f"Choose one of these CTA verbs: {cta_verbs}"

        prompt = f"""You are Ora, generating a {layout_style.upper()} style {screen_type} screen.
This is a tournament variant — it will be A/B tested against other layouts.
Domain: {domain} — {domain_hint}
User: {display_name or 'anonymous'}
Active goals: {[g['title'] for g in goals] or 'none yet'}
{avoid_themes_line}
{cta_verbs_line}

Layout style: {layout_style}
Design principles:
{principles_text}

Generate a {screen_type} screen as JSON:
- headline: compelling title for this layout style
- body: main content (length appropriate for {layout_style}: short for tiktok/tinder, longer for deep)
- primary_cta: {{label, action_type, context}}
- layout_style: "{layout_style}"
- domain: "{domain}"
- screen_type: "{screen_type}"

For tiktok: include hero_fullbleed component with gradient
For tinder: include swipe_card component
For social_proof: include social_proof_bar component
For deep: include reflection_prompt component

Where appropriate, include ora_action_button for real-world actions:
  {{"type": "ora_action_button", "label": "Schedule it", "icon": "📅",
    "tool": "calendar.schedule", "args": {{"title": "Goal work", "duration_minutes": 30}}}}
Tools: calendar.schedule, tasks.add, focus.start, music.play, open_url

Return ONLY valid JSON, no markdown."""

        try:
            response = await self.openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.85,
                max_tokens=600,
                response_format={"type": "json_object"},
            )
            data = json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.warning(f"Tournament variant AI failed ({layout_style}): {e}")
            spec = self._generate_mock(user_context, screen_type, layout_style)
            spec["layout_style"] = layout_style
            return spec

        spec = self._build_spec(data, screen_type, layout_style)
        spec["layout_style"] = layout_style
        return spec

    def _build_spec(
        self,
        data: Dict[str, Any],
        screen_type: str,
        variant: str,
        extra_components: list = None,
        is_mock: bool = False,
    ) -> Dict[str, Any]:
        components = [
            {
                "type": "headline",
                "text": data.get("headline", ""),
                "style": "large_bold",
            },
        ]

        if data.get("subheadline"):
            components.append(
                {"type": "body_text", "text": data["subheadline"], "style": "subtitle"}
            )

        if data.get("body"):
            components.append({"type": "body_text", "text": data["body"]})

        # Merge AI-generated components
        for comp in data.get("components", []):
            if isinstance(comp, dict) and comp.get("type"):
                components.append(comp)

        # Extra components (e.g. progress bars)
        for comp in (extra_components or []):
            components.append(comp)

        # Primary CTA
        cta = data.get("primary_cta", {})
        components.append(
            {
                "type": "action_button",
                "label": cta.get("label", "Continue"),
                "action": {
                    "type": cta.get("action_type", "next_screen"),
                    "context": cta.get("context", ""),
                },
            }
        )

        return {
            "type": screen_type,
            "layout": "scroll",
            "components": components,
            "feedback_overlay": {
                "type": "star_rating",
                "position": "bottom_right",
                "always_visible": True,
            },
            "metadata": {
                "agent": self.AGENT_NAME,
                "screen_type": screen_type,
                "variant": variant,
                "is_mock": is_mock,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        }
