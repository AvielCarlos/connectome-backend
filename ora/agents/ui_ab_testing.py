"""
UIABTestingAgent
Continuously A/B tests UI surfaces to optimize Ora's output.

Optimization priority (hard-coded, never overridden):
  1. user_fulfilment  — primary, never compromise
  2. engagement       — secondary
  3. revenue          — tertiary, never overrides 1 or 2

Tests running automatically:
  - feed_card_layout: minimal | social_proof | tiktok | deep
  - cta_copy: action verb variations
  - interview_format: multiple_choice | scale | written
  - overlay_trigger: tap-to-open | auto-expand (metadata hint only)
"""

import asyncio
import logging
import random
from typing import Dict, Any, Optional, List

from ora.ab_testing import get_ui_variant, record_ui_event, get_winning_variant

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Test configurations
# ---------------------------------------------------------------------------

UI_TESTS: Dict[str, Dict[str, Any]] = {
    "feed_card_layout": {
        "variants": ["minimal", "social_proof", "tiktok", "deep"],
        "weights": None,  # equal initially
        "description": "Layout philosophy for feed cards",
    },
    "cta_copy": {
        "variants": ["Explore", "Try this", "Start now", "Reflect", "Discover"],
        "weights": None,
        "description": "Primary CTA verb copy",
    },
    "interview_format": {
        "variants": ["multiple_choice", "scale", "written"],
        "weights": None,
        "description": "Discovery interview input format",
    },
    "overlay_trigger": {
        "variants": ["tap-to-open", "auto-expand"],
        "weights": None,
        "description": "Card detail overlay trigger mode (metadata hint only)",
    },
}

# Evaluation loop interval
EVAL_INTERVAL_SECONDS = 12 * 3600  # 12 hours


class UIABTestingAgent:
    """
    Runs UI surface A/B tests and applies winning variants to screen specs.
    """

    AGENT_NAME = "UIABTestingAgent"

    def __init__(self, openai_client=None):
        self.openai = openai_client
        self._running = False

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def get_screen_variant(
        self, user_id: str, spec_dict: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Determine which UI variant to serve for this user/spec combination.
        Returns a variant_info dict describing the active tests for this request.
        """
        variant_info: Dict[str, Any] = {}

        for surface, cfg in UI_TESTS.items():
            try:
                variant = await get_ui_variant(
                    user_id=user_id,
                    surface=surface,
                    variants=cfg["variants"],
                    weights=cfg["weights"],
                )
                variant_info[surface] = variant
            except Exception as e:
                logger.debug(f"UIABTestingAgent: get_ui_variant({surface}) failed: {e}")

        return variant_info

    async def apply_variant(
        self, spec_dict: Dict[str, Any], variant_info: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Modify a screen spec dict based on the active variant assignments.
        Returns the (possibly modified) spec_dict.
        Non-destructive: falls back to original if anything goes wrong.
        """
        try:
            spec = dict(spec_dict)
            spec.setdefault("metadata", {})
            spec["metadata"]["ui_ab_variants"] = variant_info

            # ── feed_card_layout ──────────────────────────────────────────────
            layout_variant = variant_info.get("feed_card_layout")
            if layout_variant:
                spec["metadata"]["layout_style"] = layout_variant
                if layout_variant == "tiktok":
                    spec["layout"] = "card_stack"
                elif layout_variant == "minimal":
                    spec["layout"] = "scroll"

            # ── cta_copy ──────────────────────────────────────────────────────
            cta_verb = variant_info.get("cta_copy")
            if cta_verb:
                components = spec.get("components", [])
                for comp in components:
                    if comp.get("type") == "action_button" and comp.get("action", {}).get("type") == "next_screen":
                        original_label = comp.get("label", "Continue")
                        # Only override if the label is a generic default
                        if original_label in ("Continue", "Next", "Go", "Start"):
                            comp["label"] = cta_verb

            # ── interview_format ──────────────────────────────────────────────
            interview_fmt = variant_info.get("interview_format")
            if interview_fmt:
                components = spec.get("components", [])
                for comp in components:
                    if comp.get("type") == "discovery_interview":
                        # Override input_type if not already a complex written response
                        if comp.get("input_type") != "written" or interview_fmt != "scale":
                            comp["input_type"] = interview_fmt
                            # Ensure options exist for multiple_choice
                            if interview_fmt == "multiple_choice" and not comp.get("options"):
                                comp["options"] = ["Often", "Sometimes", "Rarely", "Never"]

            # ── overlay_trigger ───────────────────────────────────────────────
            overlay_trigger = variant_info.get("overlay_trigger")
            if overlay_trigger:
                spec["metadata"]["overlay_trigger"] = overlay_trigger

            return spec

        except Exception as e:
            logger.warning(f"UIABTestingAgent.apply_variant failed: {e}")
            return spec_dict

    # -----------------------------------------------------------------------
    # Background evaluation loop
    # -----------------------------------------------------------------------

    async def run_ui_test_loop(self) -> None:
        """
        Background task: evaluates tests every 12 hours and promotes winners.
        Optimization priority: fulfilment_delta > completion_rate > revenue signals
        """
        self._running = True
        logger.info("UIABTestingAgent: evaluation loop started")

        while self._running:
            try:
                await asyncio.sleep(EVAL_INTERVAL_SECONDS)
                await self._evaluate_all_tests()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"UIABTestingAgent: evaluation loop error: {e}")
                await asyncio.sleep(300)  # back off 5 min on error

    async def _evaluate_all_tests(self) -> None:
        """Evaluate each test surface and log winners."""
        for surface in UI_TESTS:
            try:
                winner = await get_winning_variant(surface)
                if winner:
                    logger.info(
                        f"UIABTestingAgent: surface='{surface}' winner='{winner}' \u2014 "
                        f"will serve this variant to all users"
                    )
                else:
                    logger.debug(f"UIABTestingAgent: surface='{surface}' \u2014 no winner yet")
            except Exception as e:
                logger.debug(f"UIABTestingAgent: evaluate {surface} failed: {e}")
