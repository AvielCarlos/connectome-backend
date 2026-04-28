"""
Admin Routes — Internal insights and system health.
Protected by a simple admin token header (X-Admin-Token).
"""

import logging
import os
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Header

from core.database import fetchrow, fetch

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "connectome-admin-secret")


def _require_admin(x_admin_token: str = Header(default="")):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.get("/insights")
async def get_admin_insights(x_admin_token: str = Header(default="")) -> Dict[str, Any]:
    """
    Return platform-level insights for the admin dashboard.
    Includes WorldAgent stats alongside user/revenue metrics.
    """
    _require_admin(x_admin_token)

    # Core metrics
    users_row = await fetchrow(
        "SELECT COUNT(*) as total, "
        "COUNT(*) FILTER (WHERE last_active > NOW() - INTERVAL '1 day') as active_today, "
        "COUNT(*) FILTER (WHERE subscription_tier = 'premium') as premium, "
        "AVG(fulfilment_score) as avg_fulfilment "
        "FROM users"
    )

    revenue_row = await fetchrow(
        "SELECT COALESCE(SUM(amount_cents), 0) as total FROM revenue_events"
    )

    # Agent performance
    agent_rows = await fetch(
        """
        SELECT agent_type,
               COUNT(*) as total_screens,
               AVG(global_rating) as avg_rating,
               SUM(impression_count) as total_impressions
        FROM screen_specs
        WHERE agent_type IS NOT NULL
        GROUP BY agent_type
        ORDER BY total_screens DESC
        """
    )

    # WorldAgent stats
    world_count_row = await fetchrow(
        "SELECT COUNT(*) as total FROM world_signals"
    )
    world_recent_row = await fetchrow(
        "SELECT MAX(fetched_at) as last_fetch FROM world_signals"
    )
    world_sources_rows = await fetch(
        "SELECT DISTINCT source FROM world_signals WHERE fetched_at > NOW() - INTERVAL '6 hours'"
    )

    world_signals_count = int(world_count_row["total"]) if world_count_row else 0
    last_fetch = (
        world_recent_row["last_fetch"].isoformat()
        if world_recent_row and world_recent_row["last_fetch"]
        else None
    )
    sources_active = [row["source"] for row in world_sources_rows]

    return {
        "total_users": int(users_row["total"]) if users_row else 0,
        "active_today": int(users_row["active_today"]) if users_row else 0,
        "premium_users": int(users_row["premium"]) if users_row else 0,
        "avg_fulfilment_score": round(float(users_row["avg_fulfilment"] or 0), 3) if users_row else 0.0,
        "total_revenue_cents": int(revenue_row["total"]) if revenue_row else 0,
        "top_agents": [
            {
                "agent_type": row["agent_type"],
                "total_screens": int(row["total_screens"]),
                "avg_rating": round(float(row["avg_rating"] or 0), 2),
                "total_impressions": int(row["total_impressions"] or 0),
            }
            for row in agent_rows
        ],
        "avg_rating_by_agent": {
            row["agent_type"]: round(float(row["avg_rating"] or 0), 2)
            for row in agent_rows
        },
        # WorldAgent-specific fields
        "world_signals_count": world_signals_count,
        "last_fetch": last_fetch,
        "sources_active": sources_active,
    }


@router.get("/experiments")
async def get_experiments(
    status: str = None,
    limit: int = 50,
    x_admin_token: str = Header(default=""),
) -> Dict[str, Any]:
    """
    Return all feedback experiments with stats.
    This shows what Ora is currently testing.
    """
    _require_admin(x_admin_token)

    if status:
        rows = await fetch(
            """
            SELECT id, hypothesis, mechanism_type, control_mechanism, screen_types,
                   status, sample_size_target, control_count, treatment_count,
                   control_response_rate, treatment_response_rate,
                   control_signal_quality, treatment_signal_quality,
                   p_value, winner, summary, started_at, completed_at,
                   duration_days, created_at
            FROM feedback_experiments
            WHERE status = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            status,
            limit,
        )
    else:
        rows = await fetch(
            """
            SELECT id, hypothesis, mechanism_type, control_mechanism, screen_types,
                   status, sample_size_target, control_count, treatment_count,
                   control_response_rate, treatment_response_rate,
                   control_signal_quality, treatment_signal_quality,
                   p_value, winner, summary, started_at, completed_at,
                   duration_days, created_at
            FROM feedback_experiments
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )

    return {
        "experiments": [
            {
                "id": str(row["id"]),
                "hypothesis": row["hypothesis"],
                "mechanism_type": row["mechanism_type"],
                "control_mechanism": row["control_mechanism"],
                "screen_types": row["screen_types"],
                "status": row["status"],
                "sample_size_target": row["sample_size_target"],
                "control_count": row["control_count"],
                "treatment_count": row["treatment_count"],
                "control_response_rate": round(float(row["control_response_rate"] or 0), 4),
                "treatment_response_rate": round(float(row["treatment_response_rate"] or 0), 4),
                "control_signal_quality": round(float(row["control_signal_quality"] or 0), 4),
                "treatment_signal_quality": round(float(row["treatment_signal_quality"] or 0), 4),
                "p_value": round(float(row["p_value"]), 4) if row["p_value"] is not None else None,
                "winner": row["winner"],
                "summary": row["summary"],
                "started_at": row["started_at"].isoformat() if row["started_at"] else None,
                "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
                "duration_days": row["duration_days"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            for row in rows
        ],
        "total": len(rows),
    }


@router.get("/lessons")
async def get_ora_lessons(
    limit: int = 50,
    source: str = None,
    x_admin_token: str = Header(default=""),
) -> Dict[str, Any]:
    """
    Return ora_lessons ordered by created_at DESC.
    This is Ora's mind — what she currently knows.
    """
    _require_admin(x_admin_token)

    if source:
        rows = await fetch(
            """
            SELECT id, source, lesson, confidence, applied, applies_to, created_at
            FROM ora_lessons
            WHERE source = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            source,
            limit,
        )
    else:
        rows = await fetch(
            """
            SELECT id, source, lesson, confidence, applied, applies_to, created_at
            FROM ora_lessons
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )

    return {
        "lessons": [
            {
                "id": str(row["id"]),
                "source": row["source"],
                "lesson": row["lesson"],
                "confidence": round(float(row["confidence"] or 0.7), 3),
                "applied": row["applied"],
                "applies_to": row["applies_to"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            for row in rows
        ],
        "total": len(rows),
    }


@router.get("/world-signals")
async def get_world_signals(
    limit: int = 20,
    source: str = None,
    x_admin_token: str = Header(default=""),
) -> Dict[str, Any]:
    """
    Browse world signals in the DB. Useful for debugging WorldAgent.
    """
    _require_admin(x_admin_token)

    if source:
        rows = await fetch(
            """
            SELECT id, source, signal_type, title, summary, url, location, tags,
                   relevance_score, fetched_at
            FROM world_signals
            WHERE source = $1
            ORDER BY fetched_at DESC
            LIMIT $2
            """,
            source,
            limit,
        )
    else:
        rows = await fetch(
            """
            SELECT id, source, signal_type, title, summary, url, location, tags,
                   relevance_score, fetched_at
            FROM world_signals
            ORDER BY fetched_at DESC
            LIMIT $1
            """,
            limit,
        )

    return {
        "signals": [
            {
                "id": str(row["id"]),
                "source": row["source"],
                "signal_type": row["signal_type"],
                "title": row["title"],
                "summary": row["summary"],
                "url": row["url"],
                "location": row["location"],
                "tags": row["tags"],
                "relevance_score": row["relevance_score"],
                "fetched_at": row["fetched_at"].isoformat() if row["fetched_at"] else None,
            }
            for row in rows
        ],
        "total": len(rows),
    }


@router.get("/collective")
async def get_collective_insights(
    x_admin_token: str = Header(default=""),
) -> Dict[str, Any]:
    """
    Return Ora's collective intelligence state:
    - Latest collective_wisdom entry (what humanity is reaching for right now)
    - Active suppressions (agent+domain combos being suppressed globally)
    - collective_voice (Ora's synthesis of the collective signal)

    This endpoint is powered by CollectiveIntelligenceAgent.
    All data is aggregate — no individual user data ever appears here.
    """
    _require_admin(x_admin_token)

    # Latest collective wisdom
    wisdom_row = await fetchrow(
        """
        SELECT id, computed_at, total_users_analyzed, total_interactions_analyzed,
               fulfilment_drivers, distress_patterns, temporal_patterns,
               domain_synergies, surprises, collective_voice
        FROM collective_wisdom
        ORDER BY computed_at DESC LIMIT 1
        """
    )

    wisdom = None
    if wisdom_row:
        wisdom = {
            "id": str(wisdom_row["id"]),
            "computed_at": wisdom_row["computed_at"].isoformat() if wisdom_row["computed_at"] else None,
            "total_users_analyzed": wisdom_row["total_users_analyzed"],
            "total_interactions_analyzed": wisdom_row["total_interactions_analyzed"],
            "fulfilment_drivers": wisdom_row["fulfilment_drivers"] or [],
            "distress_patterns": wisdom_row["distress_patterns"] or [],
            "temporal_patterns": wisdom_row["temporal_patterns"] or {},
            "domain_synergies": wisdom_row["domain_synergies"] or [],
            "surprises": wisdom_row["surprises"] or [],
            "collective_voice": wisdom_row["collective_voice"],
        }

    # Active suppressions
    suppression_rows = await fetch(
        """
        SELECT agent_type, domain, reason, distress_signal,
               sample_size, created_at, expires_at
        FROM collective_suppressions
        WHERE active = TRUE
          AND (expires_at IS NULL OR expires_at > NOW())
        ORDER BY distress_signal DESC
        """
    )
    suppressions = [
        {
            "agent_type": r["agent_type"],
            "domain": r["domain"],
            "reason": r["reason"],
            "distress_signal": round(float(r["distress_signal"] or 0), 3),
            "sample_size": r["sample_size"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
        }
        for r in suppression_rows
    ]

    return {
        "collective_wisdom": wisdom,
        "active_suppressions": suppressions,
        "collective_voice": wisdom["collective_voice"] if wisdom else None,
        "privacy_note": "All data is aggregate. No individual user data is ever stored or returned here.",
    }


@router.get("/models")
async def get_model_candidates(
    x_admin_token: str = Header(default=""),
) -> Dict[str, Any]:
    """
    Show model candidates and the currently active model.
    Supports the Model Evolution System (Part 5).
    """
    _require_admin(x_admin_token)

    # Active model
    active_row = await fetchrow("SELECT value FROM system_config WHERE key = 'active_model'")
    active_model = active_row["value"] if active_row else "gpt-4o"

    # Shadow model (if any)
    shadow_row = await fetchrow("SELECT value FROM system_config WHERE key = 'shadow_model'")
    shadow_model = shadow_row["value"] if shadow_row else None

    # All candidates
    candidates = await fetch(
        """
        SELECT id, model_id, provider, discovered_at, eval_score, status, notes
        FROM model_candidates
        ORDER BY discovered_at DESC
        LIMIT 50
        """
    )

    return {
        "active_model": active_model,
        "shadow_model": shadow_model,
        "candidates": [
            {
                "id": str(row["id"]),
                "model_id": row["model_id"],
                "provider": row["provider"],
                "discovered_at": row["discovered_at"].isoformat() if row["discovered_at"] else None,
                "eval_score": round(float(row["eval_score"]), 3) if row["eval_score"] is not None else None,
                "status": row["status"],
                "notes": row["notes"],
            }
            for row in candidates
        ],
        "total_candidates": len(candidates),
    }


@router.get("/growth-metrics")
async def get_growth_metrics(
    x_admin_token: str = Header(default=""),
) -> Dict[str, Any]:
    """
    Ora's admin growth metrics endpoint.
    Used by ora_outreach/sales_optimizer.py to measure app growth
    and inform Ora's self-improvement loop.

    Returns:
    - new_users_7d: count of signups in last 7 days
    - new_paid_7d: count of new paid users in last 7 days
    - active_users_7d: users with activity in last 7 days
    - top_goals: most common goal themes (sampled, privacy-safe)
    - churn_risk: users with no activity in 5+ days (count only)
    - upgrade_candidates: free users with high engagement (count)
    """
    _require_admin(x_admin_token)

    # New signups last 7 days
    new_users_row = await fetchrow(
        "SELECT COUNT(*) as count FROM users WHERE created_at > NOW() - INTERVAL '7 days'"
    )
    new_users_7d = int(new_users_row["count"]) if new_users_row else 0

    # New paid users last 7 days
    new_paid_row = await fetchrow(
        """
        SELECT COUNT(*) as count FROM users
        WHERE subscription_tier IN ('explorer', 'sovereign', 'premium', 'paid')
          AND updated_at > NOW() - INTERVAL '7 days'
        """
    )
    new_paid_7d = int(new_paid_row["count"]) if new_paid_row else 0

    # Active users last 7 days
    active_row = await fetchrow(
        "SELECT COUNT(*) as count FROM users WHERE last_active > NOW() - INTERVAL '7 days'"
    )
    active_users_7d = int(active_row["count"]) if active_row else 0

    # Top goal themes (title words, privacy-safe aggregate)
    goal_rows = await fetch(
        """
        SELECT title, COUNT(*) as cnt
        FROM goals
        WHERE created_at > NOW() - INTERVAL '30 days'
          AND status = 'active'
        GROUP BY title
        ORDER BY cnt DESC
        LIMIT 10
        """
    )
    top_goals = [
        {"theme": row["title"][:50], "count": row["cnt"]}
        for row in goal_rows
    ]

    # Churn risk: users with no activity in 5+ days but active in last 30
    churn_row = await fetchrow(
        """
        SELECT COUNT(*) as count FROM users
        WHERE last_active < NOW() - INTERVAL '5 days'
          AND last_active > NOW() - INTERVAL '30 days'
        """
    )
    churn_risk_count = int(churn_row["count"]) if churn_row else 0

    # Upgrade candidates: free users with >5 feedback entries, active last 7 days
    upgrade_row = await fetchrow(
        """
        SELECT COUNT(DISTINCT u.id) as count
        FROM users u
        JOIN feedback f ON f.user_id = u.id
        WHERE u.subscription_tier NOT IN ('explorer', 'sovereign', 'premium', 'paid')
          AND u.last_active > NOW() - INTERVAL '7 days'
        GROUP BY u.id
        HAVING COUNT(f.id) > 5
        """
    )
    upgrade_candidates = int(upgrade_row["count"]) if upgrade_row else 0

    return {
        "new_users_7d": new_users_7d,
        "new_paid_7d": new_paid_7d,
        "active_users_7d": active_users_7d,
        "top_goals": top_goals,
        "churn_risk": churn_risk_count,
        "upgrade_candidates": upgrade_candidates,
        "generated_at": __import__('datetime').datetime.utcnow().isoformat(),
        "note": "All metrics are aggregate counts. No individual user data is returned.",
    }
