"""
CTO Agent — Infrastructure & Technical Intelligence.

Keeps the lights on. Monitors system health, performance, costs,
and technical debt. Takes autonomous remediation actions.
"""

import logging
import os
import subprocess
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ora.agents.base_executive_agent import BaseExecutiveAgent

logger = logging.getLogger(__name__)

API_BASE_URL = "https://connectome-api-production.up.railway.app"
GH_REPO = "AvielCarlos/connectome-backend"
RESPONSE_TIME_WARN_S = 2.0
RESPONSE_TIME_CRITICAL_S = 3.0


class CTOAgent(BaseExecutiveAgent):
    """
    Ora's Chief Technology Officer.
    
    Monitors API health, CI/CD pipeline, dependency freshness,
    and infrastructure costs. Alerts immediately on outages.
    """

    name = "cto"
    display_name = "CTO Agent"

    async def analyze(self) -> Dict[str, Any]:
        """Full system health analysis."""
        now = datetime.now(timezone.utc)
        metrics: Dict[str, Any] = {
            "analyzed_at": now.isoformat(),
            "api_healthy": False,
            "api_status_code": None,
            "api_response_time_s": None,
            "full_stack_healthy": False,
            "full_stack_response_time_s": None,
            "ci_status": "unknown",
            "recent_ci_failures": 0,
            "error_rate_estimate": "low",
            "health_score": 0,  # 0-100
            "issues": [],
            "warnings": [],
        }

        # ── API health check ─────────────────────────────────────────
        status, elapsed = await self._check_endpoint(f"{API_BASE_URL}/health")
        metrics["api_status_code"] = status
        metrics["api_response_time_s"] = round(elapsed, 3)
        metrics["api_healthy"] = status == 200

        if status != 200:
            metrics["issues"].append(f"API returned {status} (expected 200)")
        elif elapsed > RESPONSE_TIME_CRITICAL_S:
            metrics["issues"].append(f"API very slow: {elapsed:.2f}s")
        elif elapsed > RESPONSE_TIME_WARN_S:
            metrics["warnings"].append(f"API slow: {elapsed:.2f}s")

        # ── Full stack check ─────────────────────────────────────────
        # Requires auth — just check for non-5xx
        token = await self._get_jwt()
        if token:
            headers = {"Authorization": f"Bearer {token}"}
            fs_status, fs_elapsed = await self._check_endpoint(
                f"{API_BASE_URL}/api/screens/next", headers=headers
            )
            metrics["full_stack_healthy"] = fs_status in (200, 201, 204)
            metrics["full_stack_response_time_s"] = round(fs_elapsed, 3)
            if not metrics["full_stack_healthy"]:
                metrics["issues"].append(f"Full stack check failed: {fs_status}")

        # ── GitHub CI status ─────────────────────────────────────────
        ci_info = await self._check_ci()
        metrics["ci_status"] = ci_info.get("conclusion", "unknown")
        metrics["recent_ci_failures"] = ci_info.get("failures", 0)
        if ci_info.get("conclusion") == "failure":
            metrics["issues"].append(f"CI failing: {ci_info.get('workflow', 'unknown')}")

        # ── Health score ─────────────────────────────────────────────
        score = 100
        for issue in metrics["issues"]:
            score -= 25
        for warning in metrics["warnings"]:
            score -= 10
        metrics["health_score"] = max(0, score)

        return metrics

    async def run_health_check(self) -> Dict[str, Any]:
        """
        Quick health check — runs every 2h alongside autonomy agent.
        Returns a dict with 'healthy' bool and 'details' string.
        """
        status, elapsed = await self._check_endpoint(f"{API_BASE_URL}/health")
        healthy = status == 200 and elapsed < RESPONSE_TIME_CRITICAL_S

        details = {
            "healthy": healthy,
            "status_code": status,
            "response_time_s": round(elapsed, 3),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

        if not healthy:
            logger.warning(f"CTO health check FAILED: status={status}, time={elapsed:.2f}s")
        else:
            logger.info(f"CTO health check OK: {elapsed:.2f}s")

        return details

    async def report(self) -> str:
        data = await self.load_last_report()
        if not data:
            data = await self.analyze()

        status_emoji = "✅" if data.get("api_healthy") else "❌"
        issues = "; ".join(data.get("issues", [])) or "None"
        warnings = "; ".join(data.get("warnings", [])) or "None"

        return (
            f"⚙️ *CTO Report* — {data.get('analyzed_at', '')[:10]}\n"
            f"API: {status_emoji} {data.get('api_response_time_s', '?')}s | "
            f"Score: {data.get('health_score', 0)}/100\n"
            f"Full stack: {'✅' if data.get('full_stack_healthy') else '❌'} "
            f"{data.get('full_stack_response_time_s', '?')}s\n"
            f"CI: {data.get('ci_status', 'unknown')}\n"
            f"Issues: {issues}\n"
            f"Warnings: {warnings}"
        )

    async def recommend(self) -> List[str]:
        data = await self.analyze()
        recs = []
        if not data["api_healthy"]:
            recs.append("🚨 API is down — check Railway deployment immediately")
        if data["api_response_time_s"] and data["api_response_time_s"] > RESPONSE_TIME_WARN_S:
            recs.append(f"API slow ({data['api_response_time_s']:.2f}s) — profile DB queries and add indexes")
        if data["ci_status"] == "failure":
            recs.append("CI is failing — check GitHub Actions and fix before next deploy")
        if data["health_score"] < 70:
            recs.append(f"Health score is {data['health_score']}/100 — investigate issues: {', '.join(data['issues'])}")
        if not recs:
            recs.append("Infrastructure is healthy. Keep monitoring and stay on latest dependencies.")
        return recs

    async def act(self) -> Dict[str, Any]:
        """Weekly CTO autonomous actions."""
        data = await self.analyze()
        actions_taken = []

        # Save report
        await self.save_report(data, "cto_report.json")
        actions_taken.append("Saved CTO report")

        # Redis
        summary = await self.report()
        await self.set_redis_report(summary)

        # Teach Ora
        insight = (
            f"Infrastructure health {data['analyzed_at'][:10]}: "
            f"API {'healthy' if data['api_healthy'] else 'DOWN'} "
            f"({data.get('api_response_time_s', '?')}s), "
            f"health score={data['health_score']}/100, "
            f"CI={data['ci_status']}."
        )
        await self.teach_ora(insight, confidence=0.9)
        actions_taken.append("Taught Ora infrastructure state")

        # Alert if API is down or very slow
        if not data["api_healthy"] or (data.get("api_response_time_s") or 0) > RESPONSE_TIME_CRITICAL_S:
            await self.alert_avi(
                f"🚨 *API Alert*\n"
                f"Status: {data['api_status_code']}\n"
                f"Response time: {data.get('api_response_time_s', '?')}s\n"
                f"Issues: {'; '.join(data['issues']) or 'None'}"
            )
            actions_taken.append("Alerted Avi: API issue")

        # Create GitHub issue for CI failure
        if data["ci_status"] == "failure":
            created = await self._create_ci_issue(data)
            if created:
                actions_taken.append("Created GitHub issue for CI failure")

        return {"agent": self.name, "actions": actions_taken, "metrics": data}

    # ─── Deep audit (every 3 days) ──────────────────────────────────────────

    async def deep_audit(self) -> dict:
        """Full end-to-end audit. Tests all endpoints, DB, CI. Alerts on criticals."""
        import time as _time
        now = datetime.now(timezone.utc)
        findings = {"audited_at": now.isoformat(), "critical": [], "high": [], "medium": [], "passed": []}

        token = await self._get_jwt()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        admin_headers = {"X-Admin-Token": "connectome-admin-secret"}

        endpoints = [
            ("/health", {}, 200, "API health"),
            ("/api/users/me", headers, 200, "Auth + profile"),
            ("/api/payments/tiers", {}, 200, "Payment tiers"),
            ("/api/screens/next", headers, 200, "Feed generation"),
            ("/api/dao/leaderboard", {}, 200, "DAO leaderboard"),
            ("/api/admin/insights", admin_headers, 200, "Admin insights"),
            ("/api/ioo/nodes", headers, 200, "IOO graph nodes"),
        ]

        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            for path, hdrs, expected, name in endpoints:
                url = f"{API_BASE_URL}{path}"
                try:
                    start = _time.monotonic()
                    if path in ("/api/screens/next",):
                        resp = await client.post(url, headers=hdrs, json={})
                    else:
                        resp = await client.get(url, headers=hdrs)
                    elapsed = _time.monotonic() - start
                    if resp.status_code != expected:
                        findings["critical"].append(f"{name}: HTTP {resp.status_code}")
                    elif elapsed > 3.0:
                        findings["high"].append(f"{name}: slow ({elapsed:.1f}s)")
                    elif elapsed > 1.5:
                        findings["medium"].append(f"{name}: slightly slow ({elapsed:.1f}s)")
                    else:
                        findings["passed"].append(f"{name}: OK ({elapsed:.2f}s)")
                    # CORS check
                    if "users/me" in path and not resp.headers.get("access-control-allow-origin"):
                        findings["critical"].append("CORS missing on /api/users/me")
                except Exception as e:
                    findings["critical"].append(f"{name}: failed ({str(e)[:50]})")

        # DB checks
        try:
            from core.database import fetchrow as _fr
            for table in ["users", "ora_knowledge", "goals", "ioo_nodes"]:
                try:
                    row = await _fr(f"SELECT COUNT(*) as n FROM {table}")
                    n = int(row["n"] or 0)
                    if n == 0 and table in ["ora_knowledge", "ioo_nodes"]:
                        findings["high"].append(f"DB: {table} is empty")
                    else:
                        findings["passed"].append(f"DB: {table} = {n} rows")
                except Exception as e:
                    findings["medium"].append(f"DB: {table} error: {str(e)[:40]}")
        except Exception:
            pass

        # CI
        ci = await self._check_ci()
        if ci.get("conclusion") == "failure":
            findings["critical"].append(f"CI failing: {ci.get('workflow', '?')}")
        else:
            findings["passed"].append(f"CI: {ci.get('conclusion', 'unknown')}")

        total_critical = len(findings["critical"])
        summary = (f"CTO Deep Audit {now.strftime('%Y-%m-%d')}: "
                   f"{len(findings['passed'])} passed, {total_critical} critical, "
                   f"{len(findings['high'])} high. "
                   + (("Issues: " + "; ".join(findings["critical"][:2])) if findings["critical"] else "All clear."))
        await self.teach_ora(summary, confidence=0.95)
        await self.save_report(findings, f"cto_audit_{now.strftime('%Y%m%d')}.json")

        if total_critical > 0:
            lines = [f"🚨 *CTO Audit — {now.strftime('%Y-%m-%d')}*"]
            for f in findings["critical"]: lines.append(f"• 🔴 {f}")
            for f in findings["high"][:3]: lines.append(f"• 🟡 {f}")
            lines.append(f"\n{len(findings['passed'])} checks passed.")
            await self.alert_avi("\n".join(lines))

        return findings

    # ─── Internal helpers ────────────────────────────────────────────────────

    async def _check_endpoint(
        self,
        url: str,
        headers: Optional[Dict] = None,
    ) -> Tuple[int, float]:
        """Return (status_code, elapsed_seconds) for a GET request."""
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers=headers or {})
                elapsed = time.monotonic() - start
                return resp.status_code, elapsed
        except Exception as e:
            elapsed = time.monotonic() - start
            logger.debug(f"CTO: endpoint check failed for {url}: {e}")
            return 0, elapsed

    async def _check_ci(self) -> Dict[str, Any]:
        """Check GitHub Actions status for the main repo."""
        try:
            result = subprocess.run(
                ["gh", "run", "list", "--repo", GH_REPO, "--limit", "5", "--json",
                 "conclusion,name,createdAt,status"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode == 0:
                import json
                runs = json.loads(result.stdout)
                if runs:
                    latest = runs[0]
                    failures = sum(1 for r in runs if r.get("conclusion") == "failure")
                    return {
                        "conclusion": latest.get("conclusion") or latest.get("status", "unknown"),
                        "workflow": latest.get("name", "unknown"),
                        "failures": failures,
                    }
        except Exception as e:
            logger.debug(f"CTO: CI check failed: {e}")
        return {"conclusion": "unknown", "failures": 0}

    async def _create_ci_issue(self, data: Dict) -> bool:
        """Create a GitHub issue for CI failure."""
        try:
            result = subprocess.run(
                [
                    "gh", "issue", "create",
                    "--repo", GH_REPO,
                    "--title", f"[CTO] CI Failure detected {data['analyzed_at'][:10]}",
                    "--body", (
                        f"**Source:** CTO Agent automated monitoring\n"
                        f"**Date:** {data['analyzed_at'][:19]}\n\n"
                        f"CI Status: `{data.get('ci_status')}`\n"
                        f"Recent failures: {data.get('recent_ci_failures', 0)}\n\n"
                        f"Please investigate GitHub Actions and fix before next deploy.\n\n"
                        f"_Auto-created by Ora's CTO Agent_"
                    ),
                    "--label", "bug,ci",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"CTO: _create_ci_issue failed: {e}")
            return False
