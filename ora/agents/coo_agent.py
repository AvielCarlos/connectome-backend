"""
COO Agent — Operations Intelligence.

Runs the day-to-day. Ensures all autonomous systems are working correctly.
Identifies waste and inefficiency. Keeps Ora's cron jobs healthy.
"""

import json
import logging
import os
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from ora.agents.base_executive_agent import BaseExecutiveAgent

logger = logging.getLogger(__name__)

AGENTS = ["cfo", "cmo", "cpo", "cto", "coo", "community", "strategy"]


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

    async def analyze(self) -> Dict[str, Any]:
        """Review operational health of all autonomous systems."""
        await self.compound_context()
        now = datetime.now(timezone.utc)
        metrics: Dict[str, Any] = {
            "analyzed_at": now.isoformat(),
            "cron_jobs": [],
            "agent_reports": {},
            "erroring_crons": [],
            "stale_agents": [],
            "operational_score": 0,
            "recommendations": [],
        }

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

                # Find erroring or stale crons
                for cron in metrics["cron_jobs"]:
                    if cron.get("last_error"):
                        metrics["erroring_crons"].append(cron.get("id", "unknown"))
                    last_run = cron.get("last_run_at")
                    if last_run:
                        try:
                            last_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
                            age_hours = (now - last_dt).total_seconds() / 3600
                            if age_hours > 48:
                                metrics["stale_agents"].append(cron.get("id", "unknown"))
                        except Exception:
                            pass
        except Exception as e:
            logger.debug(f"COO: cron list failed: {e}")

        # ── Check agent reports in Redis ──────────────────────────────
        for agent_name in AGENTS:
            try:
                report = await self.get_redis_report(agent_name)
                if report:
                    metrics["agent_reports"][agent_name] = {
                        "has_report": True,
                        "summary_length": len(report),
                    }
                else:
                    metrics["agent_reports"][agent_name] = {
                        "has_report": False,
                        "summary_length": 0,
                    }
                    metrics["stale_agents"].append(f"{agent_name} (no Redis report)")
            except Exception:
                pass

        # ── Also check log files ───────────────────────────────────────
        from ora.agents.base_executive_agent import LOG_DIR
        for agent_name in AGENTS:
            report_path = os.path.join(LOG_DIR, f"{agent_name}_report.json")
            if os.path.exists(report_path):
                mtime = datetime.fromtimestamp(os.path.getmtime(report_path), tz=timezone.utc)
                age_hours = (now - mtime).total_seconds() / 3600
                if age_hours > 168:  # 7 days
                    if f"{agent_name} (stale log)" not in metrics["stale_agents"]:
                        metrics["stale_agents"].append(f"{agent_name} (log {age_hours:.0f}h old)")

        # ── Operational score ─────────────────────────────────────────
        score = 100
        score -= len(metrics["erroring_crons"]) * 15
        score -= len(metrics["stale_agents"]) * 10
        metrics["operational_score"] = max(0, score)

        # ── Recommendations ───────────────────────────────────────────
        if metrics["erroring_crons"]:
            metrics["recommendations"].append(
                f"Fix erroring crons: {', '.join(metrics['erroring_crons'])}"
            )
        stale_list = [a for a in metrics["stale_agents"] if "no Redis report" in str(a)]
        if stale_list:
            metrics["recommendations"].append(
                f"These agents haven't reported: {', '.join(stale_list)}"
            )
        if metrics["operational_score"] > 80:
            metrics["recommendations"].append("Operations running smoothly.")

        return metrics

    async def report(self) -> str:
        data = await self.load_last_report()
        if not data:
            data = await self.analyze()
        active_agents = sum(
            1 for v in data.get("agent_reports", {}).values()
            if v.get("has_report")
        )
        return (
            f"🔧 *COO Report* — {data.get('analyzed_at', '')[:10]}\n"
            f"Operational score: {data.get('operational_score', 0)}/100\n"
            f"Cron jobs tracked: {len(data.get('cron_jobs', []))}\n"
            f"Erroring crons: {len(data.get('erroring_crons', []))}\n"
            f"Active agents: {active_agents}/{len(AGENTS)}\n"
            f"Stale agents: {len(data.get('stale_agents', []))}\n"
            f"Recs: {'; '.join(data.get('recommendations', []))}"
        )

    async def recommend(self) -> List[str]:
        data = await self.analyze()
        return data.get("recommendations", ["Operations look healthy."])

    async def act(self) -> Dict[str, Any]:
        """Weekly COO autonomous actions."""
        data = await self.analyze()
        actions_taken = []

        # Save report
        await self.save_report(data, "coo_report.json")
        actions_taken.append("Saved COO report")

        # Redis
        summary = await self.report()
        await self.set_redis_report(summary)

        # Teach Ora
        insight = (
            f"Operations health {data['analyzed_at'][:10]}: "
            f"score={data['operational_score']}/100, "
            f"crons={len(data['cron_jobs'])}, "
            f"erroring={len(data['erroring_crons'])}, "
            f"stale agents={len(data['stale_agents'])}."
        )
        await self.teach_ora(insight, confidence=0.75)
        actions_taken.append("Taught Ora operational state")

        # Alert if things are broken
        if data["operational_score"] < 50:
            await self.alert_avi(
                f"⚠️ *Operations Alert*\n"
                f"Score: {data['operational_score']}/100\n"
                f"Erroring crons: {', '.join(data['erroring_crons']) or 'none'}\n"
                f"Stale agents: {', '.join(str(a) for a in data['stale_agents'][:5]) or 'none'}"
            )
            actions_taken.append("Alerted Avi: low operational score")

        return {"agent": self.name, "actions": actions_taken, "metrics": data}
