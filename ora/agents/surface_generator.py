"""
Surface Generator — Ora picks the best mini-app template for a node.

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
            config["resource_links"] = []  # Ora or user can populate

        elif template_name == "social_invite":
            config["share_message"] = f"Join me: {title}"

        elif template_name == "finance_tracker":
            budget = node.get("requires_finances")
            if budget:
                config["budget_usd"] = float(budget)
            config["savings_label"] = f"Save for: {title}"

        return config
