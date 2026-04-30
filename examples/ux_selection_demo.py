"""Run a deterministic UXSelectionAgent demo from the command line.

Usage:
    python3 examples/ux_selection_demo.py

The example mirrors the IOO execution flow: a node/objective, lightweight user
context, SearchAgent-shaped candidates, and the ranked UXSelectionAgent output.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ora.agents.ux_selection_agent import UXSelectionInput, select_ux_options


EXAMPLE_INPUT = UXSelectionInput(
    objective="Choose the best first step for joining a local community initiative",
    node={
        "id": "demo-community-node",
        "title": "Join a local community initiative",
        "description": "Find a realistic, values-aligned way to contribute locally this week.",
        "type": "experience",
        "domain": "Eviva",
        "step_type": "hybrid",
        "tags": ["community", "service", "local"],
        "requires_location": "Vancouver, Canada",
        "requires_time_hours": 2,
        "difficulty_level": 3,
    },
    user_context={
        "city": "Vancouver",
        "country": "Canada",
        "known_skills": ["event planning", "writing"],
        "free_time_weekend_hours": 3,
        "finances_level": "modest",
    },
    candidate_actions=[
        {
            "id": "local-map-search",
            "title": "Local volunteer/community options near Vancouver",
            "candidate_type": "local_discovery",
            "confidence": 0.68,
            "rationale": "Nearby initiatives are likely to be actionable this week and easy to verify.",
            "source": {
                "name": "Google Maps search",
                "type": "maps_query",
                "url": "https://www.google.com/maps/search/?api=1&query=community+volunteer+Vancouver",
            },
            "next_action": {
                "label": "Open map results and shortlist 2-3 realistic options",
                "action_type": "open_link",
                "requires_confirmation": False,
            },
            "metadata": {"location_used": "Vancouver, Canada", "query": "community volunteer Vancouver"},
        },
        {
            "id": "online-prep-guide",
            "title": "Read a beginner guide to choosing meaningful volunteer work",
            "candidate_type": "learning_or_prep_path",
            "confidence": 0.57,
            "rationale": "A short prep path can reduce uncertainty before committing to a group.",
            "source": {"name": "Web search", "type": "web_query", "url": "https://www.google.com/search?q=how+to+choose+volunteer+work"},
            "next_action": {"label": "Read the guide and note one preference", "action_type": "open_link"},
        },
        {
            "id": "ask-clarifying-question",
            "title": "Clarify preferred cause area first",
            "candidate_type": "fallback_clarification",
            "confidence": 0.45,
            "rationale": "Cause-area preference is not explicit yet.",
            "next_action": {"label": "Ask which cause area feels most alive", "action_type": "review"},
        },
    ],
    intent="do_now",
)


if __name__ == "__main__":
    print(json.dumps(select_ux_options(EXAMPLE_INPUT), indent=2, sort_keys=True))
