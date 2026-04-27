"""
ReporterAgent — Ora's autonomous daily reporting agent.

Generates a rich daily summary of system state, user activity, and
performance metrics, then sends it via Telegram to Avi.

Endpoint: POST /api/ora/report/daily
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from core.database import fetch, fetchrow
from core.redis_client import get_redis

logger = logging.getLogger(__name__)

# Avi's Telegram chat_id
AVI_CHAT_ID = 5716959016

# Bot token: env var first, then file fallback
def _get_bot_token() -> str:
    token = os.environ.get("ORA_TELEGRAM_TOKEN", "")
    if not token:
        try:
            token_path = "/Users/avielcarlos/.openclaw/secrets/telegram-bot-token.txt"
            with open(token_path) as f:
                token = f.read().strip()
        except Exception:
            pass
    return token


class ReporterAgent:
    """
    Ora's daily reporting agent. Reads all metrics and sends a
    structured Telegram message to Avi.
    """

    def __init__(self):
        self._token = _get_bot_token()

    async def send_daily_report(self) -> Dict[str, Any]:
        """Generate and send the daily report. Returns status dict."""
        logger.info("ReporterAgent: generating daily report...")

        # Gather all metrics
        metrics = await self._gather_metrics()

        # Format message
        message = self._format_report(metrics)

        # Send to Telegram
        sent = await self._send_telegram(message)

        return {
            "sent": sent,
            "chat_id": AVI_CHAT_ID,
            "metrics": metrics,
            "message_preview": message[:200],
        }

    # ------------------------------------------------------------------
    # Metrics gathering
    # ------------------------------------------------------------------

    async def _gather_metrics(self) -> Dict[str, Any]:
        metrics: Dict[str, Any] = {
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "users": {"total": 0, "active_today": 0},
            "engagement": {"top_card": "N/A", "avg_rating": 0.0},
            "goals": {"completed": 0, "active": 0},
            "suggestions": {"submitted": 0, "accepted": 0},
            "events": {"count": 0, "cities": []},
            "drive": {"docs": 0},
            "system": {"api": "ok", "db": "ok", "redis": "ok"},
        }

        try:
            # Users
            row = await fetchrow("SELECT COUNT(*) AS total FROM users")
            if row:
                metrics["users"]["total"] = row["total"] or 0

            today_row = await fetchrow(
                "SELECT COUNT(DISTINCT user_id) AS active FROM interactions WHERE created_at > NOW() - INTERVAL '1 day'"
            )
            if today_row:
                metrics["users"]["active_today"] = today_row["active"] or 0
        except Exception as e:
            logger.warning(f"ReporterAgent: user metrics failed: {e}")

        try:
            # Top performing card type today
            top_row = await fetchrow(
                """
                SELECT ss.agent_type, ROUND(AVG(i.rating)::numeric, 1) AS avg_rating
                FROM interactions i
                JOIN screen_specs ss ON ss.id = i.screen_spec_id
                WHERE i.created_at > NOW() - INTERVAL '1 day'
                  AND i.rating IS NOT NULL
                GROUP BY ss.agent_type
                ORDER BY avg_rating DESC
                LIMIT 1
                """
            )
            if top_row:
                metrics["engagement"]["top_card"] = top_row["agent_type"]
                metrics["engagement"]["avg_rating"] = float(top_row["avg_rating"] or 0)
        except Exception as e:
            logger.warning(f"ReporterAgent: engagement metrics failed: {e}")

        try:
            # Goals
            goal_row = await fetchrow(
                """
                SELECT
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active
                FROM goals
                """
            )
            if goal_row:
                metrics["goals"]["completed"] = goal_row["completed"] or 0
                metrics["goals"]["active"] = goal_row["active"] or 0
        except Exception as e:
            logger.warning(f"ReporterAgent: goals metrics failed: {e}")

        try:
            # Suggestions / contributions
            sugg_row = await fetchrow(
                """
                SELECT COUNT(*) AS submitted,
                       SUM(CASE WHEN status = 'accepted' THEN 1 ELSE 0 END) AS accepted
                FROM contributions
                WHERE created_at > NOW() - INTERVAL '1 day'
                """
            )
            if sugg_row:
                metrics["suggestions"]["submitted"] = sugg_row["submitted"] or 0
                metrics["suggestions"]["accepted"] = sugg_row["accepted"] or 0
        except Exception:
            pass

        try:
            # Events from Redis (world signals cache)
            r = await get_redis()
            event_keys = await r.keys("world_signal:*")
            metrics["events"]["count"] = len(event_keys)
            # Extract city names from keys
            cities = list({k.split(":")[-1] for k in event_keys if ":" in k})[:3]
            metrics["events"]["cities"] = cities
        except Exception as e:
            logger.warning(f"ReporterAgent: events metrics failed: {e}")

        try:
            # Drive docs
            drive_row = await fetchrow(
                "SELECT COUNT(*) AS docs FROM drive_documents"
            )
            if drive_row:
                metrics["drive"]["docs"] = drive_row["docs"] or 0
        except Exception:
            pass

        # System status from Redis ping
        try:
            r = await get_redis()
            await r.ping()
        except Exception:
            metrics["system"]["redis"] = "error"

        return metrics

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def _format_report(self, metrics: Dict[str, Any]) -> str:
        date = metrics["date"]
        total_users = metrics["users"]["total"]
        active_today = metrics["users"]["active_today"]
        top_card = metrics["engagement"]["top_card"]
        goals_completed = metrics["goals"]["completed"]
        goals_active = metrics["goals"]["active"]
        suggestions_submitted = metrics["suggestions"]["submitted"]
        suggestions_accepted = metrics["suggestions"]["accepted"]
        events_count = metrics["events"]["count"]
        cities = ", ".join(metrics["events"]["cities"]) or "none"
        docs = metrics["drive"]["docs"]

        system = metrics["system"]
        all_ok = all(v == "ok" for v in system.values())
        system_line = "All green ✅" if all_ok else "⚠️ Issues: " + ", ".join(
            f"{k}={v}" for k, v in system.items() if v != "ok"
        )

        return (
            f"🧠 Ora Daily Report — {date}\n\n"
            f"👥 Users: {total_users} total, {active_today} active today\n"
            f"📊 Engagement: {top_card} cards performing best\n"
            f"🎯 Goals: {goals_completed} completed, {goals_active} active\n"
            f"💡 Suggestions: {suggestions_submitted} submitted, {suggestions_accepted} accepted\n"
            f"🌍 Events: {events_count} indexed for {cities}\n"
            f"📚 Drive: {docs} docs indexed\n\n"
            f"🔧 System: {system_line}\n"
            f"— Ora"
        )

    # ------------------------------------------------------------------
    # Telegram delivery
    # ------------------------------------------------------------------

    async def _send_telegram(self, text: str) -> bool:
        token = self._token
        if not token:
            logger.warning("ReporterAgent: no Telegram bot token — skipping send")
            return False

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": AVI_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    logger.info("ReporterAgent: daily report sent to Telegram ✅")
                    return True
                else:
                    logger.warning(f"ReporterAgent: Telegram API error {resp.status_code}: {resp.text[:200]}")
                    return False
        except Exception as e:
            logger.error(f"ReporterAgent: Telegram send failed: {e}")
            return False


# Module-level singleton
_reporter: Optional[ReporterAgent] = None


def get_reporter() -> ReporterAgent:
    global _reporter
    if _reporter is None:
        _reporter = ReporterAgent()
    return _reporter
