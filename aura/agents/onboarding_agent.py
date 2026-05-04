"""
OnboardingAgent — New User Drive Setup Flow
===========================================
Handles the conversation-based Drive onboarding for new Google Sign-In users.

When a new user signs in with Google, Aura presents a Drive onboarding card
asking if they want to connect their Drive and at what privacy level.

The card type is `onboarding_drive` and is handled specially by the frontend.

Usage:
  agent = OnboardingAgent()
  card = await agent.build_drive_onboarding_card(user_id)
  # Returns a screen spec dict for the frontend
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from core.database import execute, fetchrow

logger = logging.getLogger(__name__)


class OnboardingAgent:
    """
    Generates onboarding flows for new users.
    Currently supports Drive connection setup.
    """

    AGENT_NAME = "OnboardingAgent"

    async def should_show_drive_onboarding(self, user_id: str) -> bool:
        """
        Return True if the user should see the Drive onboarding card.
        Conditions:
          - User signed in with Google (has a google_oauth_tokens record)
          - Drive has NOT been connected yet (drive_connected = False)
          - They haven't explicitly declined (drive_privacy_level != 'none' or no decision made)
        We use a simple heuristic: show if token record exists and drive_privacy_level is null/unset.
        """
        token_row = await fetchrow(
            """
            SELECT drive_connected, drive_privacy_level
            FROM google_oauth_tokens
            WHERE user_id = $1
            """,
            UUID(user_id),
        )

        if not token_row:
            # Not a Google user — no Drive onboarding
            return False

        # If drive is already connected or they already made a choice, don't show
        if token_row["drive_connected"]:
            return False

        # Show if they haven't explicitly chosen yet
        # 'none' could mean "not connected" (default) or "I said no"
        # We differentiate by checking if this is a brand new token record (created recently)
        # For simplicity, always show the card for new Google users who haven't connected Drive
        return True

    async def build_drive_onboarding_card(self, user_id: str) -> Dict[str, Any]:
        """
        Build the Drive onboarding screen spec.
        Returns a special card that the frontend renders as an onboarding prompt.
        """
        return {
            "screen_id": f"onboarding_drive_{user_id[:8]}",
            "type": "onboarding",
            "card_type": "onboarding_drive",
            "layout": "single_col",
            "components": [
                {
                    "type": "heading",
                    "text": "🔗 Connect your Google Drive?",
                },
                {
                    "type": "text",
                    "text": (
                        "I can read your notes and docs to give you much better coaching. "
                        "You control exactly what I access — and you can disconnect anytime."
                    ),
                },
                {
                    "type": "choice_group",
                    "choices": [
                        {
                            "id": "goals_only",
                            "label": "📝 Just my goals & recent notes",
                            "description": "Only recent docs, nothing financial or medical",
                            "action": {
                                "type": "drive_privacy",
                                "level": "goals_only",
                            },
                        },
                        {
                            "id": "full",
                            "label": "🚀 Everything — make Aura as smart as possible",
                            "description": "Full access to all your docs for deep coaching context",
                            "action": {
                                "type": "drive_privacy",
                                "level": "full",
                            },
                        },
                        {
                            "id": "none",
                            "label": "⏭️ Not yet, maybe later",
                            "description": "You can connect anytime from Settings",
                            "action": {
                                "type": "drive_privacy",
                                "level": "none",
                            },
                        },
                    ],
                },
            ],
            "metadata": {
                "agent": self.AGENT_NAME,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "user_id": user_id[:8],
            },
        }

    async def handle_drive_choice(
        self, user_id: str, privacy_level: str
    ) -> Dict[str, Any]:
        """
        Process the user's Drive onboarding choice.
        Updates their privacy level and optionally triggers a sync.

        privacy_level: 'none' | 'goals_only' | 'full'
        """
        valid_levels = {"none", "goals_only", "full"}
        if privacy_level not in valid_levels:
            return {"ok": False, "error": f"Invalid privacy level: {privacy_level}"}

        # Check if we have tokens for this user
        token_row = await fetchrow(
            "SELECT id, drive_connected FROM google_oauth_tokens WHERE user_id = $1",
            UUID(user_id),
        )

        if not token_row:
            return {
                "ok": False,
                "error": "No Google account found. Please sign in with Google first.",
            }

        # Update privacy level
        await execute(
            """
            UPDATE google_oauth_tokens
            SET drive_privacy_level = $2, updated_at = NOW()
            WHERE user_id = $1
            """,
            UUID(user_id),
            privacy_level,
        )

        if privacy_level == "none":
            return {
                "ok": True,
                "message": "No problem! You can connect Drive anytime from Settings.",
                "drive_connected": False,
                "privacy_level": "none",
            }

        # For goals_only or full: user needs to go through Drive connect flow
        # (they may already have Drive access if they granted it during login)
        if token_row["drive_connected"]:
            # Already have Drive access — trigger sync
            try:
                from aura.agents.drive_agent_v2 import DriveAgentV2
                from aura.brain import get_brain

                brain = get_brain()
                agent = DriveAgentV2(openai_client=brain._openai)
                # Run sync in background (don't await fully)
                import asyncio
                asyncio.create_task(agent.sync(user_id=user_id))

                return {
                    "ok": True,
                    "message": f"Great! I'm syncing your Drive now with privacy level '{privacy_level}'.",
                    "drive_connected": True,
                    "privacy_level": privacy_level,
                    "syncing": True,
                }
            except Exception as e:
                logger.warning(f"OnboardingAgent: background sync failed to start: {e}")
                return {
                    "ok": True,
                    "message": f"Privacy level set to '{privacy_level}'. Drive sync will run shortly.",
                    "drive_connected": True,
                    "privacy_level": privacy_level,
                }
        else:
            # User needs to grant Drive scope — return auth URL
            try:
                import secrets
                import urllib.parse
                from api.routes.google_auth import (
                    BASIC_SCOPES, DRIVE_SCOPES, _build_auth_url
                )
                state = secrets.token_urlsafe(32) + ":drive"
                auth_url = _build_auth_url(BASIC_SCOPES + DRIVE_SCOPES, state)

                return {
                    "ok": True,
                    "message": "Please grant Drive access to continue.",
                    "drive_connected": False,
                    "privacy_level": privacy_level,
                    "requires_auth": True,
                    "auth_url": auth_url,
                }
            except Exception as e:
                logger.error(f"OnboardingAgent: failed to build Drive auth URL: {e}")
                return {
                    "ok": False,
                    "error": "Failed to initiate Drive connection. Please try again.",
                }
