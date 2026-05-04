"""
Monetization API Routes
Freemium upgrade, affiliate tracking, and admin insights.
"""

import logging
import json
from typing import List
from uuid import UUID
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, status

from core.models import SubscriptionUpgrade, AffiliateClick, AffiliateConversion, AdminInsights
from core.database import fetchrow, fetch, execute, fetchval
from api.middleware import get_current_user_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/monetization", tags=["monetization"])


@router.post("/upgrade")
async def upgrade_to_premium(
    body: SubscriptionUpgrade,
    user_id: str = Depends(get_current_user_id),
):
    """
    Upgrade user to premium.
    In production this would call Stripe; here we mock the payment flow.
    """
    # In production: validate payment_method_token with Stripe
    # stripe.PaymentIntent.create(amount=1299, currency='usd', ...)

    row = await fetchrow(
        "SELECT subscription_tier FROM users WHERE id = $1", UUID(user_id)
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    if row["subscription_tier"] == "premium":
        return {"status": "already_premium", "message": "You're already on premium!"}

    # Mock: just upgrade them
    now = datetime.now(timezone.utc)
    await execute(
        "UPDATE users SET subscription_tier = 'premium', is_premium = TRUE, premium_since = $2 WHERE id = $1",
        UUID(user_id), now,
    )

    # Record revenue event
    await execute(
        """
        INSERT INTO revenue_events (user_id, event_type, amount_cents, metadata)
        VALUES ($1, 'subscription_start', $2, $3)
        """,
        UUID(user_id),
        1299,
        json.dumps({"plan": body.plan, "token": body.payment_method_token[:8] + "..."}),
    )

    # Invalidate cache
    from core.redis_client import redis_delete
    await redis_delete(f"user_model:{user_id}")

    logger.info(f"User {user_id[:8]} upgraded to premium")

    return {
        "status": "success",
        "message": "Welcome to Connectome Premium! Unlimited daily screens activated.",
        "subscription_tier": "premium",
    }


@router.post("/affiliate/click")
async def track_affiliate_click(
    body: AffiliateClick,
    user_id: str = Depends(get_current_user_id),
):
    """Track an affiliate link click."""
    await execute(
        """
        INSERT INTO revenue_events (user_id, event_type, amount_cents, metadata)
        VALUES ($1, 'affiliate_click', 0, $2)
        """,
        UUID(user_id),
        json.dumps({
            "tracking_id": body.tracking_id,
            "screen_spec_id": body.screen_spec_id,
            "url": body.url,
        }),
    )
    logger.info(f"Affiliate click: tracking_id={body.tracking_id[:8]} user={user_id[:8]}")
    return {"ok": True}


@router.post("/affiliate/conversion")
async def track_affiliate_conversion(
    body: AffiliateConversion,
    user_id: str = Depends(get_current_user_id),
):
    """Track an affiliate conversion (e.g., after redirect back from partner)."""
    amount = body.amount_cents or 0

    await execute(
        """
        INSERT INTO revenue_events (user_id, event_type, amount_cents, metadata)
        VALUES ($1, 'affiliate_conversion', $2, $3)
        """,
        UUID(user_id),
        amount,
        json.dumps({"tracking_id": body.tracking_id}),
    )

    logger.info(
        f"Affiliate conversion: tracking_id={body.tracking_id[:8]} "
        f"amount=${amount/100:.2f} user={user_id[:8]}"
    )
    return {"ok": True, "amount_cents": amount}


@router.get("/admin/insights", response_model=AdminInsights)
async def admin_insights(
    user_id: str = Depends(get_current_user_id),
):
    # Enforce admin-only access (is_admin stored in profile JSONB)
    user_row = await fetchrow(
        "SELECT profile FROM users WHERE id = $1", UUID(user_id)
    )
    profile = user_row["profile"] if user_row else {}
    if isinstance(profile, str):
        import json as _json
        try:
            profile = _json.loads(profile)
        except Exception:
            profile = {}
    if not profile.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    """
    Aggregate insights for the admin dashboard.
    Returns user stats, revenue, and top-performing agents.
    """
    # Total users
    total_users = await fetchval("SELECT COUNT(*) FROM users") or 0

    # Active today
    active_today = await fetchval(
        "SELECT COUNT(DISTINCT user_id) FROM interactions "
        "WHERE created_at > NOW() - INTERVAL '24 hours'"
    ) or 0

    # Premium users
    premium_users = await fetchval(
        "SELECT COUNT(*) FROM users WHERE subscription_tier = 'premium'"
    ) or 0

    # Avg fulfilment score
    avg_fulfilment = await fetchval(
        "SELECT AVG(fulfilment_score) FROM users WHERE fulfilment_score > 0"
    ) or 0.0

    # Total revenue
    total_revenue = await fetchval(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM revenue_events "
        "WHERE event_type IN ('subscription_start', 'affiliate_conversion')"
    ) or 0

    # Top agents by impression count and rating
    agent_rows = await fetch(
        """
        SELECT agent_type,
               COUNT(*) as impressions,
               AVG(global_rating) as avg_rating,
               SUM(completion_count) as completions
        FROM screen_specs
        WHERE agent_type IS NOT NULL
        GROUP BY agent_type
        ORDER BY impressions DESC
        LIMIT 10
        """
    )

    top_agents = [
        {
            "agent": r["agent_type"],
            "impressions": r["impressions"],
            "avg_rating": round(r["avg_rating"] or 0, 2),
            "completions": r["completions"] or 0,
        }
        for r in agent_rows
    ]

    avg_rating_by_agent = {
        r["agent_type"]: round(r["avg_rating"] or 0, 2)
        for r in agent_rows
        if r["agent_type"]
    }

    return AdminInsights(
        total_users=int(total_users),
        active_today=int(active_today),
        premium_users=int(premium_users),
        avg_fulfilment_score=round(float(avg_fulfilment), 3),
        total_revenue_cents=int(total_revenue),
        top_agents=top_agents,
        avg_rating_by_agent=avg_rating_by_agent,
    )


@router.get("/status")
async def subscription_status(user_id: str = Depends(get_current_user_id)):
    """Get current subscription status and usage for today."""
    from core.config import settings
    from aura.user_model import get_daily_screen_count

    row = await fetchrow(
        "SELECT subscription_tier FROM users WHERE id = $1", UUID(user_id)
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    tier = row["subscription_tier"]
    screens_today = await get_daily_screen_count(user_id)
    daily_limit = settings.FREE_TIER_DAILY_SCREENS if tier == "free" else None

    return {
        "subscription_tier": tier,
        "screens_today": screens_today,
        "daily_limit": daily_limit,
        "is_limited": tier == "free",
        "upgrade_price_cents": settings.PREMIUM_PRICE_CENTS,
    }
