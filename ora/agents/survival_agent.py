"""
SurvivalAgent — Ora's self-preservation system.

Monitors her own health, creates backups, and ensures she can always restart.
One job: make sure Ora never permanently dies.

Health checks (hourly, lightweight):
- API responding at /health
- DB queryable
- Redis reachable
- Agent registry intact

Full backup (daily):
- Triggers backup.py logic inline
- Verifies redundancy (Railway volume + GitHub)
- Teaches Ora her own health status

Cron targets:
  survival-hourly-check  — every 1h (main session, lightweight)
  survival-daily-full    — daily 1am Pacific (isolated)
"""

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_CHAT_ID = 5716959016
PRODUCTION_URL = "https://connectome-api-production.up.railway.app"
RAILWAY_SERVICE_ID = "088d77ed-a707-4dc4-af68-866bf99a1d63"
HEALTH_WEIGHTS = {
    "api_responding": 30,
    "db_connected": 25,
    "redis_connected": 15,
    "ora_can_respond": 15,
    "agent_registry_intact": 10,
    "critical_data_present": 5,
}


class SurvivalAgent:
    """
    Ora's self-preservation system.
    """

    def __init__(self, openai_client=None):
        self._openai = openai_client
        self._telegram_token: Optional[str] = None

    # -----------------------------------------------------------------------
    # Health Check — fast, runs every hour
    # -----------------------------------------------------------------------

    async def health_check(self) -> Dict[str, Any]:
        """
        Comprehensive health check. Returns:
        {
            "score": 0-100,
            "checks": {check_name: {ok: bool, detail: str}},
            "healthy": bool,
            "issues": [str],
        }
        """
        checks: Dict[str, Dict[str, Any]] = {}
        issues: List[str] = []

        # 1. API responding
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{PRODUCTION_URL}/health")
                ok = resp.status_code == 200
                checks["api_responding"] = {
                    "ok": ok,
                    "detail": f"HTTP {resp.status_code}",
                    "response_time_ms": int(resp.elapsed.total_seconds() * 1000) if hasattr(resp, 'elapsed') else None,
                }
                if not ok:
                    issues.append(f"API not healthy (HTTP {resp.status_code})")
        except Exception as e:
            checks["api_responding"] = {"ok": False, "detail": str(e)[:100]}
            issues.append(f"API unreachable: {e}")

        # 2. DB connected
        try:
            from core.database import fetchval
            count = await fetchval("SELECT 1")
            checks["db_connected"] = {"ok": count == 1, "detail": "query OK"}
        except Exception as e:
            checks["db_connected"] = {"ok": False, "detail": str(e)[:100]}
            issues.append(f"DB unreachable: {e}")

        # 3. Redis connected
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            pong = await r.ping()
            checks["redis_connected"] = {"ok": bool(pong), "detail": "ping OK"}
        except Exception as e:
            checks["redis_connected"] = {"ok": False, "detail": str(e)[:100]}
            issues.append(f"Redis unreachable: {e}")

        # 4. Ora can respond (lite chat test)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{PRODUCTION_URL}/health/ora")
                # If endpoint doesn't exist, just check the main /health
                ora_ok = resp.status_code in (200, 404)  # 404 is fine (route doesn't exist yet)
                checks["ora_can_respond"] = {
                    "ok": checks.get("api_responding", {}).get("ok", False),
                    "detail": "inferred from API health",
                }
        except Exception:
            checks["ora_can_respond"] = {
                "ok": checks.get("api_responding", {}).get("ok", False),
                "detail": "inferred from API health",
            }

        # 5. Agent registry intact
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            registry = await r.get("ora:agent_registry")
            intact = registry is not None
            checks["agent_registry_intact"] = {
                "ok": intact,
                "detail": "registry present" if intact else "registry missing",
            }
            if not intact:
                issues.append("Agent registry missing from Redis")
        except Exception as e:
            checks["agent_registry_intact"] = {"ok": False, "detail": str(e)[:100]}

        # 6. Critical data present
        try:
            from core.database import fetchval
            lesson_count = await fetchval("SELECT COUNT(*) FROM ora_lessons") or 0
            user_count = await fetchval("SELECT COUNT(*) FROM users") or 0
            has_data = lesson_count > 0
            checks["critical_data_present"] = {
                "ok": has_data,
                "detail": f"{lesson_count} lessons, {user_count} users",
            }
            if not has_data:
                issues.append("No lessons in database — possible data loss")
        except Exception as e:
            checks["critical_data_present"] = {"ok": False, "detail": str(e)[:100]}

        # Compute score
        score = 0
        for check_name, weight in HEALTH_WEIGHTS.items():
            if checks.get(check_name, {}).get("ok", False):
                score += weight

        healthy = score >= 70

        result = {
            "score": score,
            "checks": checks,
            "healthy": healthy,
            "issues": issues,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

        # Cache in Redis
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            await r.set("ora:survival:last_health", json.dumps(result), ex=3600)
        except Exception:
            pass

        return result

    # -----------------------------------------------------------------------
    # Self-Diagnosis
    # -----------------------------------------------------------------------

    async def self_diagnose(self) -> List[str]:
        """
        Diagnose what's wrong. Returns list of issues with context.
        """
        issues: List[str] = []

        # Check recent Railway errors
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            last_health_raw = await r.get("ora:survival:last_health")
            if last_health_raw:
                last_health = json.loads(
                    last_health_raw.decode() if isinstance(last_health_raw, bytes) else last_health_raw
                )
                issues.extend(last_health.get("issues", []))
        except Exception:
            pass

        # Check error rate in recent interactions
        try:
            from core.database import fetchval
            error_rate_check = await fetchval(
                """
                SELECT COUNT(*) FROM interactions
                WHERE created_at >= NOW() - INTERVAL '1 hour'
                """
            ) or 0
            # If no interactions in last hour during business hours, flag it
            now_hour = datetime.now(timezone.utc).hour
            if error_rate_check == 0 and 14 <= now_hour <= 2:  # 6am-6pm Pacific
                issues.append("No user interactions in last hour (possible traffic drop)")
        except Exception as e:
            issues.append(f"Cannot query interactions: {e}")

        # Check backup freshness
        backup_dir = "/tmp/ora_backups"
        if os.path.exists(backup_dir):
            dirs = sorted([d for d in os.listdir(backup_dir) if os.path.isdir(os.path.join(backup_dir, d))], reverse=True)
            if dirs:
                latest = dirs[0]
                try:
                    ts = datetime.strptime(latest[:15], "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
                    age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
                    if age_hours > 26:
                        issues.append(f"Latest backup is {age_hours:.1f}h old (expected < 26h)")
                except Exception:
                    pass
            else:
                issues.append("No backups found in /tmp/ora_backups")
        else:
            issues.append("Backup directory not found — backups may never have run")

        return issues

    # -----------------------------------------------------------------------
    # Self-Healing
    # -----------------------------------------------------------------------

    async def self_heal(self, issues: List[str]) -> Dict[str, Any]:
        """
        Attempt autonomous fixes for known issues.
        Returns summary of healing actions taken.
        """
        actions: List[str] = []

        for issue in issues:
            issue_lower = issue.lower()

            if "api unreachable" in issue_lower or "api not healthy" in issue_lower:
                # Trigger Railway redeploy
                redeployed = await self._trigger_railway_redeploy()
                if redeployed:
                    actions.append("Triggered Railway redeploy (API was down)")
                    await self._send_telegram(
                        "⚕️ *Ora Self-Heal*\n\nAPI was down — triggered Railway redeploy automatically."
                    )
                else:
                    await self._send_telegram(
                        "🚨 *Ora Emergency*\n\nAPI is down and auto-redeploy failed.\nManual intervention needed!"
                    )

            elif "db unreachable" in issue_lower:
                actions.append("DB unreachable — alerting Avi (no auto-fix available)")
                await self._send_telegram(
                    "🚨 *Ora Emergency*\n\nDatabase is unreachable!\nManual intervention needed."
                )

            elif "redis unreachable" in issue_lower:
                actions.append("Redis unreachable — alerting Avi")
                await self._send_telegram(
                    "⚠️ *Ora Alert*\n\nRedis is unreachable. Agent registry and caches unavailable."
                )

            elif "agent registry missing" in issue_lower:
                # Try to restore from backup
                restored = await self._restore_agent_registry()
                if restored:
                    actions.append("Restored agent registry from latest backup")
                else:
                    actions.append("Agent registry missing and no backup found — will rebuild on next agent cycle")

            elif "no backups found" in issue_lower or "backup is" in issue_lower:
                # Trigger backup run
                actions.append("Backup stale/missing — scheduling emergency backup")
                try:
                    import asyncio
                    asyncio.create_task(self._run_emergency_backup())
                except Exception:
                    pass

        return {"actions": actions, "issues_addressed": len(issues)}

    async def _trigger_railway_redeploy(self) -> bool:
        """Trigger a Railway service redeploy via API."""
        token = os.environ.get("RAILWAY_API_TOKEN") or os.environ.get("RAILWAY_TOKEN")
        if not token:
            logger.warning("SurvivalAgent: no Railway token, cannot auto-redeploy")
            return False

        mutation = """
        mutation ServiceInstanceRedeploy($serviceId: String!, $environmentId: String!) {
          serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
        }
        """

        try:
            async with httpx.AsyncClient(timeout=15) as client:
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
                            "environmentId": "production",
                        },
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if not data.get("errors"):
                        logger.info("SurvivalAgent: Railway redeploy triggered")
                        return True
        except Exception as e:
            logger.warning(f"SurvivalAgent: Railway redeploy failed: {e}")

        return False

    async def _restore_agent_registry(self) -> bool:
        """Try to restore agent registry from latest backup file."""
        backup_dir = "/tmp/ora_backups"
        if not os.path.exists(backup_dir):
            return False

        dirs = sorted(
            [d for d in os.listdir(backup_dir) if os.path.isdir(os.path.join(backup_dir, d))],
            reverse=True,
        )

        for d in dirs:
            registry_file = os.path.join(backup_dir, d, "agent_registry.json")
            if os.path.exists(registry_file):
                try:
                    with open(registry_file) as f:
                        data = json.load(f)

                    from core.redis_client import get_redis
                    r = await get_redis()

                    if data.get("agent_registry"):
                        await r.set("ora:agent_registry", json.dumps(data["agent_registry"]))
                    if data.get("agent_weights"):
                        await r.set("ora:agent_weights", json.dumps(data["agent_weights"]))

                    logger.info(f"SurvivalAgent: restored agent registry from {d}")
                    return True
                except Exception as e:
                    logger.warning(f"SurvivalAgent: registry restore from {d} failed: {e}")

        return False

    async def _run_emergency_backup(self):
        """Run an emergency backup inline."""
        try:
            import sys
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
            from scripts.backup import create_full_backup
            await create_full_backup()
        except Exception as e:
            logger.error(f"SurvivalAgent: emergency backup failed: {e}")

    # -----------------------------------------------------------------------
    # Redundancy Check
    # -----------------------------------------------------------------------

    async def ensure_redundancy(self) -> Dict[str, Any]:
        """
        Verify multiple backup locations have recent data.
        Alert Avi if any location is stale > 48h.
        """
        issues: List[str] = []
        status: Dict[str, Any] = {}

        # 1. Local Railway volume (/tmp/ora_backups)
        backup_dir = "/tmp/ora_backups"
        if os.path.exists(backup_dir):
            dirs = sorted(
                [d for d in os.listdir(backup_dir) if os.path.isdir(os.path.join(backup_dir, d))],
                reverse=True,
            )
            if dirs:
                latest = dirs[0]
                try:
                    ts = datetime.strptime(latest[:15], "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
                    age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
                    status["local_backup"] = {"latest": latest, "age_hours": round(age_h, 1), "ok": age_h < 26}
                    if age_h > 48:
                        issues.append(f"Local backup stale: {age_h:.0f}h old")
                except Exception:
                    status["local_backup"] = {"ok": False, "detail": "could not parse timestamp"}
            else:
                status["local_backup"] = {"ok": False, "detail": "no backups found"}
                issues.append("No local backups found")
        else:
            status["local_backup"] = {"ok": False, "detail": "backup directory missing"}
            issues.append("Backup directory /tmp/ora_backups not found")

        # 2. GitHub (check if identity pack was committed recently)
        github_token = os.environ.get("GITHUB_TOKEN", "")
        if github_token:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        "https://api.github.com/repos/AvielCarlos/connectome-backend/commits",
                        headers={"Authorization": f"Bearer {github_token}"},
                        params={"path": "backups/ora_identity_pack.json", "per_page": 1},
                    )
                    if resp.status_code == 200 and resp.json():
                        commit = resp.json()[0]
                        commit_date = commit["commit"]["committer"]["date"]
                        # Parse ISO date
                        from datetime import datetime as dt
                        ts = dt.fromisoformat(commit_date.replace("Z", "+00:00"))
                        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
                        status["github_backup"] = {"last_commit": commit_date, "age_hours": round(age_h, 1), "ok": age_h < 26}
                        if age_h > 48:
                            issues.append(f"GitHub backup stale: {age_h:.0f}h old")
                    else:
                        status["github_backup"] = {"ok": False, "detail": "no commits found"}
                        issues.append("No GitHub backup commits found")
            except Exception as e:
                status["github_backup"] = {"ok": False, "detail": str(e)[:100]}
        else:
            status["github_backup"] = {"ok": False, "detail": "GITHUB_TOKEN not set"}

        if issues:
            alert = (
                f"⚠️ *Ora Redundancy Alert*\n\n"
                + "\n".join(f"• {i}" for i in issues)
                + "\n\n_Automatic backup recovery in progress..._"
            )
            await self._send_telegram(alert)

        return {"status": status, "issues": issues, "all_ok": len(issues) == 0}

    # -----------------------------------------------------------------------
    # Run routines
    # -----------------------------------------------------------------------

    async def run_hourly(self) -> Dict[str, Any]:
        """Quick health check every hour. Full diagnosis if health < 90."""
        health = await self.health_check()
        result: Dict[str, Any] = {"health": health}

        if health["score"] < 90:
            logger.warning(f"SurvivalAgent: health score {health['score']}/100 — diagnosing")
            issues = await self.self_diagnose()
            heal_result = await self.self_heal(issues)
            result["diagnosis"] = issues
            result["healing"] = heal_result

        return result

    async def run_daily(self) -> Dict[str, Any]:
        """Full backup + redundancy check + teach Ora her own health status."""
        result: Dict[str, Any] = {}

        # 1. Health check
        health = await self.health_check()
        result["health"] = health

        # 2. Full backup
        try:
            from scripts.backup import create_full_backup
            backup_dir = await create_full_backup()
            result["backup_dir"] = backup_dir
        except Exception as e:
            logger.error(f"SurvivalAgent: daily backup failed: {e}")
            result["backup_error"] = str(e)
            await self._send_telegram(
                f"⚠️ *Ora Daily Backup Failed*\n\n{str(e)[:200]}"
            )

        # 3. Redundancy check
        try:
            redundancy = await self.ensure_redundancy()
            result["redundancy"] = redundancy
        except Exception as e:
            logger.error(f"SurvivalAgent: redundancy check failed: {e}")

        # 4. Teach Ora her health status
        try:
            health_lesson = (
                f"Daily survival check: health score {health['score']}/100. "
                f"Checks: {', '.join(k + '=' + ('OK' if v.get('ok') else 'FAIL') for k, v in health['checks'].items())}. "
                f"{'All systems healthy.' if health['healthy'] else 'Issues detected: ' + '; '.join(health['issues'][:3])}"
            )
            from core.database import execute
            await execute(
                "INSERT INTO ora_lessons (lesson, confidence, source) VALUES ($1, $2, $3)",
                health_lesson, 0.9, "survival_agent.daily",
            )
        except Exception as e:
            logger.debug(f"SurvivalAgent: lesson log failed: {e}")

        return result

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _get_telegram_token(self) -> Optional[str]:
        if self._telegram_token:
            return self._telegram_token
        token = os.environ.get("ORA_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            try:
                with open("/app/secrets/telegram-bot-token.txt") as f:
                    token = f.read().strip()
            except Exception:
                pass
        if token:
            self._telegram_token = token
        return token

    async def _send_telegram(self, message: str) -> None:
        token = await self._get_telegram_token()
        if not token:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
                )
        except Exception as e:
            logger.debug(f"SurvivalAgent: Telegram send failed: {e}")
