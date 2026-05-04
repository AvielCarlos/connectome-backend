"""
Aura Survival Agent — Comprehensive self-healing and health monitoring.

Runs all health checks concurrently, executes automated recovery actions,
generates weekly health reports, and never fails silently.

Health checks:
  api, database, redis, aura_brain, agent_registry, lesson_count,
  user_count, backup_freshness

Self-heal actions:
  reconnect pools, trigger redeployment, restore from backup, alert Avi

Run via Railway cron (hourly) or POST /api/aura/survival/run.
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
        url = "https://api.github.com/repos/AvielCarlos/connectome-backend/contents/backups/aura_identity_latest.json"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            )
            if resp.status_code == 404:
                # Backward compatibility with older backup jobs that only wrote
                # aura_identity_pack.json. New jobs write both files.
                resp = await client.get(
                    "https://api.github.com/repos/AvielCarlos/connectome-backend/contents/backups/aura_identity_pack.json",
                    headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
                )
            if resp.status_code != 200:
                return {"ok": False, "error": f"GitHub API {resp.status_code}"}

            import base64
            raw = base64.b64decode(resp.json()["content"])
            data = json.loads(raw)
            ts_str = data.get("collected_at") or data.get("exported_at") or data.get("timestamp", "")
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
    Aura's comprehensive self-healing engine.
    Runs all health checks concurrently, executes recovery actions,
    tracks failure counts in Redis.
    """

    def __init__(self):
        self._telegram_token: Optional[str] = None

    HEALTH_CHECKS = {
        "api": lambda: check_endpoint("/health", expected_status=200),
        "database": lambda: check_database(),
        "redis": lambda: check_redis(),
        "aura_brain": lambda: check_endpoint(
            "/api/aura/health/dashboard",
            expected_key="api",
            timeout=15,
        ),
        "agent_registry": lambda: check_redis_key("aura:agent_registry"),
        "lesson_count": lambda: check_db_query(
            "SELECT COUNT(*) FROM aura_lessons", min_value=1
        ),
        "user_count": lambda: check_db_query(
            "SELECT COUNT(*) FROM users", min_value=0
        ),
        "backup_freshness": lambda: check_github_backup_age(max_hours=26),
    }

    SELF_HEAL_ACTIONS = {
        # Routine recoveries stay quiet. Avi is alerted only by the escalation
        # path after repeated failed heal attempts, or for explicit data-risk
        # cases below that need human intervention.
        "api": ["trigger_railway_redeploy", "activate_standby_if_available"],
        "database": ["reconnect_pool"],
        "redis": ["reconnect_redis", "rebuild_cache_from_db"],
        "backup_freshness": ["trigger_emergency_backup", "alert_avi_if_very_stale"],
        "lesson_count": ["restore_lessons_from_backup", "alert_avi_immediately"],
        "aura_brain": ["trigger_railway_redeploy"],
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
            await r.lpush("aura:health:history", json.dumps(snapshot))
            await r.ltrim("aura:health:history", 0, 167)  # Keep 1 week of hourly checks
            await r.set("aura:health:latest", json.dumps(snapshot))
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

    async def auto_heal(self, issues: List[str], diagnostic_report: Optional[dict] = None) -> dict:
        """
        For each issue, execute corresponding heal actions.

        Every attempted action is stored as feedback so Aura can learn which
        interventions work for each failure mode, rank future actions, and distill
        durable survival lessons into `aura_lessons`.
        """
        healed = []
        escalated = []
        attempts = []
        diagnostic_report = diagnostic_report or {}

        for issue in issues:
            base_actions = self.SELF_HEAL_ACTIONS.get(issue, ["alert_avi"])
            actions = await self._rank_heal_actions(issue, base_actions)
            for action in actions:
                fail_key = f"aura:survival:heal_failures:{issue}:{action}"
                fail_count = 0
                try:
                    from core.redis_client import get_redis
                    r = await get_redis()
                    fail_count = int(await r.get(fail_key) or 0)
                    if fail_count >= 3:
                        logger.warning(
                            f"SurvivalAgent: {action} for {issue} has failed {fail_count}x, escalating"
                        )
                        escalated.append({"issue": issue, "action": action, "fail_count": fail_count})
                        attempts.append({"issue": issue, "action": action, "success": False, "escalated": True})
                        await self._record_heal_learning(
                            issue=issue,
                            action=action,
                            success=False,
                            failure_count=fail_count,
                            diagnostic_report=diagnostic_report,
                            outcome={"reason": "failure_threshold", "escalated": True},
                            escalated=True,
                            error="failure threshold reached before retry",
                        )
                        await self._send_telegram(
                            f"🆘 *Aura Heal Escalation*\n\n"
                            f"Issue: `{issue}`\n"
                            f"Action: `{action}`\n"
                            f"Failures: {fail_count}\n\n"
                            f"Auto-heal is not working. Manual intervention required."
                        )
                        continue
                except Exception:
                    pass

                started = time.time()
                success = await self._execute_action(action, issue)
                latency_ms = int((time.time() - started) * 1000)
                attempts.append({
                    "issue": issue,
                    "action": action,
                    "success": success,
                    "failure_count_before": fail_count,
                    "latency_ms": latency_ms,
                })
                await self._record_heal_learning(
                    issue=issue,
                    action=action,
                    success=success,
                    failure_count=fail_count,
                    diagnostic_report=diagnostic_report,
                    outcome={"latency_ms": latency_ms, "failure_count_before": fail_count},
                )

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

        return {"healed": healed, "escalated": escalated, "attempts": attempts}

    async def _rank_heal_actions(self, issue: str, base_actions: List[str]) -> List[str]:
        """Rank recovery actions using Aura's learned heal policy.

        Unknown actions keep their configured order. Learned successes move an
        action earlier; repeated failures/escalations move it later. Alert-only
        actions remain fallbacks unless they are the only available option.
        """
        try:
            from core.database import fetch
            rows = await fetch(
                """
                SELECT action, score, attempts, successes, failures, escalations
                FROM aura_heal_policies
                WHERE issue = $1 AND action = ANY($2::text[])
                """,
                issue,
                base_actions,
            )
            learned = {r["action"]: dict(r) for r in rows}
        except Exception as e:
            logger.debug(f"SurvivalAgent: heal policy lookup skipped: {e}")
            learned = {}

        def sort_key(item: tuple[int, str]) -> tuple[float, int]:
            idx, action = item
            row = learned.get(action)
            alert_penalty = 1.0 if action.startswith("alert_") and len(base_actions) > 1 else 0.0
            if not row:
                # Preserve baseline priority, but keep alerts as fallbacks.
                return (alert_penalty + idx / 100.0, idx)
            # Lower sort key is better. Convert learned score to priority while
            # still respecting baseline order enough to avoid wild thrashing.
            learned_priority = -float(row.get("score") or 0.0)
            return (alert_penalty + learned_priority + idx / 100.0, idx)

        ranked = [action for _, action in sorted(enumerate(base_actions), key=sort_key)]
        if ranked != base_actions:
            logger.info(f"SurvivalAgent: learned heal order for {issue}: {ranked}")
        return ranked

    async def _record_heal_learning(
        self,
        *,
        issue: str,
        action: str,
        success: bool,
        failure_count: int,
        diagnostic_report: dict,
        outcome: Optional[dict] = None,
        escalated: bool = False,
        error: Optional[str] = None,
    ) -> None:
        """Persist one healing attempt and update Aura's survival policy."""
        outcome = outcome or {}
        diagnostic = {
            "health_score": diagnostic_report.get("health_score"),
            "issues": diagnostic_report.get("issues", []),
            "check": (diagnostic_report.get("checks") or {}).get(issue, {}),
            "run_at": diagnostic_report.get("run_at"),
        }
        try:
            from core.database import execute

            await execute(
                """
                INSERT INTO aura_heal_events
                    (issue, action, success, failure_count, diagnostic, outcome, error, escalated)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8)
                """,
                issue,
                action,
                success,
                failure_count,
                json.dumps(diagnostic, default=str),
                json.dumps(outcome, default=str),
                error,
                escalated,
            )
            await execute(
                """
                INSERT INTO aura_heal_policies
                    (issue, action, attempts, successes, failures, escalations,
                     success_rate, score, last_success_at, last_failure_at, updated_at)
                VALUES (
                    $1, $2, 1,
                    CASE WHEN $3 THEN 1 ELSE 0 END,
                    CASE WHEN $3 THEN 0 ELSE 1 END,
                    CASE WHEN $4 THEN 1 ELSE 0 END,
                    CASE WHEN $3 THEN 1.0 ELSE 0.0 END,
                    CASE WHEN $3 THEN 1.0 ELSE -0.35 END - CASE WHEN $4 THEN 0.5 ELSE 0 END,
                    CASE WHEN $3 THEN NOW() ELSE NULL END,
                    CASE WHEN $3 THEN NULL ELSE NOW() END,
                    NOW()
                )
                ON CONFLICT (issue, action) DO UPDATE SET
                    attempts = aura_heal_policies.attempts + 1,
                    successes = aura_heal_policies.successes + CASE WHEN EXCLUDED.successes > 0 THEN 1 ELSE 0 END,
                    failures = aura_heal_policies.failures + CASE WHEN EXCLUDED.failures > 0 THEN 1 ELSE 0 END,
                    escalations = aura_heal_policies.escalations + CASE WHEN EXCLUDED.escalations > 0 THEN 1 ELSE 0 END,
                    success_rate = (
                        (aura_heal_policies.successes + CASE WHEN EXCLUDED.successes > 0 THEN 1 ELSE 0 END)::float /
                        GREATEST(aura_heal_policies.attempts + 1, 1)
                    ),
                    score = (
                        (aura_heal_policies.successes + CASE WHEN EXCLUDED.successes > 0 THEN 1 ELSE 0 END)::float /
                        GREATEST(aura_heal_policies.attempts + 1, 1)
                    )
                    - ((aura_heal_policies.failures + CASE WHEN EXCLUDED.failures > 0 THEN 1 ELSE 0 END)::float * 0.05)
                    - ((aura_heal_policies.escalations + CASE WHEN EXCLUDED.escalations > 0 THEN 1 ELSE 0 END)::float * 0.20),
                    last_success_at = CASE WHEN EXCLUDED.successes > 0 THEN NOW() ELSE aura_heal_policies.last_success_at END,
                    last_failure_at = CASE WHEN EXCLUDED.failures > 0 THEN NOW() ELSE aura_heal_policies.last_failure_at END,
                    updated_at = NOW()
                """,
                issue,
                action,
                success,
                escalated,
            )

            if success or escalated or failure_count > 0:
                lesson = (
                    f"Survival learning: for issue '{issue}', action '{action}' "
                    f"{'succeeded' if success else 'failed/escalated'} after "
                    f"{failure_count} prior failure(s). Prefer actions with higher "
                    f"aura_heal_policies.score for this issue."
                )
                await execute(
                    """
                    INSERT INTO aura_lessons (source, lesson, confidence, applies_to, created_at)
                    VALUES ($1, $2, $3, $4::jsonb, NOW())
                    """,
                    "survival_agent",
                    lesson,
                    0.82 if success else 0.68,
                    json.dumps({"issue": issue, "action": action, "survival_learning": True}),
                )
        except Exception as e:
            # Learning must never break healing.
            logger.debug(f"SurvivalAgent: heal learning persistence skipped: {e}")

        try:
            from core.redis_client import get_redis
            r = await get_redis()
            await r.hincrby(f"aura:survival:learn:{issue}:{action}", "attempts", 1)
            await r.hincrby(f"aura:survival:learn:{issue}:{action}", "successes" if success else "failures", 1)
            if escalated:
                await r.hincrby(f"aura:survival:learn:{issue}:{action}", "escalations", 1)
            await r.expire(f"aura:survival:learn:{issue}:{action}", 30 * 24 * 3600)
        except Exception:
            pass

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
                    f"🔧 *Aura Self-Heal in Progress*\n\n"
                    f"Issue: `{issue}`\n"
                    f"Action: `{action}`\n"
                    f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                )
                return True
            elif action == "alert_avi_if_very_stale":
                return await self._alert_if_backup_very_stale()
            elif action == "alert_avi_immediately":
                await self._send_telegram(
                    f"🚨 *URGENT — Aura Data Loss Risk*\n\n"
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
                await r.set("aura:agent_weights", json.dumps(weights))

            logger.info("SurvivalAgent: cache rebuilt from DB")
            return True
        except Exception as e:
            logger.error(f"SurvivalAgent: cache rebuild failed: {e}")
            return False

    async def _trigger_emergency_backup(self) -> bool:
        """Run backup immediately and verify it reached GitHub.

        `scripts.backup.create_full_backup()` currently returns the backup directory
        path, not a status dict. Older auto-heal code treated the return value as a
        dict and therefore marked successful backups as failed. Since
        `backup_freshness` checks the GitHub identity backup, this heal action only
        succeeds when the backup manifest confirms `github_committed`.
        """
        try:
            from scripts.backup import create_full_backup

            result = await create_full_backup(identity_only=True)
            ok = False

            if isinstance(result, dict):
                ok = bool(result.get("github_committed") or result.get("destinations_ok", 0) > 0)
            elif isinstance(result, str):
                manifest_path = os.path.join(result, "manifest.json")
                if os.path.exists(manifest_path):
                    with open(manifest_path) as f:
                        manifest = json.load(f)
                    ok = bool(manifest.get("github_committed"))
                else:
                    logger.warning(f"SurvivalAgent: backup manifest missing: {manifest_path}")

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
                    f"⚠️ *Aura Backup Very Stale*\n\n"
                    f"Last GitHub backup is {age}h old (limit: 48h).\n"
                    f"Emergency backup triggered — please verify."
                )
        except Exception:
            pass
        return True

    async def _restore_lessons_from_backup(self) -> bool:
        """Restore aura_lessons from the latest GitHub backup."""
        token = os.getenv("GITHUB_TOKEN")
        if not token:
            logger.warning("SurvivalAgent: GITHUB_TOKEN not set, cannot restore lessons")
            return False
        try:
            import base64
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "https://api.github.com/repos/AvielCarlos/connectome-backend/contents/backups/aura_identity_latest.json",
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
                            INSERT INTO aura_lessons (source, lesson, confidence, applies_to, created_at)
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
                    f"✅ *Aura Lessons Restored*\n\n"
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
            history_raw = await r.lrange("aura:health:history", 0, -1)
            history = [json.loads(h) for h in history_raw if h]
            scores = [h["score"] for h in history]
            avg_score = sum(scores) / len(scores) if scores else 0
            min_score = min(scores) if scores else 0
            incidents = sum(1 for h in history if h["score"] < CRITICAL_THRESHOLD)

            # Lesson trend
            week_lessons = await fetchval(
                "SELECT COUNT(*) FROM aura_lessons WHERE created_at >= NOW() - INTERVAL '7 days'"
            ) or 0
            total_lessons = await fetchval("SELECT COUNT(*) FROM aura_lessons") or 0

            # Backup check
            backup_result = await check_github_backup_age(max_hours=26)

            # Current model
            active_model = "gpt-4o"
            try:
                from aura.agents.model_circuit_breaker import ModelCircuitBreaker
                active_model = await ModelCircuitBreaker.get_active_model()
            except Exception:
                pass

            report = (
                f"📊 *Aura Weekly Health Report*\n\n"
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
            heal_result = await self.auto_heal(issues, diagnostic_report=report)

        # Alert Avi if health is critical
        score = report.get("health_score", 100)
        if score < CRITICAL_THRESHOLD:
            await self._send_telegram(
                f"🚨 *Aura Health Critical: {score}/100*\n\n"
                f"Issues: {', '.join(issues)}\n\n"
                f"Auto-heal attempted. Check logs."
            )
        elif score < WARNING_THRESHOLD and issues:
            await self._send_telegram(
                f"⚠️ *Aura Health Warning: {score}/100*\n\n"
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
