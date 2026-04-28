"""
Executive Council API Routes.

Provides endpoints for the admin dashboard to view:
- Latest executive brief
- Agent status grid
- Combined metrics snapshot

All routes require admin auth (X-Admin-Token header).
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException

from core.database import fetchrow

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/executive", tags=["executive"])

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "connectome-admin-secret")
LOG_DIR = "/Users/avielcarlos/.openclaw/workspace/tmp/executive_council"

AGENT_NAMES = ["cfo", "cmo", "cpo", "cto", "coo", "community", "strategy", "executive_council"]


def _require_admin(x_admin_token: str = Header(default="")):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")


def _load_agent_report(agent_name: str) -> Optional[Dict]:
    """Load an agent's latest JSON report from disk."""
    path = os.path.join(LOG_DIR, f"{agent_name}_report.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return None


async def _get_redis_report(agent_name: str) -> Optional[str]:
    """Get an agent's summary from Redis."""
    try:
        from core.redis_client import get_redis
        redis = await get_redis()
        return await redis.get(f"ora:executive:last_report:{agent_name}")
    except Exception:
        return None


@router.get("/brief")
async def get_latest_brief(
    x_admin_token: str = Header(default=""),
) -> Dict[str, Any]:
    """
    Get the latest Executive Council weekly brief.
    Falls back to most recent saved brief if council hasn't convened yet.
    """
    _require_admin(x_admin_token)

    # Try the standard report file first
    brief = _load_agent_report("executive_council")
    if brief:
        return {"brief": brief, "source": "file"}

    # Try to find a dated brief
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        files = sorted(
            [f for f in os.listdir(LOG_DIR) if f.startswith("weekly_brief_")],
            reverse=True
        )
        if files:
            with open(os.path.join(LOG_DIR, files[0])) as f:
                brief = json.load(f)
            return {"brief": brief, "source": f"file:{files[0]}"}
    except Exception:
        pass

    return {
        "brief": None,
        "source": "none",
        "message": "No council brief found yet. Cron runs Sunday at 11am PT.",
    }


@router.get("/agents")
async def get_agent_status(
    x_admin_token: str = Header(default=""),
) -> Dict[str, Any]:
    """
    Status of all executive agents — last run, last insight, health indicator.
    """
    _require_admin(x_admin_token)

    statuses = []
    now = datetime.now(timezone.utc)

    for agent_name in AGENT_NAMES:
        report_data = _load_agent_report(agent_name)
        redis_summary = await _get_redis_report(agent_name)

        status = {
            "name": agent_name,
            "has_report": report_data is not None,
            "has_redis_summary": redis_summary is not None,
            "last_run_at": None,
            "age_hours": None,
            "health": "red",  # red / yellow / green
            "summary_preview": None,
        }

        if report_data:
            saved_at = report_data.get("_saved_at") or report_data.get("analyzed_at")
            if saved_at:
                try:
                    dt = datetime.fromisoformat(saved_at.replace("Z", "+00:00"))
                    status["last_run_at"] = saved_at
                    age_h = (now - dt).total_seconds() / 3600
                    status["age_hours"] = round(age_h, 1)
                    if age_h < 24:
                        status["health"] = "green"
                    elif age_h < 168:  # 7 days
                        status["health"] = "yellow"
                    else:
                        status["health"] = "red"
                except Exception:
                    pass

        if redis_summary:
            status["summary_preview"] = redis_summary[:200]
        elif report_data:
            # Build a preview from the report
            status["summary_preview"] = json.dumps(report_data, default=str)[:200]

        statuses.append(status)

    return {
        "agents": statuses,
        "total": len(statuses),
        "healthy": sum(1 for s in statuses if s["health"] == "green"),
        "warning": sum(1 for s in statuses if s["health"] == "yellow"),
        "critical": sum(1 for s in statuses if s["health"] == "red"),
        "checked_at": now.isoformat(),
    }


@router.get("/metrics")
async def get_executive_metrics(
    x_admin_token: str = Header(default=""),
) -> Dict[str, Any]:
    """
    Combined metrics snapshot from all agents.
    Great for a quick financial + growth + tech overview.
    """
    _require_admin(x_admin_token)

    snapshot: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "financial": {},
        "growth": {},
        "product": {},
        "infrastructure": {},
        "community": {},
    }

    # Financial (from CFO report)
    cfo_report = _load_agent_report("cfo")
    if cfo_report:
        snapshot["financial"] = {
            "mrr_usd": cfo_report.get("mrr_usd", 0),
            "arr_usd": cfo_report.get("arr_usd", 0),
            "active_subscriptions": cfo_report.get("active_subscriptions", 0),
            "revenue_last_30d_usd": cfo_report.get("revenue_last_30d_usd", 0),
            "churn_rate_pct": cfo_report.get("churn_rate_pct", 0),
            "gross_margin_pct": cfo_report.get("gross_margin_pct", 0),
            "last_updated": cfo_report.get("analyzed_at", ""),
        }

    # Growth (from CMO report)
    cmo_report = _load_agent_report("cmo")
    if cmo_report:
        snapshot["growth"] = {
            "total_users": cmo_report.get("total_users", 0),
            "new_users_7d": cmo_report.get("new_users_7d", 0),
            "active_users_7d": cmo_report.get("active_users_7d", 0),
            "weekly_growth_rate_pct": cmo_report.get("weekly_growth_rate_pct", 0),
            "growth_trend": cmo_report.get("growth_trend", "unknown"),
            "best_channel": cmo_report.get("best_channel", "organic"),
            "last_updated": cmo_report.get("analyzed_at", ""),
        }

    # Fallback: pull from DB for basic user count
    if not snapshot["growth"].get("total_users"):
        try:
            row = await fetchrow("SELECT COUNT(*) as n FROM users")
            if row:
                snapshot["growth"]["total_users"] = row["n"] or 0
        except Exception:
            pass

    # Product (from CPO report)
    cpo_report = _load_agent_report("cpo")
    if cpo_report:
        snapshot["product"] = {
            "top_goal_themes": cpo_report.get("top_goal_themes", []),
            "avg_card_rating": cpo_report.get("avg_card_rating", 0),
            "onboarding_completion_rate_pct": cpo_report.get("onboarding_completion_rate_pct", 0),
            "pain_points": cpo_report.get("pain_points", []),
            "wins": cpo_report.get("wins", []),
            "last_updated": cpo_report.get("analyzed_at", ""),
        }

    # Infrastructure (from CTO report)
    cto_report = _load_agent_report("cto")
    if cto_report:
        snapshot["infrastructure"] = {
            "api_healthy": cto_report.get("api_healthy", False),
            "api_response_time_s": cto_report.get("api_response_time_s"),
            "health_score": cto_report.get("health_score", 0),
            "ci_status": cto_report.get("ci_status", "unknown"),
            "issues": cto_report.get("issues", []),
            "last_updated": cto_report.get("analyzed_at", ""),
        }

    # Community (from community report)
    community_report = _load_agent_report("community")
    if community_report:
        snapshot["community"] = {
            "total_contributors": community_report.get("total_contributors", 0),
            "active_contributors_30d": community_report.get("active_contributors_30d", 0),
            "new_contributors_30d": community_report.get("new_contributors_30d", 0),
            "community_health_score": community_report.get("community_health_score", 0),
            "last_updated": community_report.get("analyzed_at", ""),
        }

    return snapshot


@router.post("/run/{agent_name}")
async def run_agent(
    agent_name: str,
    background_tasks: BackgroundTasks,
    x_admin_token: str = Header(default=""),
) -> Dict[str, Any]:
    """
    Manually trigger a specific agent's full run cycle (analyze + act).
    Runs in background. Returns immediately.
    """
    _require_admin(x_admin_token)

    valid_agents = {
        "cfo": "ora.agents.cfo_agent.CFOAgent",
        "cmo": "ora.agents.cmo_agent.CMOAgent",
        "cpo": "ora.agents.cpo_agent.CPOAgent",
        "cto": "ora.agents.cto_agent.CTOAgent",
        "coo": "ora.agents.coo_agent.COOAgent",
        "community": "ora.agents.community_agent.CommunityAgent",
        "strategy": "ora.agents.strategy_agent.StrategyAgent",
        "executive_council": "ora.agents.executive_council.ExecutiveCouncil",
    }

    if agent_name not in valid_agents:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown agent: {agent_name}. Valid: {list(valid_agents.keys())}"
        )

    async def _run():
        try:
            module_path, class_name = valid_agents[agent_name].rsplit(".", 1)
            import importlib
            module = importlib.import_module(module_path)
            AgentClass = getattr(module, class_name)
            agent = AgentClass()
            if agent_name == "executive_council":
                result = await agent.act()
            else:
                result = await agent.act()
            logger.info(f"Executive API: {agent_name} completed: {result}")
        except Exception as e:
            logger.error(f"Executive API: {agent_name} failed: {e}")

    background_tasks.add_task(_run)

    return {
        "status": "started",
        "agent": agent_name,
        "message": f"{agent_name} is running in the background",
    }


@router.get("/health-check")
async def quick_health_check() -> Dict[str, Any]:
    """
    Quick CTO health check — no auth required (used by monitoring).
    """
    try:
        from ora.agents.cto_agent import CTOAgent
        cto = CTOAgent()
        result = await cto.run_health_check()
        return result
    except Exception as e:
        return {
            "healthy": False,
            "error": str(e),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
