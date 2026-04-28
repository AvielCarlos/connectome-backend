"""
Services API Routes — Nea's sales metrics tracking.

Endpoints:
  POST /api/services/metrics/click     — public: record a UTM click (tracking pixel equivalent)
  GET  /api/services/metrics/summary   — admin: aggregated click/conversion data
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from api.middleware import get_current_user_id
from core.database import execute, fetch, fetchrow, fetchval

logger = logging.getLogger(__name__)

router = APIRouter(tags=["services"])


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_services_clicks_date ON services_clicks(date_key)
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_services_clicks_source ON services_clicks(source, date_key)
    """)


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
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_services_conv_source ON services_conversions(source, created_at)
    """)


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/api/services/metrics/click")
async def record_service_click(request: Request, body: dict):
    """
    Record a click from a UTM source.
    body: {source, medium, campaign, content, service_id}
    No auth required — tracking pixel equivalent.
    """
    await _ensure_services_clicks_table()

    source = body.get("source", "")
    medium = body.get("medium", "")
    campaign = body.get("campaign", "")
    content = body.get("content", "")
    service_id = body.get("service_id", "")
    date_key = _today_key()

    # Hash the IP for privacy
    import hashlib
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

    logger.info(f"Service click: source={source} campaign={campaign} content={content}")
    return {"ok": True}


@router.get("/api/services/metrics/summary")
async def get_metrics_summary(
    days: int = 7,
    user_id: str = Depends(get_current_user_id),
):
    """
    Get aggregated sales metrics — admin only.
    Query params: days (default 7)
    """
    # Verify admin
    row = await fetchrow(
        "SELECT is_admin FROM users WHERE id = $1",
        user_id,
    )
    if not row or not row.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")

    await _ensure_services_clicks_table()
    await _ensure_services_conversions_table()

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_str = cutoff.isoformat()

    # Clicks by source
    click_rows = await fetch(
        """
        SELECT source, content, COUNT(*) as cnt
        FROM services_clicks
        WHERE created_at >= $1
        GROUP BY source, content
        ORDER BY cnt DESC
        """,
        cutoff_str,
    )

    # Conversions by source
    conv_rows = await fetch(
        """
        SELECT source, content, COUNT(*) as cnt, SUM(amount) as revenue
        FROM services_conversions
        WHERE created_at >= $1
        GROUP BY source, content
        ORDER BY cnt DESC
        """,
        cutoff_str,
    )

    # Total clicks per source for CTR calculation
    total_clicks = {}
    for row in click_rows:
        src = row["source"]
        total_clicks[src] = total_clicks.get(src, 0) + row["cnt"]

    total_convs = {}
    total_revenue = {}
    for row in conv_rows:
        src = row["source"]
        total_convs[src] = total_convs.get(src, 0) + row["cnt"]
        total_revenue[src] = total_revenue.get(src, 0.0) + float(row["revenue"] or 0)

    # Build by_source summary
    all_sources = set(list(total_clicks.keys()) + list(total_convs.keys()))
    by_source = {}
    for src in all_sources:
        clicks = total_clicks.get(src, 0)
        convs = total_convs.get(src, 0)
        rev = total_revenue.get(src, 0.0)
        by_source[src] = {
            "clicks": clicks,
            "conversions": convs,
            "revenue": rev,
            "conversion_rate": round(convs / clicks * 100, 2) if clicks > 0 else 0,
        }

    # Top content variants
    top_variants = [
        {"source": r["source"], "content": r["content"], "clicks": r["cnt"]}
        for r in click_rows[:10]
    ]

    return {
        "period_days": days,
        "total_clicks": sum(total_clicks.values()),
        "total_conversions": sum(total_convs.values()),
        "total_revenue": sum(total_revenue.values()),
        "by_source": by_source,
        "top_variants": top_variants,
    }


@router.post("/api/services/metrics/conversion")
async def record_service_conversion(body: dict):
    """
    Internal endpoint — record a service conversion with source tracking.
    body: {order_id, source, medium, campaign, content, service_id, amount, currency}
    Called from the Stripe webhook when a payment completes.
    """
    # This endpoint is called internally (not exposed publicly)
    # It's used by the Stripe webhook handler
    await _ensure_services_conversions_table()

    order_id = body.get("order_id", "")
    source = body.get("source", "")
    medium = body.get("medium", "")
    campaign = body.get("campaign", "")
    content = body.get("content", "")
    service_id = body.get("service_id", "")
    amount = float(body.get("amount", 0))
    currency = body.get("currency", "usd")

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
            order_id, source, medium, campaign, content, service_id, amount, currency,
        )
        logger.info(f"Service conversion: order={order_id} source={source} amount=${amount}")
    except Exception as e:
        logger.error(f"Failed to record conversion: {e}")
        return {"ok": False, "error": str(e)}

    return {"ok": True}
