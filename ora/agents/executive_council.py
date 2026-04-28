"""
Executive Council — The meta-agent that synthesizes all C-suite perspectives.

Every Sunday, the council convenes. Each agent submits their report.
The council synthesizes into ONE strategic brief and distributes it.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from ora.agents.base_executive_agent import BaseExecutiveAgent, LOG_DIR, API_BASE

logger = logging.getLogger(__name__)

ASCENSION_CHAT_ID = os.getenv("ASCENSION_CHANNEL_ID", "-1001234567890")  # @ascensionai channel
TELEGRAM_CHAT_ID = 5716959016

AGENT_NAMES = ["cfo", "cmo", "cpo", "cto", "coo", "community", "strategy"]


class ExecutiveCouncil(BaseExecutiveAgent):
    """
    Ora's Executive Council — the governing intelligence layer.
    
    Convenes weekly. Reads every agent's latest report. Synthesizes them
    into a single strategic brief. Distributes to Avi + Ora's brain.
    
    This is how Ora becomes smarter than any single agent.
    """

    name = "executive_council"
    display_name = "Executive Council"

    async def analyze(self) -> Dict[str, Any]:
        """Load all agent reports and prepare council input."""
        now = datetime.now(timezone.utc)
        council_input: Dict[str, Any] = {
            "analyzed_at": now.isoformat(),
            "agent_reports": {},
            "agents_reporting": 0,
            "agents_silent": [],
        }

        for agent_name in AGENT_NAMES:
            # Try Redis first (freshest)
            report = await self.get_redis_report(agent_name)
            if report:
                council_input["agent_reports"][agent_name] = report
                council_input["agents_reporting"] += 1
            else:
                # Fall back to log file
                report_path = os.path.join(LOG_DIR, f"{agent_name}_report.json")
                if os.path.exists(report_path):
                    try:
                        with open(report_path) as f:
                            file_data = json.load(f)
                        # Create a brief summary from the file
                        saved_at = file_data.get("_saved_at", "unknown")
                        council_input["agent_reports"][agent_name] = (
                            f"[From log file, saved {saved_at[:10]}]\n"
                            + json.dumps(file_data, default=str)[:500]
                        )
                        council_input["agents_reporting"] += 1
                    except Exception:
                        council_input["agents_silent"].append(agent_name)
                else:
                    council_input["agents_silent"].append(agent_name)

        return council_input

    async def report(self) -> str:
        data = await self.load_last_report()
        if not data:
            data = await self.analyze()
        return (
            f"🏛️ *Executive Council* — {data.get('analyzed_at', '')[:10]}\n"
            f"Agents reporting: {data.get('agents_reporting', 0)}/{len(AGENT_NAMES)}\n"
            f"Silent agents: {', '.join(data.get('agents_silent', [])) or 'none'}"
        )

    async def recommend(self) -> List[str]:
        brief = await self.convene()
        return brief.get("top_priorities", ["Council synthesis pending"])

    async def act(self) -> Dict[str, Any]:
        """Convene the council and distribute the brief."""
        brief = await self.convene()
        actions_taken = []

        # Save the brief
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = await self.save_report(brief, f"weekly_brief_{date_str}.json")
        actions_taken.append(f"Saved weekly brief to {path}")

        # Also save to the standard council location
        await self.save_report(brief, "executive_council_report.json")

        # Store in Redis
        summary = brief.get("public_summary", "")
        await self.set_redis_report(summary)

        # Teach Ora the full council synthesis
        await self.teach_ora(
            f"Executive council brief {date_str}: {brief.get('strategic_synthesis', '')[:500]}",
            confidence=0.9
        )
        actions_taken.append("Taught Ora council synthesis")

        # Send full brief to Avi via Telegram
        full_brief = self._format_telegram_brief(brief)
        await self._send_telegram(full_brief, chat_id=TELEGRAM_CHAT_ID)
        actions_taken.append("Sent full brief to Avi")

        # Post public summary to @ascensionai
        try:
            ascension_id = int(ASCENSION_CHAT_ID)
            if ascension_id != -1001234567890:  # Only if real channel ID is set
                public_msg = self._format_public_brief(brief)
                await self._send_telegram(public_msg, chat_id=ascension_id)
                actions_taken.append("Posted public summary to @ascensionai")
        except Exception:
            pass

        return {"agent": self.name, "actions": actions_taken, "brief": brief}

    async def convene(self) -> Dict[str, Any]:
        """
        The council convenes.
        1. Load all agent reports
        2. Synthesize into strategic brief
        3. Return the brief dict
        """
        now = datetime.now(timezone.utc)
        council_data = await self.analyze()

        # Build the synthesis
        agent_reports = council_data.get("agent_reports", {})

        # Extract key signals from each report
        financial_signal = agent_reports.get("cfo", "")
        growth_signal = agent_reports.get("cmo", "")
        product_signal = agent_reports.get("cpo", "")
        tech_signal = agent_reports.get("cto", "")
        ops_signal = agent_reports.get("coo", "")
        community_signal = agent_reports.get("community", "")
        strategy_signal = agent_reports.get("strategy", "")

        # Synthesize priorities (rule-based + pattern matching)
        priorities = []
        risks = []
        opportunities = []
        actions = []

        # Parse signals for key patterns
        if "churn" in financial_signal.lower() or "declining" in financial_signal.lower():
            risks.append("Financial: churn risk detected — investigate cancellation reasons")
        elif "mrr" in financial_signal.lower():
            opportunities.append("Financial: revenue growing — optimize pricing and upsell")

        if "declining" in growth_signal.lower() or "flat" in growth_signal.lower():
            risks.append("Growth: acquisition slowing — needs new channels or experiments")
        elif "growing" in growth_signal.lower() or "accelerating" in growth_signal.lower():
            opportunities.append("Growth: momentum building — double down on winning channels")

        if "pain" in product_signal.lower() or "onboarding" in product_signal.lower():
            priorities.append("Product: UX improvements needed — focus on onboarding completion")

        if "down" in tech_signal.lower() or "failing" in tech_signal.lower():
            risks.append("Tech: infrastructure issues — fix before scaling")
        else:
            opportunities.append("Tech: infrastructure healthy — ready to scale")

        if "low" in community_signal.lower() or "inactive" in community_signal.lower():
            actions.append("Community: re-engage inactive contributors, launch new bounties")

        # Default priorities if nothing detected
        if not priorities:
            priorities = [
                "Grow to next MRR milestone",
                "Ship product improvements based on user feedback",
                "Grow community and DAO participation",
            ]
        if not risks:
            risks = ["Monitor churn rates as user base grows"]
        if not opportunities:
            opportunities = [
                "Build viral sharing features to improve K-factor",
                "Deepen AI coaching quality as key differentiator",
            ]
        if not actions:
            actions = [
                "Run weekly growth experiment",
                "Review and improve onboarding flow",
                "Engage top contributors with CP recognition",
            ]

        # Strategic synthesis paragraph
        synthesis = (
            f"Week of {now.strftime('%Y-%m-%d')}: "
            f"Ora's autonomous council has reviewed all domains. "
            f"{len(agent_reports)}/{len(AGENT_NAMES)} agents reported. "
            f"Key focus: {priorities[0] if priorities else 'sustained growth'}. "
            f"Main risk: {risks[0] if risks else 'none critical'}. "
            f"Top opportunity: {opportunities[0] if opportunities else 'deepen AI quality'}."
        )

        brief = {
            "convened_at": now.isoformat(),
            "agents_reporting": council_data["agents_reporting"],
            "agents_silent": council_data["agents_silent"],
            "top_priorities": priorities[:3],
            "key_risks": risks[:3],
            "key_opportunities": opportunities[:3],
            "recommended_actions": actions[:4],
            "strategic_synthesis": synthesis,
            "agent_reports_snapshot": {
                k: v[:300] if isinstance(v, str) else str(v)[:300]
                for k, v in agent_reports.items()
            },
            "public_summary": (
                f"🏛️ Ora Executive Council — Week of {now.strftime('%b %d, %Y')}\n\n"
                f"Priorities: {' | '.join(priorities[:3])}\n"
                f"Opportunities: {' | '.join(opportunities[:2])}\n"
                f"Actions: {' | '.join(actions[:3])}"
            ),
        }

        return brief

    async def escalate(self, issue: str, agent: str) -> None:
        """
        Called by any agent when something needs Avi's immediate attention.
        Routes through the council for context.
        """
        message = (
            f"🚨 *ESCALATION from {agent.upper()}*\n\n"
            f"{issue}\n\n"
            f"_Escalated via Executive Council_"
        )
        await self._send_telegram(message, chat_id=TELEGRAM_CHAT_ID)
        await self.teach_ora(
            f"Executive escalation from {agent}: {issue[:200]}",
            confidence=0.9
        )

    def _format_telegram_brief(self, brief: Dict) -> str:
        """Format the full brief for Telegram delivery to Avi."""
        priorities = "\n".join(f"  {i+1}. {p}" for i, p in enumerate(brief.get("top_priorities", [])))
        risks = "\n".join(f"  ⚠️ {r}" for r in brief.get("key_risks", []))
        opps = "\n".join(f"  ✨ {o}" for o in brief.get("key_opportunities", []))
        actions = "\n".join(f"  → {a}" for a in brief.get("recommended_actions", []))

        silent = brief.get("agents_silent", [])
        silent_str = f"\n\n_Silent agents: {', '.join(silent)}_" if silent else ""

        return (
            f"🏛️ *Executive Council — Weekly Brief*\n"
            f"_{brief.get('convened_at', '')[:10]}_\n"
            f"_{brief.get('agents_reporting', 0)}/{len(AGENT_NAMES)} agents reporting_\n\n"
            f"*🎯 Top Priorities:*\n{priorities}\n\n"
            f"*⚠️ Key Risks:*\n{risks}\n\n"
            f"*✨ Opportunities:*\n{opps}\n\n"
            f"*→ Recommended Actions:*\n{actions}"
            f"{silent_str}"
        )

    def _format_public_brief(self, brief: Dict) -> str:
        """Format a clean public summary for @ascensionai."""
        now = datetime.now(timezone.utc)
        priorities = " → ".join(brief.get("top_priorities", [])[:2])
        return (
            f"📊 *Ora Weekly Update — {now.strftime('%b %d')}*\n\n"
            f"Our autonomous executive council has convened.\n\n"
            f"This week's focus: {priorities}\n\n"
            f"Ora is compounding intelligence across finance, growth, product, "
            f"tech, ops, and strategy — all autonomously.\n\n"
            f"Building the future, one week at a time. 🧠"
        )
