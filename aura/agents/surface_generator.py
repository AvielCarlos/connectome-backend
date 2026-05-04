"""
Surface Generator — Aura picks the best mini-app template for a node.

Usage:
    gen = SurfaceGenerator()
    spec = gen.generate_spec(node_dict)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Template catalogue
# ---------------------------------------------------------------------------

SURFACE_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "habit_tracker": {
        "components": [
            "title",
            "streak_counter",
            "check_in_button",
            "history_chart",
            "motivational_quote",
        ],
        "suitable_for": ["activity", "sub_goal"],
        "suitable_tags": ["fitness", "health", "habit", "daily", "practice"],
    },
    "booking_flow": {
        "components": [
            "title",
            "description",
            "date_picker",
            "location_map",
            "price_display",
            "book_button",
            "confirmation",
        ],
        "suitable_for": ["experience", "activity"],
        "suitable_tags": ["travel", "dining", "event", "booking", "venue"],
    },
    "challenge": {
        "components": [
            "title",
            "challenge_description",
            "timer",
            "progress_bar",
            "complete_button",
            "share_button",
        ],
        "suitable_for": ["activity", "experience"],
        "suitable_tags": ["challenge", "adventure", "social", "sport"],
    },
    "checklist": {
        "components": [
            "title",
            "step_list",
            "progress_bar",
            "complete_button",
        ],
        "suitable_for": ["sub_goal", "goal", "activity"],
        "suitable_tags": ["learning", "preparation", "planning"],
    },
    "info_card": {
        "components": [
            "title",
            "rich_body",
            "resource_links",
            "start_button",
        ],
        "suitable_for": ["activity", "experience", "sub_goal", "goal"],
        "suitable_tags": [],  # fallback for anything
    },
    "social_invite": {
        "components": [
            "title",
            "description",
            "friend_picker",
            "invite_button",
            "share_link",
        ],
        "suitable_for": ["experience", "activity"],
        "suitable_tags": ["social", "group", "community", "friend"],
    },
    "finance_tracker": {
        "components": [
            "title",
            "budget_input",
            "expense_list",
            "progress_bar",
            "savings_goal",
        ],
        "suitable_for": ["goal", "sub_goal"],
        "suitable_tags": ["finance", "money", "savings", "investment", "budget"],
    },
    "poll": {
        "components": ["title", "question", "option_buttons", "result_bars"],
        "suitable_for": ["activity", "experience", "sub_goal"],
        "suitable_tags": ["vote", "poll", "survey", "opinion", "feedback", "community"],
    },
    "countdown": {
        "components": ["title", "countdown_timer", "event_name", "action_button"],
        "suitable_for": ["experience", "goal", "activity"],
        "suitable_tags": ["event", "deadline", "launch", "timer", "countdown", "date"],
    },
    "leaderboard": {
        "components": ["title", "ranked_list", "score_display", "action_button"],
        "suitable_for": ["activity", "experience", "goal"],
        "suitable_tags": ["competition", "rank", "score", "leaderboard", "sport", "game", "challenge"],
    },
    "media_card": {
        "components": ["title", "hero_image", "body", "tags", "share_button", "cta_button"],
        "suitable_for": ["experience", "activity", "sub_goal"],
        "suitable_tags": ["media", "content", "article", "video", "photo", "read", "watch"],
    },
    "quiz": {
        "components": ["title", "question_list", "option_selector", "progress_bar", "score_reveal"],
        "suitable_for": ["activity", "sub_goal", "goal"],
        "suitable_tags": ["quiz", "learning", "test", "knowledge", "education", "study"],
    },
    "journal_prompt": {
        "components": ["title", "prompt_text", "textarea", "word_count", "submit_button"],
        "suitable_for": ["activity", "sub_goal", "goal"],
        "suitable_tags": ["journal", "reflect", "writing", "mindfulness", "gratitude", "growth"],
    },
    "location_map": {
        "components": ["title", "address_display", "distance_badge", "directions_link"],
        "suitable_for": ["experience", "activity"],
        "suitable_tags": ["location", "map", "place", "venue", "travel", "explore", "visit"],
    },
    "event_card": {
        "components": ["title", "date_badge", "time_badge", "venue_badge", "rsvp_button"],
        "suitable_for": ["experience", "activity"],
        "suitable_tags": ["event", "concert", "meetup", "conference", "party", "gathering", "rsvp"],
    },
    "product_card": {
        "components": ["title", "product_image", "price", "rating_stars", "buy_button"],
        "suitable_for": ["activity", "experience", "sub_goal"],
        "suitable_tags": ["product", "shop", "buy", "purchase", "gear", "equipment", "item"],
    },
    "conversation_starter": {
        "components": ["title", "prompt_display", "next_button", "share_button"],
        "suitable_for": ["activity", "experience", "sub_goal"],
        "suitable_tags": ["social", "conversation", "connect", "icebreaker", "question", "talk"],
    },
    "skill_tree": {
        "components": ["title", "node_list", "status_indicators", "progress_button"],
        "suitable_for": ["goal", "sub_goal", "activity"],
        "suitable_tags": ["skill", "learning", "progression", "level", "mastery", "career", "growth"],
    },
    "vision_board": {
        "components": ["title", "image_grid", "affirmation_text", "share_button"],
        "suitable_for": ["goal", "experience"],
        "suitable_tags": ["vision", "dream", "goal", "inspire", "manifesting", "aspiration", "board"],
    },
    "daily_ritual": {
        "components": ["title", "step_checklist", "progress_circle", "done_button"],
        "suitable_for": ["activity", "sub_goal"],
        "suitable_tags": ["ritual", "routine", "morning", "evening", "daily", "habit", "practice"],
    },
    "comparison": {
        "components": ["title", "option_a_card", "option_b_card", "vote_buttons", "result_display"],
        "suitable_for": ["activity", "experience", "sub_goal"],
        "suitable_tags": ["compare", "versus", "choice", "decision", "vote", "pick", "opinion"],
    },
    "celebration": {
        "components": ["title", "achievement_display", "xp_badge", "confetti", "share_button"],
        "suitable_for": ["activity", "experience", "sub_goal", "goal"],
        "suitable_tags": ["celebrate", "achievement", "milestone", "win", "reward", "complete", "success"],
    },
}

# Default configs per template
_TEMPLATE_CONFIGS: Dict[str, Dict[str, Any]] = {
    "habit_tracker": {
        "streak_goal": 30,
        "check_in_label": "Did you do this today?",
        "reminder_time": "08:00",
    },
    "booking_flow": {
        "book_button_label": "Book Now",
        "show_price": True,
    },
    "challenge": {
        "duration_days": 7,
        "challenge_label": "Accept Challenge",
        "share_enabled": True,
    },
    "checklist": {
        "steps": [
            "Research what you need",
            "Block time in your calendar",
            "Complete any preparation",
            "Take the first step",
            "Reflect and celebrate",
        ],
    },
    "info_card": {
        "start_button_label": "Let's go →",
        "show_resources": True,
    },
    "social_invite": {
        "max_friends": 5,
        "share_message": "Join me on this experience!",
    },
    "finance_tracker": {
        "currency": "USD",
        "show_expenses": True,
    },
    "poll": {
        "options": ["Yes", "No", "Maybe", "Not sure"],
    },
    "countdown": {
        "action_label": "Set reminder",
    },
    "leaderboard": {
        "action_label": "🏃 Improve my rank",
    },
    "media_card": {
        "cta_label": "▶ View content",
    },
    "quiz": {
        "questions": [],
    },
    "journal_prompt": {
        "min_words": 10,
    },
    "location_map": {
        "address": "",
    },
    "event_card": {
        "rsvp_label": "🎟️ RSVP now",
    },
    "product_card": {
        "currency": "$",
        "buy_label": "🛍️ Buy now",
    },
    "conversation_starter": {
        "prompts": [
            "What's one thing you've been avoiding that could change everything?",
            "If you could master one skill in 30 days, what would it be?",
            "What would you do today if you knew you couldn't fail?",
        ],
    },
    "skill_tree": {
        "nodes": [],
    },
    "vision_board": {
        "images": [],
        "affirmation": "Your vision is your reality in progress.",
    },
    "daily_ritual": {
        "ritual_name": "Daily Ritual",
        "steps": [
            {"label": "Breathe deeply for 1 minute", "duration": "1 min"},
            {"label": "Set your intention", "duration": "2 min"},
            {"label": "Move your body", "duration": "5 min"},
            {"label": "Review your goals", "duration": "3 min"},
            {"label": "Take one action", "duration": "10 min"},
        ],
    },
    "comparison": {
        "option_a": {"title": "Option A"},
        "option_b": {"title": "Option B"},
    },
    "celebration": {
        "emoji": "🏆",
        "xp_earned": 100,
    },
}

# ---------------------------------------------------------------------------
# SurfaceGenerator
# ---------------------------------------------------------------------------


class SurfaceGenerator:
    """Generate mini-app surface specs for IOO nodes."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_spec(
        self,
        node: Dict[str, Any],
        open_mechanism: str = "button",
    ) -> Dict[str, Any]:
        """
        Return a full surface spec dict for the given node.

        Args:
            node: Row dict from ioo_nodes (must contain at minimum: id, title, type).
            open_mechanism: How this surface is triggered (button/conversation/proactive).

        Returns:
            Spec dict suitable for insertion into ioo_surfaces.spec.
        """
        template_name = self.pick_template(node)
        template = SURFACE_TEMPLATES[template_name]
        config = dict(_TEMPLATE_CONFIGS.get(template_name, {}))

        # Enrich config with node-specific data
        config = self._enrich_config(template_name, config, node)

        spec: Dict[str, Any] = {
            "template": template_name,
            "title": node.get("title", "Untitled"),
            "description": node.get("description", ""),
            "components": list(template["components"]),
            "config": config,
            "node_id": str(node.get("id", "")),
            "open_mechanism": open_mechanism,
            "tags": node.get("tags", []) or [],
            "domain": node.get("domain", ""),
        }

        return spec

    def pick_template(self, node: Dict[str, Any]) -> str:
        """
        Score every template by type-match + tag overlap, return the best name.
        Falls back to 'info_card' when nothing scores.
        """
        node_type = (node.get("type") or "").lower()
        node_tags = {t.lower() for t in (node.get("tags") or [])}

        best_name = "info_card"
        best_score = -1

        for name, tmpl in SURFACE_TEMPLATES.items():
            score = 0

            # Type match: +3
            if node_type in tmpl["suitable_for"]:
                score += 3

            # Tag overlap: +1 per matching tag
            tmpl_tags = {t.lower() for t in tmpl["suitable_tags"]}
            score += len(node_tags & tmpl_tags)

            # info_card is a universal fallback — slight penalty so others win
            if name == "info_card":
                score -= 0.5

            if score > best_score:
                best_score = score
                best_name = name

        logger.debug(
            "pick_template: node=%s type=%s tags=%s → %s (score=%.1f)",
            node.get("id", "?"),
            node_type,
            node_tags,
            best_name,
            best_score,
        )
        return best_name

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _enrich_config(
        self,
        template_name: str,
        config: Dict[str, Any],
        node: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Add node-specific values to the template config."""
        title = node.get("title", "")
        tags = node.get("tags") or []

        if template_name == "habit_tracker":
            # Personalise the check-in label
            config["check_in_label"] = f"Did you practice '{title}' today?"

        elif template_name == "booking_flow":
            cost = node.get("requires_finances")
            if cost:
                config["estimated_cost_usd"] = float(cost)
            config["location_hint"] = node.get("location_hint", "")

        elif template_name == "challenge":
            config["challenge_title"] = f"7-Day {title} Challenge"
            config["challenge_description"] = (
                node.get("description") or f"Commit to '{title}' for 7 days straight."
            )

        elif template_name == "checklist":
            # Keep default steps; surface can add node-specific ones later
            config["node_title"] = title

        elif template_name == "info_card":
            config["body"] = node.get("description", "")
            config["resource_links"] = []  # Aura or user can populate

        elif template_name == "social_invite":
            config["share_message"] = f"Join me: {title}"

        elif template_name == "finance_tracker":
            budget = node.get("requires_finances")
            if budget:
                config["budget_usd"] = float(budget)
            config["savings_label"] = f"Save for: {title}"

        elif template_name == "poll":
            config["question"] = node.get("description") or f"What do you think about '{title}'?"

        elif template_name == "countdown":
            config["event_name"] = title

        elif template_name == "leaderboard":
            config["action_label"] = f"🏃 Compete in: {title}"

        elif template_name == "media_card":
            config["cta_label"] = "▶ View content"

        elif template_name == "quiz":
            config["title"] = f"{title} Quiz"

        elif template_name == "journal_prompt":
            config["prompt"] = (
                node.get("description") or f"Reflect on your progress with '{title}'. What did you learn today?"
            )

        elif template_name == "location_map":
            config["address"] = node.get("location_hint", "")

        elif template_name == "event_card":
            config["event_name"] = title
            if node.get("description"):
                config["description"] = node["description"]

        elif template_name == "product_card":
            cost = node.get("requires_finances")
            if cost:
                config["price"] = float(cost)

        elif template_name == "conversation_starter":
            config["prompts"] = [
                f"What does '{title}' mean to you?",
                f"What\'s the biggest obstacle to achieving '{title}'?",
                f"How will your life change once you accomplish '{title}'?",
            ]

        elif template_name == "skill_tree":
            config["title"] = f"{title} Skill Tree"

        elif template_name == "vision_board":
            config["affirmation"] = (
                node.get("description") or f"I am on my way to achieving: {title}"
            )

        elif template_name == "daily_ritual":
            config["ritual_name"] = title

        elif template_name == "comparison":
            config["option_a"] = {"title": "Option A", "description": ""}
            config["option_b"] = {"title": "Option B", "description": ""}

        elif template_name == "celebration":
            config["achievement"] = title
            config["message"] = (
                node.get("description") or f"You completed: {title}. Incredible work!"
            )

        return config
