"""
COO Agent — Operations Intelligence.

Runs the day-to-day. Ensures all autonomous systems are working correctly.
Identifies waste and inefficiency. Keeps Ora's cron jobs healthy.
"""

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from ora.agents.base_executive_agent import API_BASE, BaseExecutiveAgent

logger = logging.getLogger(__name__)

# Keep in sync with the Executive API. CGO is included because it is part of the
# live council and writes the same Redis/report heartbeat as the other agents.
AGENTS = ["cfo", "cgo", "cmo", "cpo", "cto", "coo", "community", "strategy"]
REPORT_STALE_HOURS = 24
REPORT_MISSING_ALERT_HOURS = 72


class COOAgent(BaseExecutiveAgent):
    """
    Ora's Chief Operating Officer.

    Reviews all cron jobs, checks agent health, finds inefficiencies,
    and keeps the autonomous machine running smoothly.
    """

    name = "coo"
    display_name = "COO Agent"
    domain = "ops"
    personality = (
        "Operations orchestrator. Makes sure council decisions execute, owns agent "
        "coordination, and cross-references every domain for blockers."
    )

    async def _check_api_health(self) -> Dict[str, Any]:
        """Check real production health separately from council reporting freshness."""
        result: Dict[str, Any] = {
            "healthy": False,
            "status_code": None,
            "database": "unknown",
            "redis": "unknown",
            "brain": "unknown",
            "error": None,
        }
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                resp = await client.get(f"{API_BASE}/health")
            result["status_code"] = resp.status_code
            if resp.status_code == 200:
                body = resp.json()
                result.update({
                    "healthy": body.get("status") == "ok",
                    "database": body.get("database", "unknown"),
                    "redis": body.get("redis", "unknown"),
                    "brain": body.get("brain", "unknown"),
                })
            else:
                result["error"] = resp.text[:240]
        except Exception as e:
            result["error"] = str(e)
        return result

    def _load_report_metadata(self, agent_name: str, now: datetime) -> Dict[str, Any]:
        from ora.agents.base_executive_agent import LOG_DIR

        report_path = os.path.join(LOG_DIR, f"{agent_name}_report.json")
        metadata: Dict[str, Any] = {
            "has_file_report": False,
            "file_report_age_hours": None,
            "file_report_path": report_path,
        }
        if not os.path.exists(report_path):
            return metadata
        metadata["has_file_report"] = True
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(report_path), tz=timezone.utc)
            metadata["file_report_age_hours"] = round((now - mtime).total_seconds() / 3600, 1)
        except Exception:
            pass
        return metadata

    async def analyze(self) -> Dict[str, Any]:
        """Review operational health of all autonomous systems."""
        await self.compound_context()
        now = datetime.now(timezone.utc)
        metrics: Dict[str, Any] = {
            "analyzed_at": now.isoformat(),
            "api_health": {},
            "cron_jobs": [],
            "agent_reports": {},
            "erroring_crons": [],
            "stale_crons": [],
            "missing_agent_reports": [],
            "stale_agent_reports": [],
            "stale_agents": [],  # backwards-compatible union for older UI/report consumers
            "operational_score": 0,
            "council_reporting_score": 0,
            "severity": "ok",
            "recommendations": [],
        }

        metrics["api_health"] = await self._check_api_health()

        # ── Check cron jobs ───────────────────────────────────────────
        try:
            result = subprocess.run(
                ["openclaw", "cron", "list", "--json"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                crons = json.loads(result.stdout)
                metrics["cron_jobs"] = crons if isinstance(crons, list) else []

                for cron in metrics["cron_jobs"]:
                    cron_id = cron.get("id") or cron.get("jobId") or cron.get("name") or "unknown"
                    state = cron.get("state") or {}
                    if cron.get("last_error") or state.get("lastRunStatus") == "error" or state.get("lastStatus") == "error":
                        metrics["erroring_crons"].append(str(cron_id))
                    last_run = cron.get("last_run_at") or state.get("lastRunAt") or state.get("lastRunAtMs")
                    if last_run:
                        try:
                            if isinstance(last_run, (int, float)):
                                last_dt = datetime.fromtimestamp(last_run / 1000, tz=timezone.utc)
                            else:
                                last_dt = datetime.fromisoformat(str(last_run).replace("Z", "+00:00"))
                            age_hours = (now - last_dt).total_seconds() / 3600
                            if age_hours > 48:
                                metrics["stale_crons"].append(f"{cron_id} ({age_hours:.0f}h since run)")
                        except Exception:
                            pass
        except Exception as e:
            logger.debug(f"COO: cron list failed: {e}")

        # ── Check agent reports in Redis and local report files ────────
        for agent_name in AGENTS:
            report_status = self._load_report_metadata(agent_name, now)
            report_status.update({"has_redis_report": False, "redis_summary_length": 0, "health": "missing"})
            try:
                report = await self.get_redis_report(agent_name)
                if report:
                    report_status["has_redis_report"] = True
                    report_status["redis_summary_length"] = len(report)
            except Exception as e:
                report_status["redis_error"] = str(e)

            age = report_status.get("file_report_age_hours")
            if report_status["has_redis_report"] or (isinstance(age, (int, float)) and age <= REPORT_STALE_HOURS):
                report_status["health"] = "green"
            elif isinstance(age, (int, float)) and age <= REPORT_MISSING_ALERT_HOURS:
                report_status["health"] = "yellow"
                metrics["stale_agent_reports"].append(f"{agent_name} (file report {age:.0f}h old, no Redis summary)")
            elif report_status["has_file_report"]:
                report_status["health"] = "red"
                metrics["stale_agent_reports"].append(f"{agent_name} (file report {age or 0:.0f}h old)")
            else:
                report_status["health"] = "missing"
                metrics["missing_agent_reports"].append(f"{agent_name} (not run yet)")

            metrics["agent_reports"][agent_name] = report_status

        metrics["stale_agents"] = metrics["stale_crons"] + metrics["stale_agent_reports"] + metrics["missing_agent_reports"]

        # ── Scores ────────────────────────────────────────────────────
        # Operational score is for real incidents. Missing executive summaries
        # should not look like app downtime.
        score = 100
        if not metrics["api_health"].get("healthy"):
            score -= 50
        if metrics["api_health"].get("database") == "error":
            score -= 25
        if metrics["api_health"].get("redis") == "error":
            score -= 15
        score -= len(metrics["erroring_crons"]) * 15
        score -= len(metrics["stale_crons"]) * 5
        metrics["operational_score"] = max(0, score)

        council_score = 100
        council_score -= len(metrics["missing_agent_reports"]) * 8
        council_score -= len(metrics["stale_agent_reports"]) * 5
        metrics["council_reporting_score"] = max(0, council_score)

        if not metrics["api_health"].get("healthy") or metrics["erroring_crons"]:
            metrics["severity"] = "critical"
        elif metrics["council_reporting_score"] < 80:
            metrics["severity"] = "degraded_reporting"
        elif metrics["stale_crons"]:
            metrics["severity"] = "warning"
        else:
            metrics["severity"] = "ok"

        # ── Recommendations ───────────────────────────────────────────
        if not metrics["api_health"].get("healthy"):
            metrics["recommendations"].append(
                f"Investigate production API health: {metrics['api_health'].get('error') or metrics['api_health']}"
            )
        if metrics["erroring_crons"]:
            metrics["recommendations"].append(
                f"Fix erroring crons: {', '.join(metrics['erroring_crons'])}"
            )
        if metrics["missing_agent_reports"]:
            metrics["recommendations"].append(
                "Run executive agents to refresh council reporting: " + ", ".join(metrics["missing_agent_reports"])
            )
        if metrics["stale_agent_reports"]:
            metrics["recommendations"].append(
                "Refresh stale executive reports: " + ", ".join(metrics["stale_agent_reports"])
            )
        if not metrics["recommendations"]:
            metrics["recommendations"].append("Operations running smoothly.")

        return metrics

    async def report(self) -> str:
        data = await self.load_last_report()
        if not data:
            data = await self.analyze()
        active_agents = sum(
            1 for v in data.get("agent_reports", {}).values()
            if v.get("health") == "green"
        )
        return (
            f"🔧 *COO Report* — {data.get('analyzed_at', '')[:10]}\n"
            f"Severity: {data.get('severity', 'unknown')}\n"
            f"Operational score: {data.get('operational_score', 0)}/100\n"
            f"Council reporting: {data.get('council_reporting_score', 0)}/100\n"
            f"API: {'ok' if data.get('api_health', {}).get('healthy') else 'needs attention'}\n"
            f"Cron jobs tracked: {len(data.get('cron_jobs', []))}\n"
            f"Erroring crons: {len(data.get('erroring_crons', []))}\n"
            f"Active council reports: {active_agents}/{len(AGENTS)}\n"
            f"Missing reports: {len(data.get('missing_agent_reports', []))}\n"
            f"Recs: {'; '.join(data.get('recommendations', []))}"
        )

    async def recommend(self) -> List[str]:
        data = await self.analyze()
        return data.get("recommendations", ["Operations look healthy."])

    async def act(self) -> Dict[str, Any]:
        """Weekly COO autonomous actions."""
        data = await self.analyze()
        actions_taken = []

        await self.save_report(data, "coo_report.json")
        actions_taken.append("Saved COO report")

        summary = await self.report()
        await self.set_redis_report(summary)
        actions_taken.append("Updated COO Redis report")

        insight = (
            f"Operations health {data['analyzed_at'][:10]}: "
            f"severity={data['severity']}, operational_score={data['operational_score']}/100, "
            f"council_reporting_score={data['council_reporting_score']}/100, "
            f"api_healthy={data.get('api_health', {}).get('healthy')}, "
            f"erroring_crons={len(data['erroring_crons'])}, "
            f"missing_reports={len(data['missing_agent_reports'])}."
        )
        await self.teach_ora(insight, confidence=0.75)
        actions_taken.append("Taught Ora operational state")

        if data["severity"] == "critical":
            await self.alert_avi(
                f"⚠️ *Operations Alert*\n"
                f"Severity: critical\n"
                f"Operational score: {data['operational_score']}/100\n"
                f"API healthy: {data.get('api_health', {}).get('healthy')}\n"
                f"Erroring crons: {', '.join(data['erroring_crons']) or 'none'}\n"
                f"Recommendation: {'; '.join(data.get('recommendations', [])[:2])}"
            )
            actions_taken.append("Alerted Avi: critical operations issue")
        elif data["severity"] == "degraded_reporting":
            logger.warning(
                "COO: executive council reporting degraded but production operations are healthy: %s",
                data.get("missing_agent_reports"),
            )
            actions_taken.append("Suppressed user alert: council reporting degraded only")

        return {"agent": self.name, "actions": actions_taken, "metrics": data}
