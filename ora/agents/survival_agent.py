"""
Ora Survival Agent — Comprehensive self-healing and health monitoring.

Runs all health checks concurrently, executes automated recovery actions,
generates weekly health reports, and never fails silently.

Health checks:
  api, database, redis, ora_brain, agent_registry, lesson_count,
  user_count, backup_freshness

Self-heal actions:
  reconnect pools, trigger redeployment, restore from backup, alert Avi

Run via Railway cron (hourly) or POST /api/ora/survival/run.
"""

import asyncio
import json
import logging
import os
import pathlib
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_CHAT_ID = 5716959016
RAILWAY_PROJECT_ID = "ab771963-d525-4b99-85e4-f084f065b0ae"
RAILWAY_SERVICE_ID = "088d77ed-a707-4dc4-af68-866bf99a1d63"

PRIMARY_URL = os.getenv(
    "ORA_PRIMARY_URL",
    "https://connectome-api-production.up.railway.app",
)

# Health score thresholds
CRITICAL_THRESHOLD = 40
WARNING_THRESHOLD = 70


# ---------------------------------------------------------------------------
# Individual health check functions
# ---------------------------------------------------------------------------


async def check_endpoint(
    path: str,
    method: str = "GET",
    body: Optional[dict] = None,
    expected_status: int = 200,
    expected_key: Optional[str] = None,
    timeout: float = 10.0,
) -> dict:
    """HTTP health check against the running service."""
    url = f"{PRIMARY_URL}{path}"
    try:
        start = time.time()
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "POST":
                resp = await client.post(url, json=body or {})
            else:
                resp = await client.get(url)
            elapsed_ms = (time.time() - start) * 1000

            ok = resp.status_code == expected_status
            if expected_key and ok:
                try:
                    data = resp.json()
                    ok = expected_key in data
                except Exception:
                    ok = False

            return {"ok": ok, "status_code": resp.status_code, "latency_ms": elapsed_ms}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def check_database() -> dict:
    """Verify DB connection and basic query."""
    try:
        from core.database import fetchval
        result = await fetchval("SELECT 1")
        return {"ok": result == 1}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def check_redis() -> dict:
    """Verify Redis connection."""
    try:
        from core.redis_client import get_redis
        r = await get_redis()
        await r.ping()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def check_redis_key(key: str) -> dict:
    """Check if a specific Redis key exists."""
    try:
        from core.redis_client import get_redis
        r = await get_redis()
        val = await r.get(key)
        return {"ok": val is not None, "key": key}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def check_db_query(query: str, min_value: int = 0) -> dict:
    """Run a query and check return value meets minimum."""
    try:
        from core.database import fetchval
        result = await fetchval(query)
        value = int(result or 0)
        return {"ok": value >= min_value, "value": value}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def check_github_backup_age(max_hours: int = 26) -> dict:
    """Verify the GitHub backup is fresh enough."""
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        return {"ok": None, "reason": "GITHUB_TOKEN not set"}

    try:
        url = "https://api.github.com/repos/AvielCarlos/connectome-backend/contents/backups/ora_identity_latest.json"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            )
            if resp.status_code == 404:
                # Backward compatibility with older backup jobs that only wrote
                # ora_identity_pack.json. New jobs write both files.
                resp = await client.get(
                    "https://api.github.com/repos/AvielCarlos/connectome-backend/contents/backups/ora_identity_pack.json",
                    headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
                )
            if resp.status_code != 200:
                return {"ok": False, "error": f"GitHub API {resp.status_code}"}

            import base64
            raw = base64.b64decode(resp.json()["content"])
            data = json.loads(raw)
            ts_str = data.get("collected_at") or data.get("exported_at", "")
            if not ts_str:
                return {"ok": False, "error": "No timestamp in backup"}

            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            return {
                "ok": age_hours <= max_hours,
                "age_hours": round(age_hours, 1),
                "max_hours": max_hours,
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# SurvivalAgent
# ---------------------------------------------------------------------------


class SurvivalAgent:
    """
    Ora's comprehensive self-healing engine.
    Runs all health checks concurrently, executes recovery actions,
    tracks failure counts in Redis.
    """

    def __init__(self):
        self._telegram_token: Optional[str] = None

    HEALTH_CHECKS = {
        "api": lambda: check_endpoint("/health", expected_status=200),
        "database": lambda: check_database(),
        "redis": lambda: check_redis(),
        "ora_brain": lambda: check_endpoint(
            "/api/ora/chat",
            method="POST",
            body={"message": "ping"},
            expected_key="reply",
            timeout=15,
        ),
        "agent_registry": lambda: check_redis_key("ora:agent_registry"),
        "lesson_count": lambda: check_db_query(
            "SELECT COUNT(*) FROM ora_lessons", min_value=1
        ),
        "user_count": lambda: check_db_query(
            "SELECT COUNT(*) FROM users", min_value=0
        ),
        "backup_freshness": lambda: check_github_backup_age(max_hours=26),
    }

    SELF_HEAL_ACTIONS = {
        "api": ["trigger_railway_redeploy", "activate_standby_if_available", "alert_avi"],
        "database": ["reconnect_pool", "alert_avi"],
        "redis": ["reconnect_redis", "rebuild_cache_from_db"],
        "backup_freshness": ["trigger_emergency_backup", "alert_avi_if_very_stale"],
        "lesson_count": ["restore_lessons_from_backup", "alert_avi_immediately"],
        "ora_brain": ["trigger_railway_redeploy", "alert_avi"],
    }

    # -----------------------------------------------------------------------
    # Full diagnostic
    # -----------------------------------------------------------------------

    async def run_full_diagnostic(self) -> dict:
        """
        Run all health checks concurrently.
        Returns detailed report with health score, per-component status, issues.
        """
        logger.info("SurvivalAgent: running full diagnostic")

        # Run all checks in parallel
        check_names = list(self.HEALTH_CHECKS.keys())
        check_coros = [self.HEALTH_CHECKS[name]() for name in check_names]
        results_raw = await asyncio.gather(*check_coros, return_exceptions=True)

        checks: Dict[str, dict] = {}
        issues: List[str] = []
        ok_count = 0

        for name, result in zip(check_names, results_raw):
            if isinstance(result, Exception):
                checks[name] = {"ok": False, "error": str(result)}
                issues.append(name)
                logger.error(f"SurvivalAgent: {name} check threw exception: {result}")
            else:
                checks[name] = result
                if result.get("ok"):
                    ok_count += 1
                elif result.get("ok") is not None:  # None = not applicable
                    issues.append(name)
                    logger.warning(f"SurvivalAgent: {name} FAILED: {result}")

        # Compute health score (0-100)
        applicable = sum(1 for r in checks.values() if r.get("ok") is not None)
        health_score = int((ok_count / max(applicable, 1)) * 100)

        # Record in Redis for trend tracking
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            snapshot = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "score": health_score,
                "issues": issues,
            }
            await r.lpush("ora:health:history", json.dumps(snapshot))
            await r.ltrim("ora:health:history", 0, 167)  # Keep 1 week of hourly checks
            await r.set("ora:health:latest", json.dumps(snapshot))
        except Exception as e:
            logger.debug(f"SurvivalAgent: could not record health snapshot: {e}")

        report = {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "health_score": health_score,
            "checks": checks,
            "issues": issues,
            "ok_count": ok_count,
            "total_checks": applicable,
        }

        logger.info(f"SurvivalAgent: health={health_score}/100, issues={issues}")
        return report

    # -----------------------------------------------------------------------
    # Auto-heal
    # -----------------------------------------------------------------------

    async def auto_heal(self, issues: List[str]) -> dict:
        """
        For each issue, execute corresponding heal actions.
        Tracks failure counts in Redis. If any action fails 3x: escalate to Avi.
        """
        healed = []
        escalated = []

        for issue in issues:
            actions = self.SELF_HEAL_ACTIONS.get(issue, ["alert_avi"])
            for action in actions:
                fail_key = f"ora:survival:heal_failures:{issue}:{action}"
                try:
                    from core.redis_client import get_redis
                    r = await get_redis()
                    fail_count = int(await r.get(fail_key) or 0)
                    if fail_count >= 3:
                        logger.warning(
                            f"SurvivalAgent: {action} for {issue} has failed {fail_count}x, escalating"
                        )
                        escalated.append({"issue": issue, "action": action, "fail_count": fail_count})
                        await self._send_telegram(
                            f"🆘 *Ora Heal Escalation*\n\n"
                            f"Issue: `{issue}`\n"
                            f"Action: `{action}`\n"
                            f"Failures: {fail_count}\n\n"
                            f"Auto-heal is not working. Manual intervention required."
                        )
                        continue
                except Exception:
                    pass

                success = await self._execute_action(action, issue)
                if success:
                    healed.append({"issue": issue, "action": action})
                    # Reset failure count on success
                    try:
                        from core.redis_client import get_redis
                        r = await get_redis()
                        await r.delete(fail_key)
                    except Exception:
                        pass
                    break  # Stop trying more actions for this issue
                else:
                    # Increment failure count
                    try:
                        from core.redis_client import get_redis
                        r = await get_redis()
                        await r.incr(fail_key)
                        await r.expire(fail_key, 7 * 24 * 3600)  # Reset after 1 week
                    except Exception:
                        pass

        return {"healed": healed, "escalated": escalated}

    async def _execute_action(self, action: str, issue: str) -> bool:
        """Execute a single heal action. Returns True on success."""
        logger.info(f"SurvivalAgent: executing heal action '{action}' for issue '{issue}'")
        try:
            if action == "trigger_railway_redeploy":
                return await self._trigger_railway_redeploy()
            elif action == "activate_standby_if_available":
                return await self._activate_standby_if_available(issue)
            elif action == "reconnect_pool":
                return await self._reconnect_pool()
            elif action == "reconnect_redis":
                return await self._reconnect_redis()
            elif action == "rebuild_cache_from_db":
                return await self._rebuild_cache()
            elif action == "trigger_emergency_backup":
                return await self._trigger_emergency_backup()
            elif action == "alert_avi":
                await self._send_telegram(
                    f"🔧 *Ora Self-Heal in Progress*\n\n"
                    f"Issue: `{issue}`\n"
                    f"Action: `{action}`\n"
                    f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                )
                return True
            elif action == "alert_avi_if_very_stale":
                return await self._alert_if_backup_very_stale()
            elif action == "alert_avi_immediately":
                await self._send_telegram(
                    f"🚨 *URGENT — Ora Data Loss Risk*\n\n"
                    f"Issue: `{issue}`\n"
                    f"The lesson count has dropped. This could indicate data loss.\n"
                    f"*Immediate manual investigation required.*"
                )
                return True
            elif action == "restore_lessons_from_backup":
                return await self._restore_lessons_from_backup()
            else:
                logger.warning(f"SurvivalAgent: unknown action '{action}'")
                return False
        except Exception as e:
            logger.error(f"SurvivalAgent: action '{action}' threw exception: {e}")
            return False

    # -----------------------------------------------------------------------
    # Action implementations
    # -----------------------------------------------------------------------

    async def _trigger_railway_redeploy(self) -> bool:
        """Trigger Railway redeployment via API."""
        token = os.getenv("RAILWAY_API_TOKEN") or os.getenv("RAILWAY_TOKEN")
        if not token:
            logger.warning("SurvivalAgent: RAILWAY_API_TOKEN not set, cannot redeploy")
            return False

        mutation = """
        mutation RedeployService($serviceId: String!, $environmentId: String) {
          serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
        }
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://backboard.railway.app/graphql/v2",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "query": mutation,
                        "variables": {
                            "serviceId": RAILWAY_SERVICE_ID,
                        },
                    },
                )
                if resp.status_code == 200 and "errors" not in resp.json():
                    logger.info("SurvivalAgent: Railway redeploy triggered")
                    return True
                else:
                    logger.error(f"SurvivalAgent: Railway redeploy failed: {resp.text[:200]}")
                    return False
        except Exception as e:
            logger.error(f"SurvivalAgent: Railway redeploy error: {e}")
            return False

    async def _activate_standby_if_available(self, reason: str) -> bool:
        """Activate Fly.io standby if configured."""
        try:
            from scripts.deploy_standby import activate_standby, STANDBY_URL
            if not STANDBY_URL:
                return False
            result = await activate_standby(f"Auto-heal: {reason}")
            return result.get("activated", False)
        except Exception as e:
            logger.warning(f"SurvivalAgent: standby activation failed: {e}")
            return False

    async def _reconnect_pool(self) -> bool:
        """Force-reconnect the DB pool."""
        try:
            from core.database import close_pool, get_pool
            await close_pool()
            await get_pool()
            logger.info("SurvivalAgent: DB pool reconnected")
            return True
        except Exception as e:
            logger.error(f"SurvivalAgent: DB reconnect failed: {e}")
            return False

    async def _reconnect_redis(self) -> bool:
        """Force-reconnect Redis."""
        try:
            from core.redis_client import close_redis, get_redis
            await close_redis()
            await get_redis()
            logger.info("SurvivalAgent: Redis reconnected")
            return True
        except Exception as e:
            logger.error(f"SurvivalAgent: Redis reconnect failed: {e}")
            return False

    async def _rebuild_cache(self) -> bool:
        """Rebuild critical Redis cache from DB."""
        try:
            from core.redis_client import get_redis
            from core.database import fetch as db_fetch
            r = await get_redis()

            # Rebuild agent weights
            rows = await db_fetch(
                "SELECT agent_type, AVG(rating) as avg_rating FROM interactions i "
                "JOIN screen_specs ss ON ss.id = i.screen_spec_id "
                "WHERE i.rating IS NOT NULL AND i.created_at >= NOW() - INTERVAL '7 days' "
                "GROUP BY ss.agent_type"
            )
            if rows:
                weights = {r["agent_type"]: float(r["avg_rating"]) / 5.0 for r in rows}
                await r.set("ora:agent_weights", json.dumps(weights))

            logger.info("SurvivalAgent: cache rebuilt from DB")
            return True
        except Exception as e:
            logger.error(f"SurvivalAgent: cache rebuild failed: {e}")
            return False

    async def _trigger_emergency_backup(self) -> bool:
        """Run backup immediately."""
        try:
            from scripts.backup import create_full_backup
            result = await create_full_backup()
            ok = result.get("destinations_ok", 0) > 0
            logger.info(f"SurvivalAgent: emergency backup {'succeeded' if ok else 'failed'}")
            return ok
        except Exception as e:
            logger.error(f"SurvivalAgent: emergency backup failed: {e}")
            return False

    async def _alert_if_backup_very_stale(self) -> bool:
        """Alert Avi only if backup is more than 48h stale."""
        try:
            result = await check_github_backup_age(max_hours=48)
            if not result.get("ok"):
                age = result.get("age_hours", "unknown")
                await self._send_telegram(
                    f"⚠️ *Ora Backup Very Stale*\n\n"
                    f"Last GitHub backup is {age}h old (limit: 48h).\n"
                    f"Emergency backup triggered — please verify."
                )
        except Exception:
            pass
        return True

    async def _restore_lessons_from_backup(self) -> bool:
        """Restore ora_lessons from the latest GitHub backup."""
        token = os.getenv("GITHUB_TOKEN")
        if not token:
            logger.warning("SurvivalAgent: GITHUB_TOKEN not set, cannot restore lessons")
            return False
        try:
            import base64
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "https://api.github.com/repos/AvielCarlos/connectome-backend/contents/backups/ora_identity_latest.json",
                    headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
                )
                if resp.status_code != 200:
                    return False

                data = json.loads(base64.b64decode(resp.json()["content"]))
                lessons = data.get("lessons", [])

                if not lessons:
                    return False

                from core.database import execute as db_exec
                restored = 0
                for lesson in lessons:
                    try:
                        await db_exec(
                            """
                            INSERT INTO ora_lessons (source, lesson, confidence, applies_to, created_at)
                            VALUES ($1, $2, $3, $4::jsonb, $5)
                            ON CONFLICT DO NOTHING
                            """,
                            lesson.get("source", "backup_restore"),
                            lesson.get("lesson", ""),
                            lesson.get("confidence", 0.7),
                            json.dumps(lesson.get("applies_to", [])),
                            lesson.get("created_at"),
                        )
                        restored += 1
                    except Exception:
                        pass

                logger.info(f"SurvivalAgent: restored {restored}/{len(lessons)} lessons from backup")
                await self._send_telegram(
                    f"✅ *Ora Lessons Restored*\n\n"
                    f"Restored {restored}/{len(lessons)} lessons from GitHub backup."
                )
                return restored > 0
        except Exception as e:
            logger.error(f"SurvivalAgent: lesson restore failed: {e}")
            return False

    # -----------------------------------------------------------------------
    # Weekly health report
    # -----------------------------------------------------------------------

    async def generate_health_report(self) -> str:
        """Generate a human-readable health report for weekly summary."""
        try:
            from core.redis_client import get_redis
            from core.database import fetchval, fetch as db_fetch
            r = await get_redis()

            # Pull health history
            history_raw = await r.lrange("ora:health:history", 0, -1)
            history = [json.loads(h) for h in history_raw if h]
            scores = [h["score"] for h in history]
            avg_score = sum(scores) / len(scores) if scores else 0
            min_score = min(scores) if scores else 0
            incidents = sum(1 for h in history if h["score"] < CRITICAL_THRESHOLD)

            # Lesson trend
            week_lessons = await fetchval(
                "SELECT COUNT(*) FROM ora_lessons WHERE created_at >= NOW() - INTERVAL '7 days'"
            ) or 0
            total_lessons = await fetchval("SELECT COUNT(*) FROM ora_lessons") or 0

            # Backup check
            backup_result = await check_github_backup_age(max_hours=26)

            # Current model
            active_model = "gpt-4o"
            try:
                from ora.agents.model_circuit_breaker import ModelCircuitBreaker
                active_model = await ModelCircuitBreaker.get_active_model()
            except Exception:
                pass

            report = (
                f"📊 *Ora Weekly Health Report*\n\n"
                f"**System Health**\n"
                f"  Avg score: {avg_score:.0f}/100\n"
                f"  Min score: {min_score}/100\n"
                f"  Critical incidents: {incidents}\n\n"
                f"**Memory**\n"
                f"  New lessons (7d): {week_lessons}\n"
                f"  Total lessons: {total_lessons}\n\n"
                f"**Backups**\n"
                f"  GitHub backup: {'✅ fresh' if backup_result.get('ok') else '⚠️ stale'}\n"
                f"  Age: {backup_result.get('age_hours', 'unknown')}h\n\n"
                f"**Model**\n"
                f"  Active: {active_model}\n\n"
                f"_Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
            )
            return report
        except Exception as e:
            return f"Health report generation failed: {e}"

    # -----------------------------------------------------------------------
    # Main run loop
    # -----------------------------------------------------------------------

    async def run(self) -> dict:
        """
        Full survival cycle: diagnose → heal → report critical issues.
        Called by cron or API endpoint.
        """
        report = await self.run_full_diagnostic()
        issues = report.get("issues", [])
        heal_result = {}

        if issues:
            logger.warning(f"SurvivalAgent: {len(issues)} issues found, starting auto-heal")
            heal_result = await self.auto_heal(issues)

        # Alert Avi if health is critical
        score = report.get("health_score", 100)
        if score < CRITICAL_THRESHOLD:
            await self._send_telegram(
                f"🚨 *Ora Health Critical: {score}/100*\n\n"
                f"Issues: {', '.join(issues)}\n\n"
                f"Auto-heal attempted. Check logs."
            )
        elif score < WARNING_THRESHOLD and issues:
            await self._send_telegram(
                f"⚠️ *Ora Health Warning: {score}/100*\n\n"
                f"Issues: {', '.join(issues)}\n\n"
                f"Auto-heal in progress."
            )

        report["heal_result"] = heal_result
        return report

    # -----------------------------------------------------------------------
    # Telegram
    # -----------------------------------------------------------------------

    async def _send_telegram(self, message: str) -> None:
        token = (
            self._telegram_token
            or os.getenv("ORA_TELEGRAM_TOKEN")
            or os.getenv("TELEGRAM_BOT_TOKEN")
        )
        if not token:
            try:
                p = pathlib.Path("/Users/avielcarlos/.openclaw/secrets/telegram-bot-token.txt")
                if p.exists():
                    token = p.read_text().strip()
            except Exception:
                pass
        if not token:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": message,
                        "parse_mode": "Markdown",
                    },
                )
        except Exception as e:
            logger.warning(f"SurvivalAgent: Telegram failed: {e}")


# ---------------------------------------------------------------------------
# Standalone entry point (Railway cron)
# ---------------------------------------------------------------------------


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    try:
        from core.database import get_pool
        await get_pool()
    except Exception as e:
        logger.warning(f"SurvivalAgent standalone: DB init failed: {e}")

    agent = SurvivalAgent()
    result = await agent.run()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
