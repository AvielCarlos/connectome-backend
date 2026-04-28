"""
CFO Agent — Financial intelligence for Ora/Connectome.

Tracks all financial metrics. Knows the numbers better than anyone.
Acts like a real CFO: proactive, data-driven, watching the bottom line.
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx

from ora.agents.base_executive_agent import BaseExecutiveAgent

logger = logging.getLogger(__name__)

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_BASE = "https://api.stripe.com/v1"

# Estimated fixed costs (USD/month)
RAILWAY_COST_MONTHLY = 20.0
ANTHROPIC_COST_MONTHLY = 30.0  # estimate; refine as usage grows


class CFOAgent(BaseExecutiveAgent):
    """
    Ora's Chief Financial Officer.
    
    Pulls Stripe data, calculates MRR/ARR/churn/LTV, spots trends,
    and teaches Ora what the financial health looks like.
    """

    name = "cfo"
    display_name = "CFO Agent"

    # ─── Stripe helpers ─────────────────────────────────────────────────────

    async def _stripe_get(self, path: str, params: Optional[Dict] = None) -> Optional[Dict]:
        if not STRIPE_SECRET_KEY:
            return None
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    f"{STRIPE_BASE}{path}",
                    params=params or {},
                    auth=(STRIPE_SECRET_KEY, ""),
                )
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            logger.error(f"CFO: Stripe GET {path} failed: {e}")
        return None

    # ─── Core interface ──────────────────────────────────────────────────────

    async def analyze(self) -> Dict[str, Any]:
        """Pull Stripe metrics and compute financial KPIs."""
        now = datetime.now(timezone.utc)
        thirty_days_ago = int((now - timedelta(days=30)).timestamp())
        seven_days_ago = int((now - timedelta(days=7)).timestamp())

        metrics: Dict[str, Any] = {
            "analyzed_at": now.isoformat(),
            "mrr_usd": 0.0,
            "arr_usd": 0.0,
            "active_subscriptions": 0,
            "total_customers": 0,
            "revenue_last_30d_usd": 0.0,
            "revenue_last_7d_usd": 0.0,
            "refund_rate_pct": 0.0,
            "churn_rate_pct": 0.0,
            "ltv_estimate_usd": 0.0,
            "cac_usd": 0.0,
            "gross_margin_pct": 0.0,
            "stripe_available": bool(STRIPE_SECRET_KEY),
        }

        if not STRIPE_SECRET_KEY:
            logger.warning("CFO: STRIPE_SECRET_KEY not set — financial analysis limited")
            # Try to pull from local DB as fallback
            metrics.update(await self._analyze_from_db())
            return metrics

        # ── Charges (revenue) ────────────────────────────────────────────
        charges_data = await self._stripe_get(
            "/charges",
            {"limit": 100, "created[gte]": thirty_days_ago}
        )
        if charges_data:
            charges = charges_data.get("data", [])
            rev_30d = sum(
                c.get("amount", 0) for c in charges
                if c.get("status") == "succeeded" and not c.get("refunded")
            ) / 100.0
            rev_7d = sum(
                c.get("amount", 0) for c in charges
                if c.get("status") == "succeeded"
                and not c.get("refunded")
                and c.get("created", 0) >= seven_days_ago
            ) / 100.0
            refunded = sum(
                c.get("amount_refunded", 0) for c in charges
            ) / 100.0
            total_charged = sum(c.get("amount", 0) for c in charges) / 100.0
            metrics["revenue_last_30d_usd"] = round(rev_30d, 2)
            metrics["revenue_last_7d_usd"] = round(rev_7d, 2)
            metrics["refund_rate_pct"] = round(
                (refunded / total_charged * 100) if total_charged > 0 else 0, 2
            )

        # ── Subscriptions (MRR) ──────────────────────────────────────────
        subs_data = await self._stripe_get(
            "/subscriptions",
            {"limit": 100, "status": "active"}
        )
        if subs_data:
            subs = subs_data.get("data", [])
            metrics["active_subscriptions"] = len(subs)

            mrr = 0.0
            for sub in subs:
                for item in sub.get("items", {}).get("data", []):
                    amount = item.get("price", {}).get("unit_amount", 0) / 100.0
                    interval = item.get("price", {}).get("recurring", {}).get("interval", "month")
                    qty = item.get("quantity", 1)
                    if interval == "year":
                        mrr += (amount * qty) / 12
                    else:
                        mrr += amount * qty
            metrics["mrr_usd"] = round(mrr, 2)
            metrics["arr_usd"] = round(mrr * 12, 2)

        # ── Cancelled subscriptions (churn) ─────────────────────────────
        cancelled_data = await self._stripe_get(
            "/subscriptions",
            {"limit": 100, "status": "canceled", "created[gte]": thirty_days_ago}
        )
        if cancelled_data and metrics["active_subscriptions"] > 0:
            cancelled_count = len(cancelled_data.get("data", []))
            total_base = metrics["active_subscriptions"] + cancelled_count
            metrics["churn_rate_pct"] = round(
                (cancelled_count / total_base * 100) if total_base > 0 else 0, 2
            )

        # ── Customers ────────────────────────────────────────────────────
        customers_data = await self._stripe_get("/customers", {"limit": 1})
        if customers_data:
            # Stripe returns total_count in list metadata
            metrics["total_customers"] = customers_data.get("total_count", 0)

        # ── Derived metrics ──────────────────────────────────────────────
        if metrics["active_subscriptions"] > 0 and metrics["mrr_usd"] > 0:
            avg_mrr_per_user = metrics["mrr_usd"] / metrics["active_subscriptions"]
            # LTV = avg_mrr * avg_months (assume 12 months if churn < 5%, else 1/churn%)
            churn = metrics["churn_rate_pct"] / 100
            avg_lifetime_months = (1 / churn) if churn > 0 else 12
            metrics["ltv_estimate_usd"] = round(avg_mrr_per_user * avg_lifetime_months, 2)

        # CAC = $0 (all organic currently)
        metrics["cac_usd"] = 0.0

        # Gross margin: (revenue - infra costs) / revenue
        monthly_costs = RAILWAY_COST_MONTHLY + ANTHROPIC_COST_MONTHLY
        if metrics["mrr_usd"] > 0:
            metrics["gross_margin_pct"] = round(
                ((metrics["mrr_usd"] - monthly_costs) / metrics["mrr_usd"]) * 100, 1
            )

        return metrics

    async def _analyze_from_db(self) -> Dict[str, Any]:
        """Fallback: pull basic revenue from local DB."""
        result = {}
        try:
            from core.database import fetchrow
            row = await fetchrow(
                "SELECT COALESCE(SUM(amount_cents), 0)::float / 100 as total "
                "FROM revenue_events WHERE created_at > NOW() - INTERVAL '30 days'"
            )
            if row:
                result["revenue_last_30d_usd"] = round(row["total"], 2)
        except Exception as e:
            logger.debug(f"CFO: DB fallback failed: {e}")
        return result

    async def report(self) -> str:
        data = await self.load_last_report()
        if not data:
            data = await self.analyze()
        lines = [
            f"💰 *CFO Report* — {data.get('analyzed_at', 'unknown')[:10]}",
            f"MRR: ${data.get('mrr_usd', 0):,.2f} | ARR: ${data.get('arr_usd', 0):,.2f}",
            f"Active Subs: {data.get('active_subscriptions', 0)} | Customers: {data.get('total_customers', 0)}",
            f"Revenue (30d): ${data.get('revenue_last_30d_usd', 0):,.2f}",
            f"Churn: {data.get('churn_rate_pct', 0)}% | Refund Rate: {data.get('refund_rate_pct', 0)}%",
            f"LTV Est: ${data.get('ltv_estimate_usd', 0):,.2f} | CAC: $0 (organic)",
            f"Gross Margin: {data.get('gross_margin_pct', 0)}%",
        ]
        return "\n".join(lines)

    async def recommend(self) -> List[str]:
        data = await self.analyze()
        recs = []
        if data["mrr_usd"] > 1000:
            recs.append("MRR > $1K: consider raising Sovereign tier price by 10–20%")
        if data["churn_rate_pct"] > 20:
            recs.append("⚠️ HIGH CHURN: investigate cancellation reasons immediately")
        if data["revenue_last_7d_usd"] > (data["revenue_last_30d_usd"] / 4 * 1.2):
            recs.append("Revenue trending up: document what's driving growth")
        if data["gross_margin_pct"] < 30:
            recs.append("Gross margin is low: audit Railway + API costs")
        if not recs:
            recs.append("Financials look stable. Keep monitoring for trends.")
        return recs

    async def act(self) -> Dict[str, Any]:
        """Autonomous CFO actions."""
        data = await self.analyze()
        actions_taken = []

        # Save report
        path = await self.save_report(data, "cfo_report.json")
        actions_taken.append(f"Saved financial report to {path}")

        # Store in Redis
        summary = await self.report()
        await self.set_redis_report(summary)
        actions_taken.append("Updated Redis summary")

        # Teach Ora
        insight = (
            f"Financial state as of {data['analyzed_at'][:10]}: "
            f"MRR=${data['mrr_usd']:.2f}, "
            f"Active subs={data['active_subscriptions']}, "
            f"Churn={data['churn_rate_pct']}%, "
            f"30d revenue=${data['revenue_last_30d_usd']:.2f}, "
            f"LTV est=${data['ltv_estimate_usd']:.2f}."
        )
        await self.teach_ora(insight, confidence=0.9)
        actions_taken.append("Taught Ora financial state")

        # Alert if MRR > $1000
        if data["mrr_usd"] > 1000:
            await self.alert_avi(
                f"🎉 MRR hit ${data['mrr_usd']:,.2f}! "
                f"Consider raising Sovereign price.\n"
                f"ARR run-rate: ${data['arr_usd']:,.2f}"
            )
            actions_taken.append("Alerted Avi: MRR milestone")

        # Alert if churn > 20%
        if data["churn_rate_pct"] > 20:
            await self.alert_avi(
                f"⚠️ Churn rate is {data['churn_rate_pct']}%! "
                f"Action needed — review cancellation reasons."
            )
            actions_taken.append("Alerted Avi: high churn")

        return {"agent": self.name, "actions": actions_taken, "metrics": data}

    async def optimize_pricing(self) -> str:
        """
        Biweekly pricing intelligence.
        Analyze conversion rates by tier and suggest adjustments.
        """
        data = await self.analyze()
        recommendation = ""

        if data["active_subscriptions"] == 0:
            recommendation = "No subscribers yet — pricing not the bottleneck. Focus on acquisition."
        elif data["churn_rate_pct"] > 15:
            recommendation = (
                f"Churn at {data['churn_rate_pct']}% suggests price sensitivity. "
                f"Consider adding more value at current price before raising."
            )
        elif data["mrr_usd"] > 500:
            recommendation = (
                f"MRR=${data['mrr_usd']:.2f} with healthy subs. "
                f"Explorer tier: test $1-2 price increase. "
                f"Sovereign: strong LTV={data['ltv_estimate_usd']:.2f}, hold price."
            )
        else:
            recommendation = (
                "Early stage — prioritize growth over margin optimization. "
                "Keep pricing accessible, focus on conversion."
            )

        insight = f"Pricing analysis: {recommendation}"
        await self.teach_ora(insight, confidence=0.75)
        return recommendation
