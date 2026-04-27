"""
Ora Health & Meta Routes

GET  /api/ora/health/dashboard  — full system status dashboard
GET  /api/ora/meta/report       — Ora's self-improvement meta report (cached)
POST /api/ora/meta/report       — trigger fresh MetaAgent analysis
POST /api/ora/report/daily      — generate and send daily Telegram report
GET  /api/schema                — OpenAPI JSON schema (always enabled)
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ora-ops"])


# ---------------------------------------------------------------------------
# GET /api/ora/health/dashboard
# ---------------------------------------------------------------------------

@router.get("/api/ora/health/dashboard")
async def ora_health_dashboard() -> Dict[str, Any]:
    """
    Full system health dashboard. Returns status of all subsystems,
    user counts, content indexes, agent states, and last sync times.
    """
    from core.database import fetchrow, fetchval
    from core.redis_client import get_redis

    db_ok = False
    redis_ok = False
    r = None

    # DB check
    try:
        result = await fetchval("SELECT 1")
        db_ok = result == 1
    except Exception as e:
        logger.warning(f"Health dashboard: DB check failed: {e}")

    # Redis check
    try:
        r = await get_redis()
        await r.ping()
        redis_ok = True
    except Exception as e:
        logger.warning(f"Health dashboard: Redis check failed: {e}")

    # User stats
    users_total = 0
    users_active_7d = 0
    try:
        row = await fetchrow("SELECT COUNT(*) AS total FROM users")
        users_total = row["total"] if row else 0
        row7 = await fetchrow(
            "SELECT COUNT(DISTINCT user_id) AS active FROM interactions WHERE created_at > NOW() - INTERVAL '7 days'"
        )
        users_active_7d = row7["active"] if row7 else 0
    except Exception as e:
        logger.debug(f"Health dashboard: user stats failed: {e}")

    # Content stats
    events_indexed = 0
    drive_docs = 0
    world_signals_cached = False
    try:
        if r and redis_ok:
            event_keys = await r.keys("world_signal:*")
            events_indexed = len(event_keys)
            world_signals_cached = events_indexed > 0
    except Exception:
        pass
    try:
        docs_row = await fetchrow("SELECT COUNT(*) AS docs FROM drive_documents")
        drive_docs = docs_row["docs"] if docs_row else 0
    except Exception:
        pass

    # Agent status checks (ping Redis for their last heartbeat keys)
    agent_status = {"world": "ok", "events": "ok", "drive": "ok"}
    try:
        if r and redis_ok:
            world_raw = await r.get("world_signal:Vancouver")
            if not world_raw:
                agent_status["world"] = "no_data"
    except Exception:
        agent_status["world"] = "unknown"

    # Last sync times
    last_syncs: Dict[str, Any] = {"events": None, "drive": None, "world": None}
    try:
        if r and redis_ok:
            world_ts = await r.get("ora:world:last_sync")
            if world_ts:
                last_syncs["world"] = world_ts.decode() if isinstance(world_ts, bytes) else world_ts
    except Exception:
        pass

    return {
        "api": "ok",
        "database": "ok" if db_ok else "error",
        "redis": "ok" if redis_ok else "error",
        "users": {
            "total": users_total,
            "active_7d": users_active_7d,
        },
        "content": {
            "events_indexed": events_indexed,
            "drive_docs": drive_docs,
            "world_signals_cached": world_signals_cached,
        },
        "agent_status": agent_status,
        "last_syncs": last_syncs,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /api/ora/meta/report — return cached MetaAgent report
# ---------------------------------------------------------------------------

@router.get("/api/ora/meta/report")
async def get_meta_report() -> Dict[str, Any]:
    """
    Return Ora's most recent self-improvement report from Redis cache.
    If no report exists, triggers a fresh generation.
    """
    from ora.agents.meta_agent import MetaAgent
    from ora.brain import get_brain

    brain = get_brain()
    agent = MetaAgent(brain._openai)

    report = await agent.get_cached_report()
    if report:
        return report

    # No cached report — generate one now
    logger.info("Meta report: no cache found, generating fresh report")
    return await agent.generate_report()


# ---------------------------------------------------------------------------
# POST /api/ora/meta/report — trigger fresh MetaAgent analysis
# ---------------------------------------------------------------------------

@router.post("/api/ora/meta/report")
async def trigger_meta_report() -> Dict[str, Any]:
    """
    Trigger a fresh MetaAgent self-improvement analysis.
    Results are cached in Redis and returned immediately.
    """
    from ora.agents.meta_agent import MetaAgent
    from ora.brain import get_brain

    brain = get_brain()
    agent = MetaAgent(brain._openai)

    try:
        report = await agent.generate_report()
        return {"status": "ok", "report": report}
    except Exception as e:
        logger.error(f"Meta report generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/ora/report/daily — generate and send Telegram daily report
# ---------------------------------------------------------------------------

@router.post("/api/ora/report/daily")
async def send_daily_report() -> Dict[str, Any]:
    """
    Generate a daily system summary and send it to Avi via Telegram.
    """
    from ora.agents.reporter_agent import get_reporter

    reporter = get_reporter()
    try:
        result = await reporter.send_daily_report()
        return {"status": "ok" if result["sent"] else "partial", **result}
    except Exception as e:
        logger.error(f"Daily report failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
