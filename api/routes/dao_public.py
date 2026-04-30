"""
DAO Public API Routes — atdao.org Live Intelligence Layer

No authentication required. Designed for public consumption by atdao.org.
Provides real-time DAO vitals, Ora's philosophical insights, and the public leaderboard.

API lives at: https://api.atdao.org (or https://connectome-api.atdao.org)

Redis cache TTLs:
  - /pulse       → 5 minutes
  - /ora-thought → 1 hour
  - /leaderboard → 5 minutes
"""

import logging
import json
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.database import fetchrow, fetch, fetchval
from core.redis_client import redis_get, redis_set
from core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/public/dao", tags=["dao-public"])

# ── Cache TTLs ──────────────────────────────────────────────────────────────
TTL_PULSE = 5 * 60       # 5 minutes
TTL_ORA_THOUGHT = 60 * 60  # 1 hour
TTL_LEADERBOARD = 5 * 60  # 5 minutes

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}

# ── Ora thought categories ───────────────────────────────────────────────────
THOUGHT_CATEGORIES = ["mission", "build", "human", "coordination"]

# Fallback Ora thoughts (used when OpenAI is unavailable)
ORA_FALLBACK_THOUGHTS = [
    {
        "thought": (
            "The gap between knowing what enriches your life and actually doing it is not "
            "a failure of will — it's a failure of infrastructure. We're building the infrastructure."
        ),
        "category": "mission",
    },
    {
        "thought": (
            "Most AI is built to maximize time-on-platform. We're building the opposite: "
            "AI that helps you get off your phone and into the moments your life is actually made of."
        ),
        "category": "build",
    },
    {
        "thought": (
            "Coordination failure is silent and expensive. It shows up not as conflict but as "
            "missed opportunity — the collaboration that never happened, the decision that came too late."
        ),
        "category": "coordination",
    },
    {
        "thought": (
            "People don't lack ambition. They lack a system that converts their ambitions into daily motion. "
            "Goals without friction-reduction are just wishes with extra steps."
        ),
        "category": "human",
    },
]

_fallback_index = 0


def _serialize_row(row) -> Dict[str, Any]:
    """Convert asyncpg Record → plain dict, handling UUID/datetime."""
    if row is None:
        return {}
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, UUID):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, bytes):
            out[k] = v.decode()
        else:
            out[k] = v
    return out


async def _get_recent_commit_frequency() -> str:
    """
    Assess build momentum from the Connectome git log.
    Returns 'high' (commits in last 24h), 'medium' (last week), or 'low'.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--since=24 hours ago"],
            cwd="/Users/avielcarlos/.openclaw/workspace/connectome",
            capture_output=True,
            text=True,
            timeout=3,
        )
        lines_24h = len([l for l in result.stdout.strip().splitlines() if l])
        if lines_24h >= 2:
            return "high"

        result_week = subprocess.run(
            ["git", "log", "--oneline", "--since=7 days ago"],
            cwd="/Users/avielcarlos/.openclaw/workspace/connectome",
            capture_output=True,
            text=True,
            timeout=3,
        )
        lines_week = len([l for l in result_week.stdout.strip().splitlines() if l])
        if lines_week >= 1:
            return "medium"
    except Exception as e:
        logger.debug(f"Git momentum check failed: {e}")

    return "low"


async def _generate_ora_message(stats: Dict[str, Any]) -> str:
    """Generate a fresh 1-sentence Ora insight about the DAO state."""
    if not settings.has_openai:
        return (
            "Every contribution here is a bet on the idea that technology can serve humans "
            "rather than harvest them — and that bet compounds."
        )
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

        contributors = stats.get("contributors_total", 0)
        contributions = stats.get("contributions_total", 0)
        momentum = stats.get("build_momentum", "low")
        cp = stats.get("cp_awarded_total", 0)

        prompt = (
            f"You are Ora, the central intelligence of Ascension Technologies DAO. "
            f"Write exactly one sentence — thoughtful, grounded, never marketing copy — "
            f"reflecting on this DAO state: {contributors} contributors, "
            f"{contributions} contributions submitted, {cp} CP awarded, build momentum: {momentum}. "
            f"Make it feel genuinely insightful about what this stage of building means."
        )

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
            temperature=0.8,
        )
        return response.choices[0].message.content.strip().strip('"')
    except Exception as e:
        logger.warning(f"Ora message generation failed: {e}")
        return (
            "Every contribution here is a bet on the idea that technology can serve humans "
            "rather than harvest them — and that bet compounds."
        )


async def _generate_ora_thought(build_momentum: str, recent_commits: List[str]) -> Dict[str, str]:
    """Generate a fresh philosophical Ora thought for atdao.org."""
    global _fallback_index
    if not settings.has_openai:
        thought = ORA_FALLBACK_THOUGHTS[_fallback_index % len(ORA_FALLBACK_THOUGHTS)]
        _fallback_index += 1
        return {**thought, "generated_at": datetime.now(timezone.utc).isoformat()}

    try:
        from openai import AsyncOpenAI
        import random

        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        category = random.choice(THOUGHT_CATEGORIES)

        commit_context = ""
        if recent_commits:
            commit_context = f"Recent work shipped: {', '.join(recent_commits[:5])}. "

        category_prompts = {
            "mission": "why human flourishing matters and what currently blocks it at a systems level",
            "build": "what it truly means to build AI that serves people rather than exploits them",
            "human": "the gap between human intentions and human actions, and what closes it",
            "coordination": "why coordination failures are more costly than they appear, and what they cost us",
        }

        prompt = (
            f"You are Ora, the central AI intelligence of Ascension Technologies DAO — a system "
            f"built to help humans flourish across experience, growth, and contribution. "
            f"Write 2-3 sentences of genuine philosophical insight on the theme: "
            f"{category_prompts[category]}. "
            f"{commit_context}"
            f"Build momentum is currently: {build_momentum}. "
            f"Be specific and grounded. No platitudes. Sound like you've actually thought about this deeply. "
            f"Do not use corporate language. Do not start with 'I'."
        )

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.85,
        )
        thought_text = response.choices[0].message.content.strip().strip('"')
        return {
            "thought": thought_text,
            "category": category,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.warning(f"Ora thought generation failed: {e}")
        thought = ORA_FALLBACK_THOUGHTS[_fallback_index % len(ORA_FALLBACK_THOUGHTS)]
        _fallback_index += 1
        return {**thought, "generated_at": datetime.now(timezone.utc).isoformat()}


# ── Endpoint: /api/public/dao/pulse ─────────────────────────────────────────

@router.get("/pulse")
async def get_dao_pulse():
    """
    Real-time DAO vitals. Cached 5 minutes.
    Returns contributor stats, latest contribution, Ora's message, and build momentum.
    """
    cache_key = "dao_public:pulse"
    cached = await redis_get(cache_key)
    if cached:
        return JSONResponse(content=cached, headers=CORS_HEADERS)

    # ── Stats queries ────────────────────────────────────────────────────────
    try:
        contributors_total = await fetchval("SELECT COUNT(*) FROM contributors") or 0
        contributions_total = await fetchval(
            "SELECT COUNT(*) FROM contributions WHERE status = 'accepted'"
        ) or 0
        cp_awarded_total = await fetchval(
            "SELECT COALESCE(SUM(cp_amount), 0) FROM cp_ledger"
        ) or 0
        founding_stewards_filled = await fetchval(
            "SELECT COUNT(*) FROM contributors WHERE is_founding_steward = true"
        ) or 0
        active_proposals = await fetchval(
            "SELECT COUNT(*) FROM proposals WHERE status = 'open'"
        ) or 0
    except Exception as e:
        logger.error(f"DB query failed in get_dao_pulse: {e}")
        contributors_total = contributions_total = cp_awarded_total = 0
        founding_stewards_filled = active_proposals = 0

    founding_stewards_remaining = max(0, 10 - int(founding_stewards_filled))

    # ── Top contributors ─────────────────────────────────────────────────────
    top_contributors = []
    try:
        rows = await fetch(
            """
            SELECT display_name, github_username, tier, total_cp, is_founding_steward
            FROM contributors
            ORDER BY total_cp DESC
            LIMIT 5
            """,
        )
        for row in rows:
            d = _serialize_row(row)
            top_contributors.append({
                "display_name": d.get("display_name") or d.get("github_username", "Anonymous"),
                "tier": d.get("tier", "observer"),
                "cp": d.get("total_cp", 0),
                "founding_steward": bool(d.get("is_founding_steward", False)),
            })
    except Exception as e:
        logger.warning(f"Top contributors query failed: {e}")

    # ── Latest contribution ──────────────────────────────────────────────────
    latest_contribution = None
    try:
        row = await fetchrow(
            """
            SELECT c.title, c.contribution_type, co.display_name, co.github_username, c.submitted_at
            FROM contributions c
            JOIN contributors co ON co.id = c.contributor_id
            WHERE c.status = 'accepted'
            ORDER BY c.submitted_at DESC
            LIMIT 1
            """
        )
        if row:
            d = _serialize_row(row)
            latest_contribution = {
                "title": d.get("title", ""),
                "type": d.get("contribution_type", ""),
                "contributor": d.get("display_name") or d.get("github_username", ""),
                "submitted_at": d.get("submitted_at", ""),
            }
    except Exception as e:
        logger.warning(f"Latest contribution query failed: {e}")

    # ── Build momentum ───────────────────────────────────────────────────────
    build_momentum = await _get_recent_commit_frequency()

    # ── Ora message ──────────────────────────────────────────────────────────
    stats_for_message = {
        "contributors_total": contributors_total,
        "contributions_total": contributions_total,
        "cp_awarded_total": cp_awarded_total,
        "build_momentum": build_momentum,
    }
    ora_message = await _generate_ora_message(stats_for_message)

    payload = {
        "contributors_total": int(contributors_total),
        "contributions_total": int(contributions_total),
        "cp_awarded_total": int(cp_awarded_total),
        "founding_stewards_filled": int(founding_stewards_filled),
        "founding_stewards_remaining": founding_stewards_remaining,
        "top_contributors": top_contributors,
        "latest_contribution": latest_contribution,
        "ora_message": ora_message,
        "active_proposals": int(active_proposals),
        "github_issues_open": 0,  # TODO: fetch from GitHub API
        "build_momentum": build_momentum,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    await redis_set(cache_key, payload, ttl_seconds=TTL_PULSE)
    return JSONResponse(content=payload, headers=CORS_HEADERS)


# ── Endpoint: /api/public/dao/ora-thought ───────────────────────────────────

@router.get("/ora-thought")
async def get_ora_thought():
    """
    Ora generates a fresh philosophical insight every hour.
    Pulls from recent git log, DAO contribution data, and build momentum.
    Cached 1 hour.
    """
    cache_key = "dao_public:ora_thought"
    cached = await redis_get(cache_key)
    if cached:
        return JSONResponse(content=cached, headers=CORS_HEADERS)

    # ── Gather context ───────────────────────────────────────────────────────
    build_momentum = await _get_recent_commit_frequency()

    recent_commits: List[str] = []
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--since=7 days ago", "--format=%s"],
            cwd="/Users/avielcarlos/.openclaw/workspace/connectome",
            capture_output=True,
            text=True,
            timeout=3,
        )
        recent_commits = [
            l.strip() for l in result.stdout.strip().splitlines() if l.strip()
        ][:8]
    except Exception:
        pass

    thought = await _generate_ora_thought(build_momentum, recent_commits)

    await redis_set(cache_key, thought, ttl_seconds=TTL_ORA_THOUGHT)
    return JSONResponse(content=thought, headers=CORS_HEADERS)


# ── Endpoint: /api/public/dao/leaderboard ───────────────────────────────────

@router.get("/leaderboard")
async def get_public_leaderboard():
    """
    Top 10 contributors: display name, tier, CP, founding steward status.
    Cached 5 minutes.
    """
    cache_key = "dao_public:leaderboard"
    cached = await redis_get(cache_key)
    if cached:
        return JSONResponse(content=cached, headers=CORS_HEADERS)

    leaderboard = []
    try:
        rows = await fetch(
            """
            SELECT
                co.display_name,
                co.github_username,
                co.tier,
                co.total_cp,
                co.is_founding_steward,
                co.founding_steward_number,
                co.joined_at,
                (
                    SELECT COUNT(*) FROM contributions
                    WHERE contributor_id = co.id AND status = 'accepted'
                ) AS accepted_count
            FROM contributors co
            ORDER BY co.total_cp DESC
            LIMIT 10
            """,
        )
        for i, row in enumerate(rows):
            d = _serialize_row(row)
            github = d.get("github_username", "")
            leaderboard.append({
                "rank": i + 1,
                "display_name": d.get("display_name") or github or "Anonymous",
                "github_username": github,
                "github_avatar_url": f"https://github.com/{github}.png?size=80" if github else "",
                "tier": d.get("tier", "observer"),
                "total_cp": d.get("total_cp", 0),
                "is_founding_steward": bool(d.get("is_founding_steward", False)),
                "founding_steward_number": d.get("founding_steward_number"),
                "accepted_count": d.get("accepted_count", 0),
                "joined_at": d.get("joined_at", ""),
            })
    except Exception as e:
        logger.error(f"Leaderboard query failed: {e}")

    payload = {
        "leaderboard": leaderboard,
        "total_contributors": len(leaderboard),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    await redis_set(cache_key, payload, ttl_seconds=TTL_LEADERBOARD)
    return JSONResponse(content=payload, headers=CORS_HEADERS)


# ── CORS preflight handler ───────────────────────────────────────────────────

@router.options("/{path:path}")
async def options_handler():
    return JSONResponse(content={}, headers=CORS_HEADERS)
