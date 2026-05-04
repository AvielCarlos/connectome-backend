"""
CGO routes — growth analysis and new revenue-stream billing.
"""

import logging
import os
from typing import Any, Dict

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from api.middleware import get_current_user_id
from aura.agents.cgo_agent import run_cgo_growth_analysis
from aura.payments.growth_billing import (
    API_ACCESS_PLANS,
    ORA_SESSION_TYPES,
    GrowthBillingError,
    create_api_access_checkout,
    create_corporate_plan_checkout,
    create_aura_session_payment,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cgo", tags=["cgo"])

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN") or os.getenv("ADMIN_SECRET", "")


class AuraSessionCheckoutRequest(BaseModel):
    session_type: str = "clarity"


class CorporateCheckoutRequest(BaseModel):
    org_name: str = "Connectome Corporate Partner"
    seats: int = 10
    contact_email: str = "growth@connectome.app"


def _require_admin(x_admin_token: str = Header(default="")) -> None:
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")


def _translate_billing_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, GrowthBillingError):
        return HTTPException(status_code=exc.status, detail={"message": str(exc), "code": exc.code})
    logger.exception("Unexpected CGO billing error: %s", exc)
    return HTTPException(status_code=500, detail="Unable to create checkout session")


@router.get("/growth-report")
async def growth_report(x_admin_token: str = Header(default="")) -> Dict[str, Any]:
    """Run the CGO analysis and return a structured growth report. Admin-only."""
    _require_admin(x_admin_token)
    return await run_cgo_growth_analysis()


@router.get("/billing/api-access")
async def billing_api_access(
    plan: str = Query("developer", description="developer | scale"),
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Create a Stripe Checkout Session for the developer API access tier."""
    try:
        checkout = await create_api_access_checkout(user_id=user_id, plan=plan)
        return {
            "stream": "developer_api",
            "plan": plan,
            "available_plans": API_ACCESS_PLANS,
            **checkout,
        }
    except Exception as exc:
        raise _translate_billing_error(exc)


@router.get("/billing/corporate")
async def billing_corporate(
    org_name: str = Query("Connectome Corporate Partner"),
    seats: int = Query(10, ge=10),
    contact_email: str = Query("growth@connectome.app"),
    x_admin_token: str = Header(default=""),
) -> Dict[str, Any]:
    """
    Return corporate wellness plan info and create a Stripe Checkout Session.
    Admin-only because this is a negotiated B2B stream.
    """
    _require_admin(x_admin_token)
    try:
        checkout = await create_corporate_plan_checkout(
            org_name=org_name,
            seats=seats,
            contact_email=str(contact_email),
        )
        return {
            "stream": "corporate_wellness",
            "plan": {
                "unit_amount_cents_per_seat": 800,
                "currency": "usd",
                "interval": "month",
                "minimum_seats": 10,
                "requested_seats": max(seats, 10),
                "monthly_total_cents": max(seats, 10) * 800,
            },
            **checkout,
        }
    except Exception as exc:
        raise _translate_billing_error(exc)


@router.post("/billing/corporate")
async def billing_corporate_post(
    body: CorporateCheckoutRequest,
    x_admin_token: str = Header(default=""),
) -> Dict[str, Any]:
    """POST variant for corporate checkout creation with a JSON body."""
    _require_admin(x_admin_token)
    try:
        checkout = await create_corporate_plan_checkout(
            org_name=body.org_name,
            seats=body.seats,
            contact_email=str(body.contact_email),
        )
        return {
            "stream": "corporate_wellness",
            "plan": {
                "unit_amount_cents_per_seat": 800,
                "currency": "usd",
                "interval": "month",
                "minimum_seats": 10,
                "requested_seats": max(body.seats, 10),
                "monthly_total_cents": max(body.seats, 10) * 800,
            },
            **checkout,
        }
    except Exception as exc:
        raise _translate_billing_error(exc)


@router.post("/billing/aura-session")
async def billing_aura_session(
    body: AuraSessionCheckoutRequest,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Create a one-off Stripe Checkout Session for a premium Aura Session."""
    try:
        checkout = await create_aura_session_payment(user_id=user_id, session_type=body.session_type)
        return {
            "stream": "aura_session",
            "session_type": body.session_type,
            "available_session_types": ORA_SESSION_TYPES,
            **checkout,
        }
    except Exception as exc:
        raise _translate_billing_error(exc)
