"""
Nea-as-Agent-for-Hire — Autonomous digital services marketplace.

Users request services through the app or by emailing nea@atdao.org.
They pay via Stripe Checkout, and Nea (Ora's agent persona) delivers.

Stripe keys live in Railway env vars:
  STRIPE_SECRET_KEY
  STRIPE_WEBHOOK_SECRET
"""

import hashlib
import hmac
import logging
import os
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.database import fetchrow, fetch, execute
from api.middleware import get_current_user_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/services", tags=["services"])

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://avielcarlos.github.io/connectome-web").rstrip("/")

# ---------------------------------------------------------------------------
# Service catalog
# ---------------------------------------------------------------------------

SERVICE_CATALOG = [
    {
        "id": "ora-goal-path-map",
        "name": "Ora Goal Path Map",
        "description": (
            "Ora turns one clarified intention into a practical IOO node map: attainable goal, "
            "prerequisites, user-owned steps, Ora-owned actions, and next decisions. Price covers "
            "model/search/tool costs plus a small growth margin."
        ),
        "price_usd": 19,
        "delivery_hours": 24,
        "agent": "ora_goal_path_agent",
        "icon": "🧭",
    },
    {
        "id": "ora-opportunity-scout",
        "name": "Ora Opportunity Scout",
        "description": (
            "Ora researches concrete opportunities connected to a goal — people, places, tools, "
            "events, jobs, communities, services, grants, or resources — then ranks the best next nodes. "
            "Price covers research/tool/model costs plus growth margin."
        ),
        "price_usd": 49,
        "delivery_hours": 48,
        "agent": "ora_opportunity_scout_agent",
        "icon": "🔎",
    },
    {
        "id": "ora-delegated-action-pack",
        "name": "Ora Delegated Action Pack",
        "description": (
            "Ora handles a small bundle of approved digital execution work for a goal: drafting, comparing, "
            "planning, admin, setup, outreach prep, or booking research. External commitments still need user approval. "
            "Price covers agent/tool costs plus growth margin."
        ),
        "price_usd": 99,
        "delivery_hours": 72,
        "agent": "ora_delegated_action_agent",
        "icon": "⚡",
    },
    {
        "id": "ora-ai-os-setup-beta",
        "name": "Ora Personal AI OS Setup — Beta",
        "description": (
            "Founder-led private AI operating system setup for entrepreneurs, creators, and "
            "mission-led operators. Includes discovery, OS map, custom Ora persona/system "
            "instructions, 3-5 practical AI workflows, and a 30-day implementation path. "
            "Founder Beta: 3 private builds only."
        ),
        "price_usd": 1500,
        "delivery_hours": 168,
        "agent": "ora_ai_os_setup_agent",
        "icon": "🧭",
    },
    {
        "id": "ora-ai-os-setup-standard",
        "name": "Ora Personal AI OS Setup — Standard",
        "description": (
            "Bespoke 14-day personal AI OS build for clarity, execution, and momentum. "
            "Avi maps your life/business domains, architects your knowledge and context layer, "
            "creates personalised assistant instructions, and installs repeatable AI workflows "
            "around your real goals and tools."
        ),
        "price_usd": 3500,
        "delivery_hours": 336,
        "agent": "ora_ai_os_setup_agent",
        "icon": "🧬",
    },
    {
        "id": "research-report",
        "name": "Research Report",
        "description": (
            "Nea researches any topic and delivers a comprehensive report with sources, "
            "analysis, and key insights. 24-48h delivery."
        ),
        "price_usd": 29,
        "delivery_hours": 48,
        "agent": "research_agent",
        "icon": "🔍",
    },
    {
        "id": "code-review",
        "name": "Code Review",
        "description": (
            "Submit a GitHub repo or file. Nea reviews architecture, security, and quality. "
            "Delivers written report + suggested improvements."
        ),
        "price_usd": 49,
        "delivery_hours": 24,
        "agent": "code_review_agent",
        "icon": "💻",
    },
    {
        "id": "content-pack",
        "name": "Content Pack",
        "description": (
            "5 high-quality social media posts, 1 blog article, and a content calendar "
            "for any topic or brand."
        ),
        "price_usd": 39,
        "delivery_hours": 24,
        "agent": "content_agent",
        "icon": "✍️",
    },
    {
        "id": "data-analysis",
        "name": "Data Analysis",
        "description": (
            "Upload a CSV or connect a data source. Nea analyzes it, finds patterns, "
            "and delivers insights + visualizations."
        ),
        "price_usd": 59,
        "delivery_hours": 48,
        "agent": "data_agent",
        "icon": "📊",
    },
    {
        "id": "custom",
        "name": "Custom Request",
        "description": (
            "Email nea@atdao.org with your need. Nea will respond with a quote and timeline."
        ),
        "price_usd": None,
        "delivery_hours": None,
        "agent": "custom_agent",
        "icon": "⚡",
    },
]

_CATALOG_BY_ID = {s["id"]: s for s in SERVICE_CATALOG}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ServiceOrderRequest(BaseModel):
    service_id: str
    description: str
    context: Optional[str] = None
    email: Optional[str] = None  # for non-logged-in orders
    source: Optional[str] = None
    campaign: Optional[str] = None
    medium: Optional[str] = None
    content: Optional[str] = None
    term: Optional[str] = None
    referrer: Optional[str] = None


# ---------------------------------------------------------------------------
# GET /api/services/catalog
# ---------------------------------------------------------------------------

@router.get("/catalog")
async def get_catalog():
    """Return the full service catalog."""
    return {"services": SERVICE_CATALOG}


# ---------------------------------------------------------------------------
# POST /api/services/order
# ---------------------------------------------------------------------------

@router.post("/order")
async def create_service_order(
    body: ServiceOrderRequest,
    request: Request,
    user_id: Optional[str] = None,
):
    """
    Create a Stripe Checkout session for a one-time service purchase.
    Returns { checkout_url, order_id, estimated_delivery }.
    On successful payment the Stripe webhook triggers the agent work.
    """
    service = _CATALOG_BY_ID.get(body.service_id)
    if not service:
        raise HTTPException(status_code=404, detail=f"Unknown service: {body.service_id}")
    if service["price_usd"] is None:
        # Custom requests go via email
        return {
            "checkout_url": "mailto:nea@atdao.org?subject=Custom%20Service%20Request",
            "order_id": None,
            "estimated_delivery": None,
            "custom": True,
        }

    if not STRIPE_SECRET_KEY:
        raise HTTPException(
            status_code=503,
            detail="Payment processing is not configured. Please email nea@atdao.org.",
        )

    order_id = str(uuid.uuid4())
    price_cents = service["price_usd"] * 100

    # Redirect customers back to the web app, not the API origin.
    # FRONTEND_BASE_URL should be set in Railway if the production frontend moves.
    origin = FRONTEND_BASE_URL

    try:
        import httpx

        attribution = {
            "source": body.source,
            "campaign": body.campaign,
            "medium": body.medium,
            "content": body.content,
            "term": body.term,
            "referrer": body.referrer,
        }

        payload = {
            "mode": "payment",
            "payment_method_types[]": "card",
            "line_items[0][price_data][currency]": "usd",
            "line_items[0][price_data][unit_amount]": str(price_cents),
            "line_items[0][price_data][product_data][name]": service["name"],
            "line_items[0][price_data][product_data][description]": service["description"][:500],
            "success_url": f"{origin}/services?order={order_id}&status=success",
            "cancel_url": f"{origin}/services?order={order_id}&status=cancel",
            "metadata[order_id]": order_id,
            "metadata[service_id]": body.service_id,
            "metadata[user_id]": user_id or "",
        }
        for key, value in attribution.items():
            if value:
                payload[f"metadata[{key}]"] = value[:500]
                payload[f"metadata[utm_{key}]"] = value[:500]
        if body.email:
            payload["customer_email"] = body.email

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.stripe.com/v1/checkout/sessions",
                data=payload,
                auth=(STRIPE_SECRET_KEY, ""),
                timeout=15,
            )
        resp.raise_for_status()
        session_data = resp.json()
        stripe_session_id = session_data.get("id")
        checkout_url = session_data.get("url")
    except Exception as exc:
        logger.error(f"Services: Stripe checkout creation failed: {exc}")
        raise HTTPException(status_code=502, detail="Failed to create payment session. Please try again.")

    # Store order in database
    try:
        await execute(
            """
            INSERT INTO service_orders (id, user_id, service_id, description, stripe_session_id, status)
            VALUES ($1, $2::uuid, $3, $4, $5, 'pending_payment')
            """,
            order_id,
            user_id,
            body.service_id,
            body.description,
            stripe_session_id,
        )
    except Exception as exc:
        logger.warning(f"Services: could not store order {order_id}: {exc}")

    from datetime import datetime, timezone, timedelta
    estimated = None
    if service["delivery_hours"]:
        eta = datetime.now(timezone.utc) + timedelta(hours=service["delivery_hours"])
        estimated = eta.isoformat()

    return {
        "checkout_url": checkout_url,
        "order_id": order_id,
        "estimated_delivery": estimated,
    }


# ---------------------------------------------------------------------------
# GET /api/services/my-orders
# ---------------------------------------------------------------------------

@router.get("/my-orders")
async def get_my_orders(user_id: str = Depends(get_current_user_id)):
    """List the authenticated user's service orders with status."""
    try:
        rows = await fetch(
            """
            SELECT id, service_id, description, status, created_at, delivered_at, result
            FROM service_orders
            WHERE user_id = $1::uuid
            ORDER BY created_at DESC
            LIMIT 50
            """,
            user_id,
        )
    except Exception as exc:
        logger.warning(f"Services: could not fetch orders for {user_id}: {exc}")
        return {"orders": []}

    from datetime import datetime
    orders = []
    for row in rows:
        d = dict(row)
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        # Enrich with catalog info
        svc = _CATALOG_BY_ID.get(d.get("service_id", ""), {})
        d["service_name"] = svc.get("name", d.get("service_id", ""))
        d["service_icon"] = svc.get("icon", "📦")
        orders.append(d)

    return {"orders": orders}


# ---------------------------------------------------------------------------
# POST /api/services/webhook/stripe
# ---------------------------------------------------------------------------

@router.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    """
    Handle Stripe webhook events.
    On checkout.session.completed:
      1. Update order status to 'in_progress'
      2. Deliver the service result
      3. Update order status to 'delivered'
    """
    payload_bytes = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    # Verify webhook signature
    if STRIPE_WEBHOOK_SECRET:
        try:
            _verify_stripe_signature(payload_bytes, sig_header, STRIPE_WEBHOOK_SECRET)
        except Exception as exc:
            logger.warning(f"Services webhook: signature verification failed: {exc}")
            raise HTTPException(status_code=400, detail="Invalid webhook signature")

    try:
        import json
        event = json.loads(payload_bytes)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = event.get("type")
    if event_type != "checkout.session.completed":
        return {"received": True}

    session = event.get("data", {}).get("object", {})
    metadata = session.get("metadata", {})
    order_id = metadata.get("order_id")
    service_id = metadata.get("service_id")
    customer_email = session.get("customer_details", {}).get("email") or session.get("customer_email")

    if not order_id:
        logger.warning("Services webhook: checkout.session.completed missing order_id in metadata")
        return {"received": True}

    # Update status to in_progress
    try:
        await execute(
            "UPDATE service_orders SET status = 'in_progress' WHERE id = $1",
            order_id,
        )
    except Exception as exc:
        logger.error(f"Services webhook: failed to update order {order_id}: {exc}")

    # Deliver the service work asynchronously
    import asyncio
    asyncio.create_task(_deliver_service(order_id, service_id, metadata, customer_email))

    # Record UTM conversion if outreach metadata present
    utm_source = metadata.get("utm_source", "")
    if utm_source:
        import asyncio as _asyncio
        amount_cents = session.get("amount_total", 0)
        _asyncio.create_task(record_service_conversion_internal({
            "order_id": str(session.get("id", order_id)),
            "source": utm_source,
            "medium": metadata.get("utm_medium", ""),
            "campaign": metadata.get("utm_campaign", ""),
            "content": metadata.get("utm_content", ""),
            "service_id": service_id or "",
            "amount": amount_cents / 100.0 if amount_cents else 0.0,
            "currency": session.get("currency", "usd"),
        }))

    return {"received": True}


async def _deliver_service(
    order_id: str,
    service_id: str,
    metadata: dict,
    customer_email: Optional[str],
):
    """
    Perform the actual service work and deliver the result.
    This runs in the background after payment confirmation.
    """
    try:
        # Fetch order details
        order = await fetchrow(
            "SELECT id, service_id, description FROM service_orders WHERE id = $1",
            order_id,
        )
        if not order:
            logger.error(f"Services delivery: order {order_id} not found")
            return

        description = order["description"]
        result_text = await _run_service_agent(service_id, description, order_id)

        # Save result and mark delivered
        from datetime import datetime, timezone
        await execute(
            """
            UPDATE service_orders
            SET status = 'delivered', result = $1, delivered_at = NOW()
            WHERE id = $2
            """,
            result_text[:10000],  # cap at 10k chars for the DB column
            order_id,
        )

        # Email result to customer if we have their address
        if customer_email:
            await _email_result(customer_email, service_id, order_id, result_text)

        logger.info(f"Services: order {order_id} ({service_id}) delivered successfully")

    except Exception as exc:
        logger.error(f"Services: delivery failed for order {order_id}: {exc}")
        try:
            await execute(
                "UPDATE service_orders SET status = 'failed' WHERE id = $1",
                order_id,
            )
        except Exception:
            pass


async def _run_service_agent(service_id: str, description: str, order_id: str) -> str:
    """
    Execute the service-specific work. Returns the result as a string.
    v1: lightweight implementations that produce real value quickly.
    """
    if service_id == "research-report":
        return await _run_research_agent(description, order_id)
    elif service_id == "code-review":
        return await _run_code_review_agent(description, order_id)
    elif service_id == "content-pack":
        return await _run_content_agent(description, order_id)
    elif service_id == "data-analysis":
        return await _run_data_agent(description, order_id)
    else:
        return (
            f"Thank you for your order! Our team has received your custom request: '{description}'. "
            "Nea will follow up within 24 hours with a detailed plan and deliverables."
        )


async def _run_research_agent(topic: str, order_id: str) -> str:
    """Research agent: uses Ora brain to generate a structured report."""
    try:
        from ora.brain import get_brain
        brain = get_brain()
        prompt = (
            f"You are Nea, an expert research analyst at Ascension Technologies. "
            f"A client has requested a research report on the following topic:\n\n"
            f'"{topic}"\n\n'
            "Please produce a comprehensive, well-structured research report with:\n"
            "1. Executive Summary (2-3 paragraphs)\n"
            "2. Key Findings (5-7 bullet points with data/facts)\n"
            "3. Detailed Analysis (3-4 sections, ~200 words each)\n"
            "4. Implications & Opportunities\n"
            "5. Recommended Actions (5 specific, actionable steps)\n"
            "6. Sources & Further Reading (suggest 5-8 credible sources)\n\n"
            "Format as clean, professional markdown. Be specific, insightful, and genuinely useful."
        )
        response = await brain.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3000,
        )
        return response or f"Research report on '{topic}' — generation encountered an issue. Please contact nea@atdao.org."
    except Exception as exc:
        logger.error(f"Research agent failed: {exc}")
        return (
            f"# Research Report: {topic}\n\n"
            "Your research report is being prepared by Nea. "
            "Due to a processing issue, our team will deliver it manually within 24 hours. "
            "We apologize for any inconvenience and you will receive the full report via email."
        )


async def _run_code_review_agent(description: str, order_id: str) -> str:
    """Code review agent: fetch repo info and provide analysis."""
    try:
        from ora.brain import get_brain
        brain = get_brain()
        prompt = (
            f"You are Nea, an expert software engineer and code reviewer at Ascension Technologies. "
            f"A client has submitted the following for code review:\n\n"
            f'"{description}"\n\n'
            "Please provide a comprehensive code review covering:\n"
            "1. Architecture Assessment\n"
            "2. Security Review (potential vulnerabilities, best practices)\n"
            "3. Code Quality & Maintainability\n"
            "4. Performance Considerations\n"
            "5. Testing Coverage\n"
            "6. Specific Improvement Recommendations (numbered list)\n"
            "7. Overall Score (1-10) with justification\n\n"
            "Be specific, technical, and genuinely helpful. Format as clean markdown."
        )
        response = await brain.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2500,
        )
        return response or "Code review in progress. Our team will deliver manually within 24 hours."
    except Exception as exc:
        logger.error(f"Code review agent failed: {exc}")
        return "Your code review is being prepared. Please contact nea@atdao.org if you have questions."


async def _run_content_agent(description: str, order_id: str) -> str:
    """Content agent: generate 5 social posts + 1 blog article."""
    try:
        from ora.brain import get_brain
        brain = get_brain()
        prompt = (
            f"You are Nea, a world-class content strategist and writer at Ascension Technologies. "
            f"A client wants a content pack about:\n\n"
            f'"{description}"\n\n'
            "Please create:\n"
            "## 5 Social Media Posts\n"
            "Write 5 distinct posts optimized for different platforms/angles. "
            "Each should be engaging, shareable, and on-brand. Include relevant hashtags.\n\n"
            "## 1 Blog Article (600-800 words)\n"
            "Write a high-quality, SEO-friendly blog post with a compelling headline, "
            "introduction, 3-4 body sections, and a call-to-action.\n\n"
            "## Content Calendar (2 weeks)\n"
            "A simple 2-week posting schedule showing when to publish each piece.\n\n"
            "Format everything as clean, ready-to-use markdown."
        )
        response = await brain.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3000,
        )
        return response or "Content pack in progress. Our team will deliver manually within 24 hours."
    except Exception as exc:
        logger.error(f"Content agent failed: {exc}")
        return "Your content pack is being prepared. Please contact nea@atdao.org if you have questions."


async def _run_data_agent(description: str, order_id: str) -> str:
    """Data analysis agent: analyze the described dataset."""
    try:
        from ora.brain import get_brain
        brain = get_brain()
        prompt = (
            f"You are Nea, a senior data analyst and scientist at Ascension Technologies. "
            f"A client has requested data analysis for the following:\n\n"
            f'"{description}"\n\n'
            "Please provide:\n"
            "1. Analysis Plan (what you would analyze and why)\n"
            "2. Key Metrics to Track\n"
            "3. Visualization Recommendations (what charts/graphs to create)\n"
            "4. Statistical Methods to Apply\n"
            "5. Expected Insights (what patterns to look for)\n"
            "6. Business Implications\n"
            "7. Data Quality Checklist\n\n"
            "Note: If you receive the actual data file, provide specific analysis. "
            "Otherwise, provide a comprehensive framework tailored to their specific use case.\n\n"
            "Format as clean, professional markdown."
        )
        response = await brain.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2500,
        )
        return response or "Data analysis framework prepared. Our team will deliver the full analysis within 48 hours."
    except Exception as exc:
        logger.error(f"Data agent failed: {exc}")
        return "Your data analysis is being prepared. Please contact nea@atdao.org if you have questions."


async def _email_result(email: str, service_id: str, order_id: str, result: str):
    """Send the service result to the customer via email."""
    svc = _CATALOG_BY_ID.get(service_id, {})
    service_name = svc.get("name", service_id)
    subject = f"Your {service_name} from Nea is ready! (Order {order_id[:8]})"

    body = (
        f"Hi there,\n\n"
        f"Your {service_name} order has been completed! Here's your deliverable:\n\n"
        f"{'=' * 60}\n\n"
        f"{result}\n\n"
        f"{'=' * 60}\n\n"
        f"Order ID: {order_id}\n\n"
        f"Questions or revisions? Reply to this email or contact nea@atdao.org.\n\n"
        f"Thank you for choosing Nea!\n"
        f"— Nea, Autonomous Agent at Ascension Technologies\n"
    )

    try:
        import subprocess
        # Use himalaya or system mail as best-effort delivery
        subprocess.run(
            ["himalaya", "send", "--subject", subject, "--to", email, "--body", body],
            capture_output=True,
            text=True,
            timeout=30,
        )
        logger.info(f"Services: result email sent to {email} for order {order_id}")
    except Exception as exc:
        logger.warning(f"Services: email delivery failed for {email}: {exc}")


def _verify_stripe_signature(payload: bytes, sig_header: str, secret: str) -> None:
    """Verify Stripe webhook signature (HMAC-SHA256)."""
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    timestamp = parts.get("t", "")
    v1_sig = parts.get("v1", "")

    if not timestamp or not v1_sig:
        raise ValueError("Invalid Stripe signature header")

    # Reject stale webhooks (>5 min)
    if abs(time.time() - int(timestamp)) > 300:
        raise ValueError("Webhook timestamp too old")

    signed_payload = f"{timestamp}.{payload.decode()}"
    expected = hmac.new(
        secret.encode(),
        signed_payload.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, v1_sig):
        raise ValueError("Stripe signature mismatch")


# ---------------------------------------------------------------------------
# Sales Metrics Endpoints (UTM tracking for Nea's outreach)
# ---------------------------------------------------------------------------

from datetime import timedelta


async def _ensure_services_clicks_table():
    """Create the services_clicks table if it doesn't exist yet."""
    await execute("""
        CREATE TABLE IF NOT EXISTS services_clicks (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            date_key TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            medium TEXT NOT NULL DEFAULT '',
            campaign TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            service_id TEXT NOT NULL DEFAULT '',
            ip_hash TEXT NOT NULL DEFAULT '',
            user_agent TEXT NOT NULL DEFAULT ''
        )
    """)
    await execute("CREATE INDEX IF NOT EXISTS idx_svc_clicks_date ON services_clicks(date_key)")
    await execute("CREATE INDEX IF NOT EXISTS idx_svc_clicks_source ON services_clicks(source, date_key)")


async def _ensure_services_conversions_table():
    """Create the services_conversions table if it doesn't exist yet."""
    await execute("""
        CREATE TABLE IF NOT EXISTS services_conversions (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            order_id TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL DEFAULT '',
            medium TEXT NOT NULL DEFAULT '',
            campaign TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            service_id TEXT NOT NULL DEFAULT '',
            amount NUMERIC(10, 2) NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'usd'
        )
    """)
    await execute("CREATE INDEX IF NOT EXISTS idx_svc_conv_src ON services_conversions(source, created_at)")


@router.post("/metrics/click")
async def record_service_click(request: Request, body: dict):
    """
    Record a UTM click — no auth required (tracking pixel equivalent).
    body: {source, medium, campaign, content, service_id}
    """
    await _ensure_services_clicks_table()

    source = body.get("source", "")
    medium = body.get("medium", "")
    campaign = body.get("campaign", "")
    content = body.get("content", "")
    service_id = body.get("service_id", "")
    date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    client_ip = request.client.host if request.client else ""
    ip_hash = hashlib.sha256(client_ip.encode()).hexdigest()[:16] if client_ip else ""
    user_agent = request.headers.get("user-agent", "")[:200]

    await execute(
        """
        INSERT INTO services_clicks
            (date_key, source, medium, campaign, content, service_id, ip_hash, user_agent)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        date_key, source, medium, campaign, content, service_id, ip_hash, user_agent,
    )

    logger.info(f"Service click tracked: source={source} campaign={campaign} content={content}")
    return {"ok": True}


@router.get("/metrics/summary")
async def get_metrics_summary(
    days: int = 7,
    user_id: str = Depends(get_current_user_id),
):
    """Aggregated click/conversion data — admin only."""
    row = await fetchrow("SELECT is_admin FROM users WHERE id = $1", user_id)
    if not row or not row.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")

    await _ensure_services_clicks_table()
    await _ensure_services_conversions_table()

    from datetime import timezone as tz
    cutoff = datetime.now(tz.utc) - timedelta(days=days)
    cutoff_str = cutoff.isoformat()

    click_rows = await fetch(
        "SELECT source, content, COUNT(*) as cnt FROM services_clicks WHERE created_at >= $1 GROUP BY source, content ORDER BY cnt DESC",
        cutoff_str,
    )
    conv_rows = await fetch(
        "SELECT source, content, COUNT(*) as cnt, SUM(amount) as revenue FROM services_conversions WHERE created_at >= $1 GROUP BY source, content ORDER BY cnt DESC",
        cutoff_str,
    )

    total_clicks = {}
    for r in click_rows:
        total_clicks[r["source"]] = total_clicks.get(r["source"], 0) + r["cnt"]

    total_convs = {}
    total_revenue = {}
    for r in conv_rows:
        total_convs[r["source"]] = total_convs.get(r["source"], 0) + r["cnt"]
        total_revenue[r["source"]] = total_revenue.get(r["source"], 0.0) + float(r["revenue"] or 0)

    all_sources = set(list(total_clicks.keys()) + list(total_convs.keys()))
    by_source = {}
    for src in all_sources:
        cl = total_clicks.get(src, 0)
        cv = total_convs.get(src, 0)
        rev = total_revenue.get(src, 0.0)
        by_source[src] = {
            "clicks": cl,
            "conversions": cv,
            "revenue": rev,
            "conversion_rate": round(cv / cl * 100, 2) if cl > 0 else 0,
        }

    return {
        "period_days": days,
        "total_clicks": sum(total_clicks.values()),
        "total_conversions": sum(total_convs.values()),
        "total_revenue": sum(total_revenue.values()),
        "by_source": by_source,
        "top_variants": [{"source": r["source"], "content": r["content"], "clicks": r["cnt"]} for r in click_rows[:10]],
    }


@router.post("/metrics/conversion")
async def record_service_conversion_internal(body: dict):
    """
    Internal: record a confirmed service conversion with UTM attribution.
    Called from Stripe webhook when checkout.session.completed fires.
    body: {order_id, source, medium, campaign, content, service_id, amount, currency}
    """
    await _ensure_services_conversions_table()

    order_id = body.get("order_id", "")
    if not order_id:
        return {"ok": False, "error": "order_id required"}

    try:
        await execute(
            """
            INSERT INTO services_conversions
                (order_id, source, medium, campaign, content, service_id, amount, currency)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (order_id) DO NOTHING
            """,
            order_id,
            body.get("source", ""),
            body.get("medium", ""),
            body.get("campaign", ""),
            body.get("content", ""),
            body.get("service_id", ""),
            float(body.get("amount", 0)),
            body.get("currency", "usd"),
        )
        logger.info(f"Conversion recorded: order={order_id} source={body.get('source')} amount=${body.get('amount')}")
    except Exception as e:
        logger.error(f"Failed to record conversion {order_id}: {e}")
        return {"ok": False, "error": str(e)}

    return {"ok": True}
