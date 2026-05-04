"""
Stripe Payment Client — Aura's gateway to the financial world.

Uses direct httpx calls to the Stripe REST API (no SDK dependency).
More reliable in constrained environments and gives Aura full visibility
into every request and response.

Setup:
  Set STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET in Railway env vars.
  Create products at dashboard.stripe.com, then set:
    STRIPE_PRICE_EXPLORER_MONTHLY=price_xxx
    STRIPE_PRICE_EXPLORER_YEARLY=price_xxx
    STRIPE_PRICE_SOVEREIGN_MONTHLY=price_xxx
    STRIPE_PRICE_SOVEREIGN_YEARLY=price_xxx
"""

import hashlib
import hmac
import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

# Price IDs — set in Railway after creating products in Stripe dashboard
STRIPE_PRICE_IDS = {
    "explorer": {
        "monthly": os.getenv("STRIPE_PRICE_EXPLORER_MONTHLY", ""),
        "yearly": os.getenv("STRIPE_PRICE_EXPLORER_YEARLY", ""),
    },
    "sovereign": {
        "monthly": os.getenv("STRIPE_PRICE_SOVEREIGN_MONTHLY", ""),
        "yearly": os.getenv("STRIPE_PRICE_SOVEREIGN_YEARLY", ""),
    },
}


class StripeClient:
    """
    Aura's Stripe integration. All calls are async and use basic auth
    with the secret key (as per Stripe's API).
    """

    BASE = "https://api.stripe.com/v1"

    def __init__(self, secret_key: str = ""):
        self._key = secret_key or STRIPE_SECRET_KEY
        self._configured = bool(self._key)

    @property
    def configured(self) -> bool:
        return self._configured

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Stripe-Version": "2024-04-10",
        }

    async def _post(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """POST to Stripe API with form encoding."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.BASE}{path}",
                headers=self._headers(),
                data=data,
            )
            body = resp.json()
            if resp.status_code >= 400:
                error = body.get("error", {})
                raise StripeError(
                    error.get("message", "Stripe API error"),
                    code=error.get("code", ""),
                    status=resp.status_code,
                )
            return body

    async def _get(self, path: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """GET from Stripe API."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self.BASE}{path}",
                headers=self._headers(),
                params=params,
            )
            body = resp.json()
            if resp.status_code >= 400:
                error = body.get("error", {})
                raise StripeError(
                    error.get("message", "Stripe API error"),
                    code=error.get("code", ""),
                    status=resp.status_code,
                )
            return body

    async def _delete(self, path: str) -> Dict[str, Any]:
        """DELETE on Stripe API."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.delete(
                f"{self.BASE}{path}",
                headers=self._headers(),
            )
            body = resp.json()
            if resp.status_code >= 400:
                error = body.get("error", {})
                raise StripeError(
                    error.get("message", "Stripe API error"),
                    code=error.get("code", ""),
                    status=resp.status_code,
                )
            return body

    # ─── Customer ─────────────────────────────────────────────────────────────

    async def create_customer(self, email: str, user_id: str) -> Dict[str, Any]:
        """Create a Stripe customer linked to a Connectome user."""
        return await self._post("/customers", {
            "email": email,
            "metadata[connectome_user_id]": user_id,
        })

    async def get_customer(self, customer_id: str) -> Dict[str, Any]:
        """Retrieve a Stripe customer by ID."""
        return await self._get(f"/customers/{customer_id}")

    # ─── Checkout ─────────────────────────────────────────────────────────────

    async def create_checkout_session(
        self,
        customer_id: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
        trial_days: int = 0,
        mode: str = "subscription",
        metadata: Optional[Dict[str, str]] = None,
        client_reference_id: str = "",
    ) -> Dict[str, Any]:
        """Create a Stripe Checkout session for subscription or one-time payment."""
        data: Dict[str, Any] = {
            "customer": customer_id,
            "mode": mode,
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": "1",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "allow_promotion_codes": "true",
            "billing_address_collection": "auto",
        }
        if client_reference_id:
            data["client_reference_id"] = client_reference_id
        if metadata:
            for key, value in metadata.items():
                data[f"metadata[{key}]"] = str(value)
                if mode == "subscription":
                    data[f"subscription_data[metadata][{key}]"] = str(value)
        if trial_days > 0:
            data["subscription_data[trial_period_days]"] = str(trial_days)

        return await self._post("/checkout/sessions", data)

    # ─── Billing Portal ───────────────────────────────────────────────────────

    async def create_portal_session(
        self,
        customer_id: str,
        return_url: str,
    ) -> Dict[str, Any]:
        """Create a Stripe Customer Portal session for subscription management."""
        return await self._post("/billing_portal/sessions", {
            "customer": customer_id,
            "return_url": return_url,
        })

    # ─── Subscriptions ────────────────────────────────────────────────────────

    async def get_subscription(self, subscription_id: str) -> Dict[str, Any]:
        """Retrieve a subscription by ID."""
        return await self._get(f"/subscriptions/{subscription_id}")

    async def cancel_subscription(
        self,
        subscription_id: str,
        at_period_end: bool = True,
    ) -> Dict[str, Any]:
        """Cancel a subscription (default: at period end, not immediately)."""
        return await self._post(f"/subscriptions/{subscription_id}", {
            "cancel_at_period_end": "true" if at_period_end else "false",
        })

    async def list_subscriptions(self, customer_id: str) -> Dict[str, Any]:
        """List active subscriptions for a customer."""
        return await self._get("/subscriptions", {
            "customer": customer_id,
            "status": "active",
            "limit": "5",
        })

    # ─── Webhooks ─────────────────────────────────────────────────────────────

    async def handle_webhook(
        self,
        payload: bytes,
        sig_header: str,
        webhook_secret: str = "",
    ) -> Dict[str, Any]:
        """
        Verify Stripe webhook signature and return the parsed event.
        Raises StripeWebhookError if signature is invalid.
        """
        secret = webhook_secret or STRIPE_WEBHOOK_SECRET
        if not secret:
            if settings.is_production:
                raise StripeWebhookError("Stripe webhook secret not configured")
            logger.warning("Stripe webhook secret not configured — skipping verification")
            import json
            return json.loads(payload)

        # Verify signature using HMAC-SHA256
        try:
            parts = {k: v for k, v in (p.split("=", 1) for p in sig_header.split(","))}
            timestamp = parts.get("t", "")
            signature = parts.get("v1", "")

            signed_payload = f"{timestamp}.{payload.decode('utf-8')}"
            expected = hmac.new(
                secret.encode("utf-8"),
                signed_payload.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()

            if not hmac.compare_digest(expected, signature):
                raise StripeWebhookError("Invalid webhook signature")

            # Reject stale events (> 5 minutes)
            age = abs(time.time() - int(timestamp))
            if age > 300:
                raise StripeWebhookError(f"Webhook event too old: {age:.0f}s")

        except (KeyError, ValueError) as e:
            raise StripeWebhookError(f"Malformed Stripe-Signature header: {e}")

        import json
        return json.loads(payload)

    # ─── Price ID Lookup ──────────────────────────────────────────────────────

    def get_price_id(self, tier: str, billing: str) -> Optional[str]:
        """Look up the Stripe Price ID for a tier + billing cycle."""
        price_id = STRIPE_PRICE_IDS.get(tier, {}).get(billing, "")
        return price_id if price_id else None


# ─── Errors ───────────────────────────────────────────────────────────────────

class StripeError(Exception):
    def __init__(self, message: str, code: str = "", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class StripeWebhookError(Exception):
    pass


# ─── Singleton ────────────────────────────────────────────────────────────────

_stripe_client: Optional[StripeClient] = None


def get_stripe_client() -> StripeClient:
    global _stripe_client
    if _stripe_client is None:
        _stripe_client = StripeClient()
    return _stripe_client
