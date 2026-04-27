"""
DiscoveryInterviewAgent
Surfaces personal "get-to-know-you" questions to the user.
Answers are stored in the user profile to improve future recommendations.

Appears approximately:
  - 1 in 8 cards for new users (< 20 interactions)
  - 1 in 15 cards for established users
"""

import logging
import random
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Question bank — stable question_id keys, ~20 questions
# ---------------------------------------------------------------------------

QUESTION_BANK = [
    # VALUES
    {
        "question_id": "values_top_three",
        "question": "Which of these resonate most with you right now?",
        "input_type": "multiple_choice",
        "options": ["Deep relationships", "Creative work", "Financial freedom", "Health & vitality", "Meaning & purpose"],
        "profile_field": "core_values",
    },
    {
        "question_id": "values_non_negotiable",
        "question": "What's one thing you'd never compromise on?",
        "input_type": "written",
        "profile_field": "core_values_text",
    },
    # LIFESTYLE
    {
        "question_id": "lifestyle_morning",
        "question": "What does your ideal morning look like?",
        "input_type": "multiple_choice",
        "options": ["Early rise, quiet time", "Slow start, coffee first", "Exercise right away", "Depends on the day", "I'm not a morning person"],
        "profile_field": "lifestyle_morning",
    },
    {
        "question_id": "lifestyle_social",
        "question": "How do you recharge?",
        "input_type": "multiple_choice",
        "options": ["Solo time", "Small group of close friends", "Big social gatherings", "Nature / outdoors", "Creative work"],
        "profile_field": "lifestyle_social",
    },
    # ENERGY PATTERNS
    {
        "question_id": "energy_peak",
        "question": "When are you at your sharpest?",
        "input_type": "multiple_choice",
        "options": ["Early morning (5–9am)", "Late morning (9am–12pm)", "Afternoon (12–5pm)", "Evening (5–10pm)", "Late night (10pm+)"],
        "profile_field": "energy_peak_time",
    },
    {
        "question_id": "energy_drain",
        "question": "What drains you most?",
        "input_type": "multiple_choice",
        "options": ["Shallow small talk", "Too many decisions", "Waiting on others", "Repetitive tasks", "Conflict"],
        "profile_field": "energy_drains",
    },
    # GOALS
    {
        "question_id": "goals_horizon",
        "question": "What's your biggest focus over the next 90 days?",
        "input_type": "written",
        "profile_field": "goals_90_day",
    },
    {
        "question_id": "goals_obstacle",
        "question": "What's the main thing holding you back right now?",
        "input_type": "multiple_choice",
        "options": ["Not enough time", "Not enough clarity", "Fear of failure", "Lack of support", "I'm not sure"],
        "profile_field": "goals_obstacle",
    },
    # RELATIONSHIPS
    {
        "question_id": "relationships_depth",
        "question": "How would you describe your social life?",
        "input_type": "scale",
        "scale_min": 1,
        "scale_max": 5,
        "scale_labels": {"1": "Very isolated", "3": "Balanced", "5": "Very connected"},
        "profile_field": "social_connectedness",
    },
    {
        "question_id": "relationships_investment",
        "question": "How much do you invest in your relationships?",
        "input_type": "multiple_choice",
        "options": ["I reach out constantly", "I'm responsive but not proactive", "I tend to withdraw", "I'm selective and intentional", "Relationships are hard for me"],
        "profile_field": "relationship_style",
    },
    # LEARNING STYLE
    {
        "question_id": "learning_format",
        "question": "How do you learn best?",
        "input_type": "multiple_choice",
        "options": ["Reading long-form", "Short videos / reels", "Podcasts while moving", "Hands-on doing", "Discussion with others"],
        "profile_field": "learning_style",
    },
    {
        "question_id": "learning_depth",
        "question": "Do you prefer to go broad or deep?",
        "input_type": "scale",
        "scale_min": 1,
        "scale_max": 5,
        "scale_labels": {"1": "Wide generalist", "3": "Mixed", "5": "Deep specialist"},
        "profile_field": "learning_breadth",
    },
    # CREATIVITY
    {
        "question_id": "creativity_outlet",
        "question": "How do you express your creativity?",
        "input_type": "multiple_choice",
        "options": ["Writing", "Visual art / design", "Music", "Building things", "Cooking", "I don't think of myself as creative"],
        "profile_field": "creativity_outlet",
    },
    {
        "question_id": "creativity_blocked",
        "question": "When do you feel most stuck creatively?",
        "input_type": "written",
        "profile_field": "creativity_blocks",
    },
    # HEALTH
    {
        "question_id": "health_priority",
        "question": "Which health area matters most to you right now?",
        "input_type": "multiple_choice",
        "options": ["Sleep quality", "Physical fitness", "Mental health", "Nutrition", "Stress management"],
        "profile_field": "health_priority",
    },
    {
        "question_id": "health_sleep",
        "question": "How's your sleep been lately?",
        "input_type": "scale",
        "scale_min": 1,
        "scale_max": 5,
        "scale_labels": {"1": "Terrible", "3": "Okay", "5": "Great"},
        "profile_field": "sleep_quality",
    },
    # WELLBEING
    {
        "question_id": "wellbeing_fulfillment",
        "question": "How fulfilled do you feel in your daily life?",
        "input_type": "scale",
        "scale_min": 1,
        "scale_max": 5,
        "scale_labels": {"1": "Not at all", "3": "Somewhat", "5": "Very fulfilled"},
        "profile_field": "fulfilment_self_report",
    },
    {
        "question_id": "wellbeing_stress",
        "question": "What's your current stress level?",
        "input_type": "scale",
        "scale_min": 1,
        "scale_max": 5,
        "scale_labels": {"1": "Very low", "3": "Moderate", "5": "Very high"},
        "profile_field": "stress_level",
    },
    # PURPOSE
    {
        "question_id": "purpose_ikigai",
        "question": "What would you do if you knew you couldn't fail?",
        "input_type": "written",
        "profile_field": "ikigai_text",
    },
    {
        "question_id": "purpose_legacy",
        "question": "What do you want to be remembered for?",
        "input_type": "written",
        "profile_field": "legacy_text",
    },
]

# Fields considered "thin" — questions to ask first
PRIORITY_FIELDS = [
    "core_values",
    "lifestyle_morning",
    "energy_peak_time",
    "learning_style",
    "health_priority",
    "goals_90_day",
]


class DiscoveryInterviewAgent:
    """
    Generates personal discovery question cards to build the user profile.
    """

    AGENT_NAME = "DiscoveryInterviewAgent"

    def __init__(self, openai_client=None):
        self.openai = openai_client

    def _pick_question(self, user_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Choose which question to ask based on profile gaps.
        Priority: fields in PRIORITY_FIELDS that are missing first.
        Falls back to random selection.
        """
        profile = user_context.get("profile", {})
        shown_ids = set(user_context.get("shown_interview_ids", []))

        # Prioritize high-priority fields
        for q in QUESTION_BANK:
            if q["question_id"] in shown_ids:
                continue
            field = q.get("profile_field", "")
            if field in PRIORITY_FIELDS and field not in profile:
                return q

        # Any unseen question
        unseen = [q for q in QUESTION_BANK if q["question_id"] not in shown_ids]
        if unseen:
            return random.choice(unseen)

        # All seen — repeat from full bank
        return random.choice(QUESTION_BANK)

    async def generate_screen(
        self, user_context: Dict[str, Any], variant: str = "A"
    ) -> Dict[str, Any]:
        """
        Generate a discovery interview card.
        Returns a ScreenSpec-compatible dict.
        """
        question = self._pick_question(user_context)

        # Build the discovery_interview component
        interview_component: Dict[str, Any] = {
            "type": "discovery_interview",
            "question_id": question["question_id"],
            "question": question["question"],
            "input_type": question["input_type"],
            "profile_field": question["profile_field"],
        }

        if question["input_type"] == "multiple_choice":
            interview_component["options"] = question.get("options", [])
        elif question["input_type"] == "scale":
            interview_component["scale_min"] = question.get("scale_min", 1)
            interview_component["scale_max"] = question.get("scale_max", 5)
            interview_component["scale_labels"] = question.get("scale_labels", {})

        # Skip button — ghost action_button that fires next_screen
        skip_button = {
            "type": "action_button",
            "label": "Skip",
            "style": "ghost",
            "action": {"type": "next_screen", "context": "discovery_skip"},
        }

        return {
            "type": "discovery_interview",
            "layout": "scroll",
            "components": [interview_component, skip_button],
            "feedback_overlay": {
                "type": "none",
                "position": "none",
                "always_visible": False,
            },
            "metadata": {
                "agent": self.AGENT_NAME,
                "question_id": question["question_id"],
                "profile_field": question["profile_field"],
                "variant": variant,
            },
        }
