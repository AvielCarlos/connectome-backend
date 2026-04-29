"""
Growth billing helpers for Ora/Connectome revenue experiments.

These functions use Stripe Checkout Sessions with inline price_data so new growth
streams can be activated without pre-created Stripe Price IDs. If
STRIPE_SECRET_KEY is not configured they return safe mock checkout data instead
of failing the product surface.
"""

import logging
import os
from typing import Any, Dict

import httpx

logger = logging.getLogger(__name__)

STRIPE_API_BASE = "https://api.stripe.com/v1"
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://connectome.app")
CURRENCY = "usd"

API_ACCESS_PLANS: Dict[str, Dict[str, Any]] = {
    "developer": {
        "name": "Ora Developer API — 10k calls",
        "unit_amount": 2900,
        "included_calls": 10_000,
        "interval": "month",
    },
    "scale": {
        "name": "Ora Developer API — 100k calls",
        "unit_amount": 9900,
        "included_calls": 100_000,
        "interval": "month",
    },
}

ORA_SESSION_TYPES: Dict[str, Dict[str, Any]] = {
    "clarity": {
        "name": "Ora Clarity Session",
        "description": "One premium 1:1 coaching session with Ora's human-flourishing workflow.",
        "unit_amount": 4900,
    },
    "deep_work": {
        "name": "Ora Deep Work Session",
        "description": "A focused 1:1 session for goals, blockers, and next-action architecture.",
        "unit_amount": 9900,
    },
}


class GrowthBillingError(Exception):
    """Raised when Stripe returns an error for a growth billing action."""

    def __init__(self, message: str, status: int = 400, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def _stripe_key() -> str:
    return os.getenv("STRIPE_SECRET_KEY", "")


def _mock_checkout(kind: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    logger.warning("STRIPE_SECRET_KEY is not configured; returning mock %s checkout", kind)
    return {
        "configured": False,
        "mock": True,
        "id": f"mock_{kind}_checkout",
        "checkout_url": f"{FRONTEND_BASE_URL}/billing/mock?stream={kind}",
        "metadata": metadata,
        "note": "Set STRIPE_SECRET_KEY in Railway to create live Stripe Checkout Sessions.",
    }


async def _create_checkout_session(data: Dict[str, Any]) -> Dict[str, Any]:
    key = _stripe_key()
    if not key:
        return _mock_checkout(str(data.get("metadata[growth_stream]", "growth")), dict(data))

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Stripe-Version": "2024-04-10",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{STRIPE_API_BASE}/checkout/sessions", headers=headers, data=data)

    body = resp.json()
    if resp.status_code >= 400:
        error = body.get("error", {})
        raise GrowthBillingError(
            error.get("message", "Stripe Checkout Session creation failed"),
            status=resp.status_code,
            code=error.get("code", ""),
        )

    return {
        "configured": True,
        "mock": False,
        "id": body.get("id"),
        "checkout_url": body.get("url"),
        "status": body.get("status"),
        "metadata": {k: v for k, v in data.items() if k.startswith("metadata[")},
    }


async def create_api_access_checkout(user_id: str, plan: str) -> Dict[str, Any]:
    """Create a Stripe Checkout Session for developer API access."""
    plan_key = (plan or "developer").lower()
    if plan_key not in API_ACCESS_PLANS:
        raise ValueError(f"Unknown API access plan: {plan}")

    cfg = API_ACCESS_PLANS[plan_key]
    data: Dict[str, Any] = {
        "mode": "subscription",
        "client_reference_id": str(user_id),
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": CURRENCY,
        "line_items[0][price_data][unit_amount]": str(cfg["unit_amount"]),
        "line_items[0][price_data][recurring][interval]": cfg["interval"],
        "line_items[0][price_data][product_data][name]": cfg["name"],
        "line_items[0][price_data][product_data][description]": (
            f"Monthly developer API access with {cfg['included_calls']:,} included calls."
        ),
        "allow_promotion_codes": "true",
        "success_url": f"{FRONTEND_BASE_URL}/developer/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{FRONTEND_BASE_URL}/developer/billing",
        "metadata[growth_stream]": "developer_api",
        "metadata[connectome_user_id]": str(user_id),
        "metadata[plan]": plan_key,
        "subscription_data[metadata][growth_stream]": "developer_api",
        "subscription_data[metadata][connectome_user_id]": str(user_id),
        "subscription_data[metadata][included_calls]": str(cfg["included_calls"]),
    }
    return await _create_checkout_session(data)


async def create_corporate_plan_checkout(org_name: str, seats: int, contact_email: str) -> Dict[str, Any]:
    """Create a Stripe Checkout Session for corporate wellness bulk seats."""
    safe_org = (org_name or "Connectome Corporate Partner").strip()[:120]
    seat_count = max(int(seats or 10), 10)
    email = (contact_email or "").strip()

    data: Dict[str, Any] = {
        "mode": "subscription",
        "customer_email": email,
        "client_reference_id": f"corporate:{safe_org}",
        "line_items[0][quantity]": str(seat_count),
        "line_items[0][price_data][currency]": CURRENCY,
        "line_items[0][price_data][unit_amount]": "800",
        "line_items[0][price_data][recurring][interval]": "month",
        "line_items[0][price_data][product_data][name]": "Ora Corporate Wellness Seat",
        "line_items[0][price_data][product_data][description]": (
            "Bulk Ora access for workplace clarity, fulfilment, and aligned action. Minimum 10 seats."
        ),
        "allow_promotion_codes": "true",
        "success_url": f"{FRONTEND_BASE_URL}/corporate/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{FRONTEND_BASE_URL}/corporate",
        "metadata[growth_stream]": "corporate_wellness",
        "metadata[org_name]": safe_org,
        "metadata[seats]": str(seat_count),
        "metadata[contact_email]": email,
        "subscription_data[metadata][growth_stream]": "corporate_wellness",
        "subscription_data[metadata][org_name]": safe_org,
        "subscription_data[metadata][seats]": str(seat_count),
    }
    if not email:
        data.pop("customer_email")
    return await _create_checkout_session(data)


async def create_ora_session_payment(user_id: str, session_type: str) -> Dict[str, Any]:
    """Create a one-off Stripe Checkout Session for a premium Ora coaching session."""
    type_key = (session_type or "clarity").lower()
    if type_key not in ORA_SESSION_TYPES:
        raise ValueError(f"Unknown Ora session type: {session_type}")

    cfg = ORA_SESSION_TYPES[type_key]
    data: Dict[str, Any] = {
        "mode": "payment",
        "client_reference_id": str(user_id),
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": CURRENCY,
        "line_items[0][price_data][unit_amount]": str(cfg["unit_amount"]),
        "line_items[0][price_data][product_data][name]": cfg["name"],
        "line_items[0][price_data][product_data][description]": cfg["description"],
        "success_url": f"{FRONTEND_BASE_URL}/sessions/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{FRONTEND_BASE_URL}/sessions",
        "metadata[growth_stream]": "ora_session",
        "metadata[connectome_user_id]": str(user_id),
        "metadata[session_type]": type_key,
        "payment_intent_data[metadata][growth_stream]": "ora_session",
        "payment_intent_data[metadata][connectome_user_id]": str(user_id),
    }
    return await _create_checkout_session(data)
