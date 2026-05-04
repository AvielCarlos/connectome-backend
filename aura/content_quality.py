"""
Content Quality Filter
Shared module for detecting and rejecting generic platitude content.
Used by all Aura agents to ensure screens contain real, specific value.
"""

import json
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

BANNED_PHRASES = [
    "discover joy",
    "find your aliveness",
    "unlock your potential",
    "live your best life",
    "embrace the journey",
    "be present",
    "your journey begins",
    "start your journey",
    "transform your life",
    "find your purpose",
    "discover yourself",
    "ignite your passion",
    "step into your greatness",
    "unleash your",
    "awaken your",
    "harness the power",
    "tap into your",
    "align with your",
]


def _extract_text_values(obj: Any) -> str:
    """
    Recursively extract all string values from a dict/list/str structure
    and concatenate them into a single lowercase string for phrase scanning.
    """
    if isinstance(obj, str):
        return obj.lower()
    if isinstance(obj, dict):
        return " ".join(_extract_text_values(v) for v in obj.values())
    if isinstance(obj, list):
        return " ".join(_extract_text_values(item) for item in obj)
    return ""


def content_quality_check(spec: Dict[str, Any]) -> bool:
    """
    Returns True if content passes quality check (no banned phrases).
    Returns False if content contains generic platitudes.

    Usage:
        if not content_quality_check(spec):
            # regenerate
    """
    full_text = _extract_text_values(spec)
    for phrase in BANNED_PHRASES:
        if phrase.lower() in full_text:
            logger.debug(f"content_quality_check: banned phrase found: '{phrase}'")
            return False
    return True
