"""
Tier Guard — Ora enforces her own subscription limits.

Usage:
    from api.tier_guard import check_tier_limit, TierLimitExceeded

    await check_tier_limit(user_id, "daily_screens")  # raises 402 if limit hit
    await check_tier_limit(user_id, "goals")
    await check_tier_limit(user_id, "chat_messages_daily")

Resources:
    "daily_screens"             — discovery cards per day
    "goals"                     — active goals count
    "chat_messages_daily"       — Ora chat messages per day
    "journal_entries_monthly"   — journal entries per month
    "drive_docs_indexed"        — Drive docs indexed
    "event_recommendations_weekly" — event recs per week
"""

import logging
from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status

from core.database import fetchrow, fetchval
from ora.agents.pricing_agent import get_pricing_agent

logger = logging.getLogger(__name__)


class TierLimitExceeded(Exception):
    """Raised when a user has exceeded their tier's resource limit."""

    def __init__(self, resource: str, limit: int, tier: str, upgrade_message: str = ""):
        self.resource = resource
        self.limit = limit
        self.tier = tier
        self.upgrade_message = upgrade_message
        super().__init__(f"Tier limit exceeded: {resource} (limit={limit}, tier={tier})")


async def get_user_tier(user_id: str) -> str:
    """Return the user's current subscription tier."""
    # Admin bypass — admins get sovereign tier unlimited
    try:
        from core.config import settings
        from core.database import fetchrow as _fetchrow
        from uuid import UUID as _UUID
        _row = await _fetchrow("SELECT email FROM users WHERE id = $1", _str(user_id))
        if _row and (_row["email"] or "").lower() in settings.admin_email_list:
            return "sovereign"
    except Exception:
        pass
    # Check new subscriptions table first (Stripe-managed)
    sub_row = await fetchrow(
        "SELECT tier, status FROM subscriptions WHERE user_id = $1",
        str(user_id),
    )
    if sub_row and sub_row["status"] in ("active", "trialing"):
        return sub_row["tier"]

    # Fall back to legacy users.subscription_tier
    user_row = await fetchrow(
        "SELECT subscription_tier FROM users WHERE id = $1",
        str(user_id),
    )
    if user_row:
        tier = user_row["subscription_tier"]
        # Map legacy "premium" to "explorer"
        if tier == "premium":
            return "explorer"
        return tier or "free"

    return "free"


async def get_current_usage(user_id: str, resource: str) -> int:
    """
    Get the current usage count for a resource.
    Returns the count relevant to the resource's time window.
    """
    try:
        if resource == "daily_screens":
            from ora.user_model import get_daily_screen_count
            return await get_daily_screen_count(user_id)

        elif resource == "goals":
            count = await fetchval(
                "SELECT COUNT(*) FROM goals WHERE user_id = $1 AND status = 'active'",
                str(user_id),
            )
            return int(count or 0)

        elif resource == "chat_messages_daily":
            count = await fetchval(
                """
                SELECT COUNT(*) FROM interactions
                WHERE user_id = $1
                  AND interaction_type = 'ora_chat'
                  AND created_at > NOW() - INTERVAL '24 hours'
                """,
                str(user_id),
            )
            return int(count or 0)

        elif resource == "journal_entries_monthly":
            count = await fetchval(
                """
                SELECT COUNT(*) FROM journal_entries
                WHERE user_id = $1
                  AND created_at > NOW() - INTERVAL '30 days'
                """,
                str(user_id),
            )
            return int(count or 0)

        elif resource == "drive_docs_indexed":
            count = await fetchval(
                "SELECT COUNT(*) FROM drive_documents WHERE user_id = $1",
                str(user_id),
            )
            return int(count or 0)

        elif resource == "event_recommendations_weekly":
            count = await fetchval(
                """
                SELECT COUNT(*) FROM interactions
                WHERE user_id = $1
                  AND interaction_type = 'event_recommendation'
                  AND created_at > NOW() - INTERVAL '7 days'
                """,
                str(user_id),
            )
            return int(count or 0)

    except Exception as e:
        logger.debug(f"TierGuard: usage check for {resource} failed: {e}")

    return 0


async def check_tier_limit(
    user_id: str,
    resource: str,
    raise_http: bool = True,
) -> Optional[dict]:
    """
    Check if the user has exceeded their tier limit for the given resource.

    Args:
        user_id: The user to check.
        resource: The resource key (e.g., "daily_screens", "goals").
        raise_http: If True, raises HTTPException(402) on limit exceeded.
                    If False, returns a dict with limit info or None if ok.

    Returns:
        None if within limits.
        Dict with limit info if raise_http=False and limit exceeded.

    Raises:
        HTTPException(402) if raise_http=True and limit exceeded.
    """
    try:
        tier = await get_user_tier(user_id)
        pricing_agent = get_pricing_agent()
        limits = await pricing_agent.get_tier_limits(tier)

        limit = limits.get(resource, -1)

        # -1 means unlimited
        if limit == -1:
            return None

        current = await get_current_usage(user_id, resource)

        if current >= limit:
            upgrade_message = _build_upgrade_message(resource, limit, tier)
            logger.info(
                f"TierGuard: user {user_id[:8]} hit {resource} limit "
                f"(tier={tier}, current={current}, limit={limit})"
            )

            if raise_http:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail={
                        "error": "tier_limit_exceeded",
                        "resource": resource,
                        "current": current,
                        "limit": limit,
                        "tier": tier,
                        "upgrade_message": upgrade_message,
                        "upgrade_url": "/api/payments/checkout",
                    },
                    headers={"X-Upgrade-URL": "/api/payments/checkout"},
                )
            else:
                return {
                    "exceeded": True,
                    "resource": resource,
                    "current": current,
                    "limit": limit,
                    "tier": tier,
                    "upgrade_message": upgrade_message,
                }

        return None

    except HTTPException:
        raise
    except Exception as e:
        # Don't block users on guard errors
        logger.error(f"TierGuard: unexpected error checking {resource}: {e}")
        return None


def _build_upgrade_message(resource: str, limit: int, current_tier: str) -> str:
    """
    Ora writes her own upgrade messages — warm, not pushy.
    """
    messages = {
        "daily_screens": (
            f"You've explored {limit} ideas today ✦\n\n"
            "Want to go deeper? Explorer unlocks unlimited discovery, "
            "full Ora chat, and your personal Drive connection. "
            "$12.99/mo — or join as a Founding Member."
        ),
        "goals": (
            f"You've got {limit} active goals — Ora's keeping you focused ✦\n\n"
            "Explorer removes all limits, adds AI step generation, and connects "
            "your Google Drive to your goals. $12.99/mo."
        ),
        "chat_messages_daily": (
            f"You've had {limit} Ora conversations today ✦\n\n"
            "Explorer unlocks unlimited Ora chat — she's ready to think with you "
            "as long as you need. $12.99/mo."
        ),
        "journal_entries_monthly": (
            "You've journaled deeply this month ✦\n\n"
            "Explorer gives you unlimited journal entries with Ora reflections "
            "after each one. $12.99/mo."
        ),
        "drive_docs_indexed": (
            "Your Drive knowledge limit is reached ✦\n\n"
            "Explorer indexes up to 50 Drive docs. Sovereign unlocks everything — "
            "Ora reads your entire Drive to inform your journey. $29.99/mo."
        ),
        "event_recommendations_weekly": (
            "You've seen all this week's local events ✦\n\n"
            "Explorer gives you unlimited personalized event recommendations — "
            "Ora finds what matters in your city. $12.99/mo."
        ),
    }

    return messages.get(
        resource,
        (
            "You've reached your plan limit ✦\n\n"
            "Explorer unlocks the full Ora experience — unlimited discovery, "
            "goals, and coaching. $12.99/mo."
        ),
    )


async def build_upgrade_card(resource: str, limit: int, tier: str) -> dict:
    """
    Generate a special Ora upgrade card for the discovery feed.
    Called when free users hit the daily screen limit.
    Returns a screen-spec-like dict that can be returned directly.
    """
    message = _build_upgrade_message(resource, limit, tier)

    return {
        "type": "ora_message",
        "layout": "single",
        "is_upgrade_card": True,
        "components": [
            {
                "type": "text",
                "style": "heading",
                "text": "You've reached today's limit ✦",
            },
            {
                "type": "text",
                "style": "body",
                "text": message,
            },
            {
                "type": "button",
                "label": "Unlock Explorer — $12.99/mo",
                "action": {
                    "type": "navigate",
                    "url": "/upgrade",
                },
            },
            {
                "type": "button",
                "label": "See all plans",
                "action": {
                    "type": "navigate",
                    "url": "/upgrade?view=tiers",
                },
                "style": "secondary",
            },
        ],
        "metadata": {
            "agent": "pricing_agent",
            "card_type": "upgrade_prompt",
            "resource": resource,
        },
    }
