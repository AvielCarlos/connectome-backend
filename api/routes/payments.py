"""
Payments API Routes — Ora's monetization layer.

# Set STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET in Railway env vars.
# Create products at dashboard.stripe.com, then set:
#   STRIPE_PRICE_EXPLORER_MONTHLY=price_xxx
#   STRIPE_PRICE_EXPLORER_YEARLY=price_xxx
#   STRIPE_PRICE_SOVEREIGN_MONTHLY=price_xxx
#   STRIPE_PRICE_SOVEREIGN_YEARLY=price_xxx

Endpoints:
  GET  /api/payments/tiers                 — public: current tier definitions
  GET  /api/payments/subscription          — auth: current user subscription
  POST /api/payments/checkout              — auth: create Stripe checkout session
  POST /api/payments/portal               — auth: Stripe billing portal
  POST /api/payments/webhook              — public: Stripe webhook handler
  GET  /api/pricing/proposals             — admin: Ora's tier change proposals
  POST /api/pricing/proposals/{id}/approve — admin: apply a pricing proposal
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from api.middleware import get_current_user_id
from api.tier_guard import get_user_tier
from core.database import execute, fetch, fetchrow, fetchval
from ora.agents.pricing_agent import get_pricing_agent
from ora.payments.stripe_client import StripeError, StripeWebhookError, get_stripe_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["payments"])

# ─── Models ───────────────────────────────────────────────────────────────────


class CheckoutRequest(BaseModel):
    tier: str  # "explorer" | "sovereign"
    billing: str = "monthly"  # "monthly" | "yearly"
    success_url: str = "https://connectome.app/upgrade/success"
    cancel_url: str = "https://connectome.app/upgrade"


class CreditsCheckoutRequest(BaseModel):
    quantity: int = 3  # 3 extra open paths per pack
    success_url: str = "https://avielcarlos.github.io/connectome-web/app/goals?credits=granted"
    cancel_url: str = "https://avielcarlos.github.io/connectome-web/app/goals"


# Credits grant per purchase — 3 extra open paths for $9 one-time
CREDITS_PER_PACK = 3
TIER_PATH_LIMITS = {"free": 4, "explorer": 12, "sovereign": 999}


class PortalRequest(BaseModel):
    return_url: str = "https://connectome.app/account"


# ─── Helper: ensure subscription row exists ───────────────────────────────────


async def _ensure_subscription_row(user_id: str) -> dict:
    """
    Get the subscription row for a user, creating a free-tier row if needed.
    """
    row = await fetchrow(
        "SELECT * FROM subscriptions WHERE user_id = $1",
        str(user_id),
    )
    if not row:
        await execute(
            """
            INSERT INTO subscriptions (user_id, tier, status)
            VALUES ($1, 'free', 'active')
            ON CONFLICT (user_id) DO NOTHING
            """,
            str(user_id),
        )
        row = await fetchrow(
            "SELECT * FROM subscriptions WHERE user_id = $1",
            str(user_id),
        )
    return dict(row) if row else {"user_id": user_id, "tier": "free", "status": "active"}


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.get("/api/payments/tiers")
async def get_tiers():
    """
    Return current tier definitions as set by Ora's PricingAgent.
    Public endpoint — no auth required.
    """
    pricing = get_pricing_agent()
    tiers = await pricing.get_tiers()

    # Enrich with Stripe availability info
    stripe = get_stripe_client()
    stripe_configured = stripe.configured

    return {
        "tiers": tiers,
        "stripe_configured": stripe_configured,
        "currency": "usd",
        "note": "Prices in USD. Explorer and Sovereign require Stripe checkout.",
    }


@router.get("/api/payments/subscription")
async def get_subscription(user_id: str = Depends(get_current_user_id)):
    """Return the current user's subscription status and tier."""
    sub = await _ensure_subscription_row(user_id)
    pricing = get_pricing_agent()
    tiers = await pricing.get_tiers()
    tier_key = sub.get("tier", "free")
    tier_config = tiers.get(tier_key, tiers["free"])

    # Get current usage
    from api.tier_guard import get_current_usage
    daily_screens = await get_current_usage(user_id, "daily_screens")
    goals_count = await get_current_usage(user_id, "goals")

    limits = tier_config.get("limits", {})

    return {
        "tier": tier_key,
        "tier_name": tier_config.get("name", "Ora Free"),
        "status": sub.get("status", "active"),
        "stripe_subscription_id": sub.get("stripe_subscription_id"),
        "current_period_end": sub.get("current_period_end"),
        "cancel_at_period_end": sub.get("cancel_at_period_end", False),
        "trial_end": sub.get("trial_end"),
        "limits": limits,
        "usage": {
            "daily_screens": daily_screens,
            "goals": goals_count,
        },
        "is_free": tier_key == "free",
        "is_paid": tier_key in ("explorer", "sovereign"),
    }


@router.post("/api/payments/checkout")
async def create_checkout_session(
    body: CheckoutRequest,
    user_id: str = Depends(get_current_user_id),
):
    """
    Create a Stripe Checkout session for upgrading to a paid tier.

    The client should redirect to the returned `checkout_url`.
    On success, Stripe sends a webhook which updates the subscription record.
    """
    # Validate tier
    if body.tier not in ("explorer", "sovereign"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid tier: {body.tier}. Must be 'explorer' or 'sovereign'.",
        )

    if body.billing not in ("monthly", "yearly"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid billing: {body.billing}. Must be 'monthly' or 'yearly'.",
        )

    stripe = get_stripe_client()

    if not stripe.configured:
        raise HTTPException(
            status_code=503,
            detail=(
                "Payment processing is not yet configured. "
                "Set STRIPE_SECRET_KEY in Railway env vars and create your products "
                "at dashboard.stripe.com to enable checkout."
            ),
        )

    price_id = stripe.get_price_id(body.tier, body.billing)
    if not price_id:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Stripe Price ID for {body.tier}/{body.billing} is not configured. "
                f"Create the product in Stripe dashboard and set "
                f"STRIPE_PRICE_{body.tier.upper()}_{body.billing.upper()} in Railway env vars."
            ),
        )

    # Get or create Stripe customer
    user_row = await fetchrow(
        "SELECT email FROM users WHERE id = $1",
        str(user_id),
    )
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    sub = await _ensure_subscription_row(user_id)
    customer_id = sub.get("stripe_customer_id")

    try:
        if not customer_id:
            customer = await stripe.create_customer(
                email=user_row["email"],
                user_id=user_id,
            )
            customer_id = customer["id"]
            await execute(
                "UPDATE subscriptions SET stripe_customer_id = $2 WHERE user_id = $1",
                str(user_id),
                customer_id,
            )

        session = await stripe.create_checkout_session(
            customer_id=customer_id,
            price_id=price_id,
            success_url=body.success_url + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=body.cancel_url,
        )

        logger.info(
            f"Checkout session created: user={user_id[:8]} tier={body.tier} billing={body.billing}"
        )

        return {
            "checkout_url": session["url"],
            "session_id": session["id"],
            "tier": body.tier,
            "billing": body.billing,
        }

    except StripeError as e:
        logger.error(f"Stripe checkout error: {e} (code={e.code})")
        raise HTTPException(status_code=502, detail=f"Payment error: {e}")


@router.post("/api/payments/credits/checkout")
async def create_credits_checkout(
    body: CreditsCheckoutRequest,
    user_id: str = Depends(get_current_user_id),
):
    """
    Create a Stripe Checkout session for purchasing path credits (one-time).
    3 credits = 3 extra open paths, $9 one-time.
    Set STRIPE_PRICE_PATH_CREDITS in Railway after creating the product in Stripe.
    """
    stripe = get_stripe_client()
    if not stripe.configured:
        raise HTTPException(status_code=503, detail="Payment processing not configured.")

    import os
    price_id = os.getenv("STRIPE_PRICE_PATH_CREDITS", "")
    if not price_id:
        raise HTTPException(
            status_code=503,
            detail="Path credits product not configured. Set STRIPE_PRICE_PATH_CREDITS in Railway.",
        )

    user_row = await fetchrow("SELECT email FROM users WHERE id = $1", str(user_id))
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    sub = await _ensure_subscription_row(user_id)
    customer_id = sub.get("stripe_customer_id")

    try:
        if not customer_id:
            customer = await stripe.create_customer(email=user_row["email"], user_id=user_id)
            customer_id = customer["id"]
            await execute(
                "UPDATE subscriptions SET stripe_customer_id = $2 WHERE user_id = $1",
                str(user_id), customer_id,
            )

        session = await stripe.create_checkout_session(
            customer_id=customer_id,
            price_id=price_id,
            success_url=body.success_url + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=body.cancel_url,
            mode="payment",
            metadata={"product_type": "path_credits", "credits": str(CREDITS_PER_PACK), "user_id": user_id},
        )

        logger.info(f"Credits checkout created: user={user_id[:8]} credits={CREDITS_PER_PACK}")
        return {"checkout_url": session["url"], "session_id": session["id"], "credits": CREDITS_PER_PACK}

    except StripeError as e:
        raise HTTPException(status_code=502, detail=f"Payment error: {e}")


@router.post("/api/payments/portal")
async def create_billing_portal(
    body: PortalRequest,
    user_id: str = Depends(get_current_user_id),
):
    """
    Create a Stripe Customer Portal session for subscription management.
    Returns a URL the client should redirect to.
    """
    stripe = get_stripe_client()

    if not stripe.configured:
        raise HTTPException(
            status_code=503,
            detail="Payment processing is not yet configured.",
        )

    sub = await _ensure_subscription_row(user_id)
    customer_id = sub.get("stripe_customer_id")

    if not customer_id:
        raise HTTPException(
            status_code=400,
            detail="No Stripe customer found. You must complete a checkout first.",
        )

    try:
        session = await stripe.create_portal_session(
            customer_id=customer_id,
            return_url=body.return_url,
        )
        return {"portal_url": session["url"]}

    except StripeError as e:
        logger.error(f"Stripe portal error: {e}")
        raise HTTPException(status_code=502, detail=f"Payment error: {e}")


@router.post("/api/payments/webhook")
async def stripe_webhook(request: Request):
    """
    Handle Stripe webhook events.
    Stripe sends events here when subscriptions change.

    Configure in Stripe Dashboard:
      Endpoint URL: https://connectome-api-production.up.railway.app/api/payments/webhook
      Events to listen for:
        - customer.subscription.created
        - customer.subscription.updated
        - customer.subscription.deleted
        - invoice.payment_succeeded
        - invoice.payment_failed
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    stripe = get_stripe_client()

    try:
        event = await stripe.handle_webhook(payload, sig_header)
    except StripeWebhookError as e:
        logger.warning(f"Stripe webhook validation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Stripe webhook parse error: {e}")
        raise HTTPException(status_code=400, detail="Invalid webhook payload")

    event_type = event.get("type", "")
    data = event.get("data", {}).get("object", {})

    logger.info(f"Stripe webhook: {event_type} id={event.get('id', '')[:12]}")

    # Alert Avi on any purchase
    try:
        if event_type in ("checkout.session.completed", "invoice.payment_succeeded",
                          "customer.subscription.created", "payment_intent.succeeded"):
            await _alert_avi_purchase(event_type, data)
    except Exception:
        pass

    try:
        if event_type in ("customer.subscription.created", "customer.subscription.updated"):
            await _handle_subscription_upsert(data)

        elif event_type == "customer.subscription.deleted":
            await _handle_subscription_canceled(data)

        elif event_type == "invoice.payment_succeeded":
            sub_id = data.get("subscription")
            if sub_id:
                sub_data = await stripe.get_subscription(sub_id)
                await _handle_subscription_upsert(sub_data)
            # Also track as a service conversion if metadata has UTM info
            await _maybe_record_service_conversion(data, event_type)

        elif event_type == "invoice.payment_failed":
            await _handle_payment_failed(data)

        elif event_type == "checkout.session.completed":
            # Grant path credits on one-time purchase
            await _handle_credits_purchase(data)
            # Track one-time service purchases via checkout sessions
            await _maybe_record_service_conversion(data, event_type)

        elif event_type == "payment_intent.succeeded":
            # Track direct payment intents (one-time service purchases)
            await _maybe_record_service_conversion(data, event_type)

    except Exception as e:
        logger.error(f"Stripe webhook handler error ({event_type}): {e}", exc_info=True)
        # Return 200 to prevent Stripe from retrying — we'll handle data issues separately
        return {"received": True, "error": str(e)}

    return {"received": True, "type": event_type}


async def _maybe_record_service_conversion(data: dict, event_type: str) -> None:
    """Extract UTM metadata from Stripe event and record as a service conversion."""
    try:
        # Stripe metadata is set in checkout session with UTM params
        metadata = data.get("metadata") or {}
        source = metadata.get("utm_source", "")
        medium = metadata.get("utm_medium", "")
        campaign = metadata.get("utm_campaign", "")
        content = metadata.get("utm_content", "")
        service_id = metadata.get("service_id", "")

        # Only record if we have UTM data (Nea's outreach)
        if not source and not campaign:
            return

        # Get amount from the event
        amount = 0.0
        if event_type == "checkout.session.completed":
            amount = data.get("amount_total", 0) / 100.0  # Stripe amounts in cents
            order_id = data.get("id", "")
            currency = data.get("currency", "usd")
        elif event_type == "invoice.payment_succeeded":
            amount = data.get("amount_paid", 0) / 100.0
            order_id = data.get("id", "")
            currency = data.get("currency", "usd")
        elif event_type == "payment_intent.succeeded":
            amount = data.get("amount", 0) / 100.0
            order_id = data.get("id", "")
            currency = data.get("currency", "usd")
        else:
            return

        if not order_id:
            return

        # Record in services_conversions table
        from api.routes.services import record_service_conversion
        await record_service_conversion({
            "order_id": order_id,
            "source": source,
            "medium": medium,
            "campaign": campaign,
            "content": content,
            "service_id": service_id,
            "amount": amount,
            "currency": currency,
        })
        logger.info(f"Service conversion recorded: order={order_id} source={source} amount=${amount}")
    except Exception as e:
        logger.warning(f"Could not record service conversion: {e}")


async def _handle_subscription_upsert(sub: dict) -> None:
    """Upsert subscription record from a Stripe subscription object."""
    customer_id = sub.get("customer")
    if not customer_id:
        return

    # Find user by stripe_customer_id
    row = await fetchrow(
        "SELECT user_id FROM subscriptions WHERE stripe_customer_id = $1",
        customer_id,
    )
    if not row:
        logger.warning(f"Stripe webhook: no user found for customer {customer_id}")
        return

    user_id = row["user_id"]

    # Determine tier from price ID
    price_id = ""
    items = sub.get("items", {}).get("data", [])
    if items:
        price_id = items[0].get("price", {}).get("id", "")

    tier = _price_id_to_tier(price_id)
    status_str = sub.get("status", "active")

    # Map Stripe status to our status
    if status_str in ("active", "trialing"):
        db_status = status_str
    elif status_str == "past_due":
        db_status = "past_due"
    elif status_str in ("canceled", "unpaid", "incomplete_expired"):
        db_status = "canceled"
    else:
        db_status = "active"

    period_start = _ts_to_dt(sub.get("current_period_start"))
    period_end = _ts_to_dt(sub.get("current_period_end"))
    trial_end = _ts_to_dt(sub.get("trial_end"))
    cancel_at_end = sub.get("cancel_at_period_end", False)

    await execute(
        """
        UPDATE subscriptions SET
            tier = $2,
            stripe_subscription_id = $3,
            stripe_price_id = $4,
            status = $5,
            current_period_start = $6,
            current_period_end = $7,
            cancel_at_period_end = $8,
            trial_end = $9,
            updated_at = NOW()
        WHERE user_id = $1
        """,
        user_id,
        tier,
        sub.get("id"),
        price_id,
        db_status,
        period_start,
        period_end,
        cancel_at_end,
        trial_end,
    )

    # Also update users table for backwards compatibility
    await execute(
        "UPDATE users SET subscription_tier = $2 WHERE id = $1",
        user_id,
        tier,
    )
    # Sync path_limit with the new tier
    await _apply_tier_path_limit(str(user_id), tier)

    logger.info(
        f"Subscription updated: user={str(user_id)[:8]} tier={tier} status={db_status}"
    )


async def _handle_subscription_canceled(sub: dict) -> None:
    """Handle subscription cancellation."""
    customer_id = sub.get("customer")
    if not customer_id:
        return

    row = await fetchrow(
        "SELECT user_id FROM subscriptions WHERE stripe_customer_id = $1",
        customer_id,
    )
    if not row:
        return

    user_id = row["user_id"]

    await execute(
        """
        UPDATE subscriptions SET
            tier = 'free',
            status = 'canceled',
            cancel_at_period_end = FALSE,
            updated_at = NOW()
        WHERE user_id = $1
        """,
        user_id,
    )

    await execute(
        "UPDATE users SET subscription_tier = 'free', is_premium = FALSE WHERE id = $1",
        user_id,
    )

    logger.info(f"Subscription canceled: user={str(user_id)[:8]} → downgraded to free")


async def _handle_payment_failed(invoice: dict) -> None:
    """Handle failed payment — mark subscription as past_due."""
    customer_id = invoice.get("customer")
    sub_id = invoice.get("subscription")
    if not customer_id:
        return

    row = await fetchrow(
        "SELECT user_id FROM subscriptions WHERE stripe_customer_id = $1",
        customer_id,
    )
    if not row:
        return

    await execute(
        """
        UPDATE subscriptions SET
            status = 'past_due',
            updated_at = NOW()
        WHERE user_id = $1
        """,
        row["user_id"],
    )

    logger.warning(
        f"Payment failed: user={str(row['user_id'])[:8]} subscription={sub_id}"
    )


async def _handle_credits_purchase(data: dict) -> None:
    """Grant path credits to user after a successful one-time checkout."""
    try:
        metadata = data.get("metadata") or {}
        if metadata.get("product_type") != "path_credits":
            return
        user_id = metadata.get("user_id")
        credits = int(metadata.get("credits", CREDITS_PER_PACK))
        if not user_id:
            # Fall back to customer lookup
            customer_id = data.get("customer")
            if customer_id:
                row = await fetchrow(
                    "SELECT user_id FROM subscriptions WHERE stripe_customer_id = $1", customer_id
                )
                user_id = str(row["user_id"]) if row else None
        if not user_id:
            logger.warning("Credits purchase: could not resolve user_id")
            return
        await execute(
            "UPDATE users SET path_credits = path_credits + $2 WHERE id = $1",
            user_id, credits,
        )
        logger.info(f"Granted {credits} path credits to user {user_id[:8]}")
    except Exception as e:
        logger.error(f"Credits grant failed: {e}")


async def _apply_tier_path_limit(user_id: str, tier: str) -> None:
    """Update users.path_limit when subscription tier changes."""
    try:
        limit = TIER_PATH_LIMITS.get(tier, 4)
        await execute(
            "UPDATE users SET path_limit = $2 WHERE id = $1",
            user_id, limit,
        )
    except Exception as e:
        logger.error(f"path_limit update failed for user {user_id}: {e}")


def _price_id_to_tier(price_id: str) -> str:
    """Map a Stripe Price ID to an Ora tier name."""
    import os
    price_map = {
        os.getenv("STRIPE_PRICE_EXPLORER_MONTHLY", ""): "explorer",
        os.getenv("STRIPE_PRICE_EXPLORER_YEARLY", ""): "explorer",
        os.getenv("STRIPE_PRICE_SOVEREIGN_MONTHLY", ""): "sovereign",
        os.getenv("STRIPE_PRICE_SOVEREIGN_YEARLY", ""): "sovereign",
    }
    return price_map.get(price_id, "explorer")  # Default to explorer for unknown paid prices


def _ts_to_dt(ts) -> Optional[datetime]:
    """Convert Unix timestamp to datetime."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (ValueError, TypeError):
        return None


# ─── Pricing Proposals (Admin) ────────────────────────────────────────────────


@router.get("/api/pricing/proposals")
async def get_pricing_proposals(
    user_id: str = Depends(get_current_user_id),
):
    """
    Return Ora's pending tier change proposals.
    Admin only — in production, add a role check here.
    """
    pricing = get_pricing_agent()
    proposals = await pricing.get_proposals()
    return {
        "proposals": proposals,
        "total": len(proposals),
        "pending": sum(1 for p in proposals if p.get("status") == "pending"),
    }


@router.post("/api/pricing/proposals/{proposal_id}/approve")
async def approve_pricing_proposal(
    proposal_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """
    Apply one of Ora's proposed tier changes to the live config.
    Admin only — in production, add a role check here.
    """
    pricing = get_pricing_agent()
    applied = await pricing.approve_proposal(proposal_id)

    if not applied:
        raise HTTPException(
            status_code=404,
            detail=f"Proposal {proposal_id} not found or could not be applied",
        )

    logger.info(f"Pricing proposal approved by user {user_id[:8]}: {applied.get('title')}")

    return {
        "status": "approved",
        "proposal": applied,
        "message": "Ora's pricing adjustment is now live.",
    }


async def _alert_avi_purchase(event_type: str, data: dict) -> None:
    """Send Telegram alert to Avi on any purchase."""
    import httpx, os
    bot_token = os.getenv("ORA_TELEGRAM_TOKEN", "")
    if not bot_token:
        return

    # Extract purchase details
    amount = 0.0
    customer_email = data.get("customer_email") or data.get("customer_details", {}).get("email", "unknown")
    
    if event_type in ("checkout.session.completed", "payment_intent.succeeded"):
        amount = (data.get("amount_total") or data.get("amount_received") or 0) / 100.0
        currency = data.get("currency", "usd").upper()
        metadata = data.get("metadata") or {}
        service = metadata.get("service_id", "subscription")
    elif event_type in ("invoice.payment_succeeded", "customer.subscription.created"):
        amount = data.get("amount_paid", 0) / 100.0
        currency = data.get("currency", "usd").upper()
        service = "subscription"
    else:
        return

    if amount <= 0:
        return

    emoji = "🎉" if amount >= 30 else "💰"
    msg = (
        f"{emoji} New purchase!\n"
        f"💳 ${amount:.2f} {currency}\n"
        f"📦 {service}\n"
        f"📧 {customer_email}\n"
        f"🔔 {event_type.replace('_', ' ').title()}"
    )

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": "5716959016", "text": msg}
            )
    except Exception:
        pass
