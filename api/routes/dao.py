"""
DAO API Routes — Ascension Technologies Contribution + Reward System

Public leaderboard, contribution submission, voting, and proposals.
All read endpoints are public. Write endpoints require auth.
"""

import asyncio
import logging
import json
import subprocess
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, status, Body
from pydantic import BaseModel

from core.database import fetchrow, fetch, execute, fetchval
from core.redis_client import redis_set, redis_get
from api.middleware import get_current_user_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dao", tags=["dao"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class RegisterContributorRequest(BaseModel):
    github_username: str
    display_name: Optional[str] = None
    telegram_username: Optional[str] = None
    email: Optional[str] = None
    bio: Optional[str] = None


class SubmitContributionRequest(BaseModel):
    contribution_type: str  # code, design, research, content, community, idea, feedback
    title: str
    description: str  # required — must describe what you did
    github_pr_url: Optional[str] = None  # optional, for code contributors
    external_link: Optional[str] = None  # link to design file, doc, video, etc.
    evidence_text: Optional[str] = None  # screenshots, notes, detailed proof of work


class SubmitProposalRequest(BaseModel):
    title: str
    description: str
    proposal_type: Optional[str] = None  # feature, governance, budget, direction


class VoteProposalRequest(BaseModel):
    vote: str  # "for" or "against"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TIER_ORDER = {"observer": 0, "contributor": 1, "builder": 2, "steward": 3, "founding_steward": 4}
VALID_CONTRIBUTION_TYPES = {
    "code", "agent", "design", "doc", "content", "research", "feedback", "community", "idea",
    "review", "ops", "security", "implemented_idea", "spec",
}


def _tier_for_cp(total_cp: int, founding: bool = False) -> str:
    if founding or total_cp >= 3000:
        return "steward"
    if total_cp >= 500:
        return "builder"
    if total_cp >= 100:
        return "contributor"
    return "observer"


def _tier_badge_for_cp(total_cp: int) -> str:
    if total_cp >= 3000:
        return "👑"
    if total_cp >= 500:
        return "🔨"
    if total_cp >= 100:
        return "⭐"
    return "👀"


def _tier_badge(tier: str, is_founding_steward: bool = False) -> str:
    if is_founding_steward:
        return "⚡"
    badges = {
        "observer": "○",
        "contributor": "◆",
        "builder": "⬡",
        "steward": "★",
    }
    return badges.get(tier, "○")


def _record_to_dict(row) -> Dict[str, Any]:
    """Convert asyncpg Record to plain dict."""
    return dict(row) if row else {}


def _serialize(d: Dict[str, Any]) -> Dict[str, Any]:
    """Convert UUIDs and datetimes for JSON serialization."""
    out = {}
    for k, v in d.items():
        if isinstance(v, UUID):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, bytes):
            out[k] = v.decode()
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# GET /api/dao/leaderboard
# ---------------------------------------------------------------------------

@router.get("/leaderboard")
async def get_leaderboard(limit: int = 20):
    """Public leaderboard — top contributors with tier, CP, and recent contribution."""
    # CP can come from two legacy systems:
    # - user_cp_balance (email/user-based rewards)
    # - contributors.total_cp (DAO task/PR rewards backed by cp_ledger)
    # Merge them so task submitters and legacy CP recipients both appear.
    balance_rows = await fetch(
        """
        SELECT
            u.id, u.email,
            COALESCE(u.profile->>'display_name', split_part(u.email, '@', 1)) AS display_name,
            cp.cp_balance, cp.total_cp_earned, cp.last_updated
        FROM user_cp_balance cp
        JOIN users u ON u.id = cp.user_id
        WHERE cp.total_cp_earned > 0
        """
    )

    contributor_rows = await fetch(
        """
        SELECT
            id, github_username, display_name, email,
            total_cp, tier, joined_at, is_founding_steward
        FROM contributors
        WHERE total_cp > 0
        """
    )

    def leaderboard_tier(total_cp: int, contributor_tier: Optional[str] = None) -> str:
        if contributor_tier and contributor_tier != "observer":
            return contributor_tier
        return "steward" if total_cp >= 3000 else "builder" if total_cp >= 500 else "contributor" if total_cp >= 100 else "observer"

    def badge_for(total_cp: int) -> str:
        return "👑" if total_cp >= 3000 else "⭐" if total_cp >= 500 else "🔨" if total_cp >= 100 else "👀"

    merged: Dict[str, Dict[str, Any]] = {}

    for row in balance_rows:
        email = (row["email"] or "").lower()
        key = f"email:{email}" if email else f"user:{row['id']}"
        total_cp = int(row["total_cp_earned"] or 0)
        merged[key] = {
            "id": str(row["id"]),
            "display_name": row["display_name"] or "Anonymous",
            "total_cp": total_cp,
            "cp_balance": int(row["cp_balance"] or 0),
            "tier": leaderboard_tier(total_cp),
            "is_founding_steward": total_cp >= 3000,
            "joined_at": row["last_updated"].isoformat() if row["last_updated"] else None,
        }

    for row in contributor_rows:
        email = (row["email"] or "").lower()
        github_username = (row["github_username"] or "").lower()
        key = f"email:{email}" if email else f"github:{github_username}" if github_username else f"contributor:{row['id']}"
        contributor_cp = int(row["total_cp"] or 0)
        existing = merged.get(key)

        if existing:
            total_cp = int(existing["total_cp"] or 0) + contributor_cp
            existing.update({
                "total_cp": total_cp,
                "cp_balance": int(existing["cp_balance"] or 0) + contributor_cp,
                "tier": leaderboard_tier(total_cp, row["tier"]),
                "is_founding_steward": bool(row["is_founding_steward"]) or total_cp >= 3000,
                "joined_at": existing["joined_at"] or (row["joined_at"].isoformat() if row["joined_at"] else None),
            })
        else:
            total_cp = contributor_cp
            merged[key] = {
                "id": str(row["id"]),
                "display_name": row["display_name"] or row["github_username"] or "Anonymous",
                "total_cp": total_cp,
                "cp_balance": total_cp,
                "tier": leaderboard_tier(total_cp, row["tier"]),
                "is_founding_steward": bool(row["is_founding_steward"]) or total_cp >= 3000,
                "joined_at": row["joined_at"].isoformat() if row["joined_at"] else None,
            }

    leaderboard = sorted(
        (entry for entry in merged.values() if int(entry["total_cp"] or 0) > 0),
        key=lambda entry: int(entry["total_cp"] or 0),
        reverse=True,
    )[:limit]

    for i, entry in enumerate(leaderboard):
        entry["rank"] = i + 1
        entry["tier_badge"] = badge_for(int(entry["total_cp"] or 0))

    return {
        "leaderboard": leaderboard,
        "total_contributors": len(merged),
        "total_cp_awarded": sum(int(entry["total_cp"] or 0) for entry in merged.values()),
    }


# ---------------------------------------------------------------------------
# GET /api/dao/contributions
# ---------------------------------------------------------------------------

@router.get("/contributions")
async def get_contributions(
    status_filter: Optional[str] = "accepted",
    limit: int = 50,
    offset: int = 0,
):
    """List contributions with Ora evaluations."""
    rows = await fetch(
        """
        SELECT
            c.id,
            c.contribution_type,
            c.title,
            c.description,
            c.github_pr_url,
            c.submitted_at,
            c.status,
            c.base_cp,
            c.multiplier,
            c.final_cp,
            c.ora_evaluation,
            c.ora_confidence,
            c.impact_data,
            c.community_upvotes,
            co.github_username,
            co.display_name,
            co.tier
        FROM contributions c
        JOIN contributors co ON co.id = c.contributor_id
        WHERE c.status = $1
        ORDER BY c.submitted_at DESC
        LIMIT $2 OFFSET $3
        """,
        status_filter,
        limit,
        offset,
    )

    contributions = []
    for row in rows:
        d = _serialize(_record_to_dict(row))
        d["github_avatar_url"] = f"https://github.com/{d['github_username']}.png?size=60"
        d["tier_badge"] = _tier_badge(d["tier"])
        # Parse impact_data if it's a string
        if isinstance(d.get("impact_data"), str):
            try:
                d["impact_data"] = json.loads(d["impact_data"])
            except Exception:
                pass
        contributions.append(d)

    return {"contributions": contributions}


# ---------------------------------------------------------------------------
# POST /api/dao/register
# ---------------------------------------------------------------------------

@router.post("/register", status_code=201)
async def register_contributor(
    body: RegisterContributorRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Register as a DAO contributor."""
    # Check if already registered
    existing = await fetchrow(
        "SELECT id FROM contributors WHERE github_username = $1",
        body.github_username.lower().strip(),
    )
    if existing:
        # Return existing contributor
        row = await fetchrow(
            "SELECT id, github_username, display_name, total_cp, tier, joined_at FROM contributors WHERE id = $1",
            existing["id"],
        )
        return {"contributor": _serialize(_record_to_dict(row)), "already_registered": True}

    # Create contributor
    row = await fetchrow(
        """
        INSERT INTO contributors (github_username, display_name, telegram_username, email, bio)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id, github_username, display_name, total_cp, tier, joined_at
        """,
        body.github_username.lower().strip(),
        body.display_name or body.github_username,
        body.telegram_username,
        body.email,
        body.bio,
    )
    logger.info(f"DaoAgent: new contributor registered: {body.github_username}")
    return {
        "contributor": _serialize(_record_to_dict(row)),
        "already_registered": False,
        "message": "Welcome to the Ascension DAO! Your contributions will be seen and valued.",
    }


# ---------------------------------------------------------------------------
# POST /api/dao/contribute
# ---------------------------------------------------------------------------

@router.post("/contribute", status_code=201)
async def submit_contribution(
    body: SubmitContributionRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Submit a contribution for Ora's review without requiring GitHub registration."""
    if body.contribution_type not in VALID_CONTRIBUTION_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"contribution_type must be one of: {', '.join(sorted(VALID_CONTRIBUTION_TYPES))}",
        )

    # Idempotent schema hardening for deployments that have not run the latest DB bootstrap yet.
    await execute("ALTER TABLE contributors ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id)")
    await execute("CREATE INDEX IF NOT EXISTS idx_contributors_user_id ON contributors(user_id)")
    await execute("ALTER TABLE contributions ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id)")
    await execute("ALTER TABLE contributions ADD COLUMN IF NOT EXISTS external_link TEXT")
    await execute("ALTER TABLE contributions ADD COLUMN IF NOT EXISTS evidence_text TEXT")

    user_uuid = UUID(user_id)

    contributor = await fetchrow(
        "SELECT id FROM contributors WHERE user_id = $1",
        user_uuid,
    )

    if not contributor:
        user_row = await fetchrow(
            "SELECT email, display_name, profile FROM users WHERE id = $1",
            user_uuid,
        )
        user_data = _record_to_dict(user_row)
        email = user_data.get("email") or ""
        profile = user_data.get("profile") or {}
        display = (
            user_data.get("display_name")
            or profile.get("display_name")
            or (email.split("@")[0] if email else f"user_{user_id[:8]}")
        )
        contributor = await fetchrow(
            """
            INSERT INTO contributors (github_username, display_name, email, tier, user_id)
            VALUES ($1, $2, $3, 'observer', $4)
            ON CONFLICT (github_username) DO UPDATE SET
                user_id = EXCLUDED.user_id,
                display_name = COALESCE(contributors.display_name, EXCLUDED.display_name),
                email = COALESCE(contributors.email, EXCLUDED.email)
            RETURNING id
            """,
            f"user_{user_id[:8]}",
            display,
            email,
            user_uuid,
        )

    row = await fetchrow(
        """
        INSERT INTO contributions (
            contributor_id, user_id, contribution_type, title, description, github_pr_url, external_link, evidence_text
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id, contribution_type, title, submitted_at, status
        """,
        contributor["id"],
        user_uuid,
        body.contribution_type,
        body.title,
        body.description,
        body.github_pr_url,
        body.external_link,
        body.evidence_text,
    )

    logger.info(f"DAO: new contribution submitted: '{body.title}' ({body.contribution_type})")

    # Award XP for submitting a contribution — encourages participation
    try:
        await execute(
            "INSERT INTO xp_log (user_id, amount, reason, ref_id) VALUES ($1, $2, $3, $4)",
            user_uuid, 50, "contribution_submit", row["id"],
        )
    except Exception:
        pass  # Non-critical

    return {
        "contribution": _serialize(_record_to_dict(row)),
        "message": "Contribution submitted! Ora will review within 24h.",
    }


# ---------------------------------------------------------------------------
# GET /api/dao/my-contributions
# ---------------------------------------------------------------------------

@router.get("/my-contributions")
async def my_contributions(user_id: str = Depends(get_current_user_id)):
    """Return the authenticated user's submitted contributions."""
    await execute("ALTER TABLE contributions ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id)")
    await execute("ALTER TABLE contributions ADD COLUMN IF NOT EXISTS external_link TEXT")
    await execute("ALTER TABLE contributions ADD COLUMN IF NOT EXISTS evidence_text TEXT")

    rows = await fetch(
        """
        SELECT id, contribution_type, title, description, github_pr_url, external_link,
               submitted_at, status, final_cp AS cp_awarded
        FROM contributions
        WHERE user_id = $1
        ORDER BY submitted_at DESC
        """,
        UUID(user_id),
    )
    return {"contributions": [_serialize(_record_to_dict(r)) for r in rows]}


# ---------------------------------------------------------------------------
# POST /api/dao/contribute/{github_username}  (with explicit contributor)
# ---------------------------------------------------------------------------

@router.post("/contribute/{github_username}", status_code=201)
async def submit_contribution_for_user(
    github_username: str,
    body: SubmitContributionRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Submit a contribution linked to a specific contributor (by github_username)."""
    contributor = await fetchrow(
        "SELECT id FROM contributors WHERE github_username = $1",
        github_username.lower().strip(),
    )
    if not contributor:
        raise HTTPException(status_code=404, detail=f"Contributor '{github_username}' not registered")

    if body.contribution_type not in VALID_CONTRIBUTION_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid contribution_type")

    row = await fetchrow(
        """
        INSERT INTO contributions (
            contributor_id, contribution_type, title, description, github_pr_url
        )
        VALUES ($1::uuid, $2, $3, $4, $5)
        RETURNING id, contribution_type, title, submitted_at, status
        """,
        str(contributor["id"]),
        body.contribution_type,
        body.title,
        body.description,
        body.github_pr_url,
    )

    logger.info(f"DAO: contribution submitted by {github_username}: '{body.title}'")
    return {
        "contribution": _serialize(_record_to_dict(row)),
        "message": "Contribution submitted. Ora will evaluate it within 24 hours.",
    }


# ---------------------------------------------------------------------------
# GET /api/dao/contributor/{github_username}
# ---------------------------------------------------------------------------

@router.get("/contributor/{github_username}")
async def get_contributor_profile(github_username: str):
    """Public contributor profile with full CP history."""
    contributor = await fetchrow(
        """
        SELECT id, github_username, display_name, telegram_username,
               total_cp, tier, joined_at, bio
        FROM contributors
        WHERE github_username = $1
        """,
        github_username.lower().strip(),
    )
    if not contributor:
        raise HTTPException(status_code=404, detail=f"Contributor '{github_username}' not found")

    contributor_dict = _serialize(_record_to_dict(contributor))
    contributor_dict["github_avatar_url"] = f"https://github.com/{github_username}.png?size=120"
    contributor_dict["tier_badge"] = _tier_badge(contributor_dict["tier"])

    # Get their contribution history
    contributions = await fetch(
        """
        SELECT id, contribution_type, title, submitted_at, status,
               base_cp, multiplier, final_cp, ora_evaluation, community_upvotes
        FROM contributions
        WHERE contributor_id = $1::uuid
        ORDER BY submitted_at DESC
        LIMIT 20
        """,
        str(contributor["id"]),
    )

    # CP ledger summary
    ledger = await fetch(
        """
        SELECT cp_amount, reason, created_at
        FROM cp_ledger
        WHERE contributor_id = $1::uuid
        ORDER BY created_at DESC
        LIMIT 10
        """,
        str(contributor["id"]),
    )

    return {
        "contributor": contributor_dict,
        "contributions": [_serialize(_record_to_dict(c)) for c in contributions],
        "cp_ledger": [_serialize(_record_to_dict(l)) for l in ledger],
    }


# ---------------------------------------------------------------------------
# POST /api/dao/vote/{contribution_id}
# ---------------------------------------------------------------------------

@router.post("/vote/{contribution_id}")
async def upvote_contribution(
    contribution_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Upvote a contribution (one vote per user — tracked via simple increment for now)."""
    row = await fetchrow(
        "SELECT id, community_upvotes FROM contributions WHERE id = $1::uuid",
        contribution_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Contribution not found")

    await execute(
        "UPDATE contributions SET community_upvotes = community_upvotes + 1, community_votes = community_votes + 1 WHERE id = $1::uuid",
        contribution_id,
    )
    new_count = (row["community_upvotes"] or 0) + 1
    return {"upvotes": new_count, "message": "Vote recorded"}


# ---------------------------------------------------------------------------
# GET /api/dao/proposals
# ---------------------------------------------------------------------------

@router.get("/proposals")
async def get_proposals(
    status_filter: Optional[str] = None,
    limit: int = 20,
):
    """List open DAO proposals."""
    if status_filter:
        rows = await fetch(
            """
            SELECT p.id, p.title, p.description, p.proposal_type, p.status,
                   p.votes_for, p.votes_against, p.created_at, p.closes_at, p.result_summary,
                   co.github_username, co.display_name, co.tier
            FROM dao_proposals p
            JOIN contributors co ON co.id = p.proposer_id
            WHERE p.status = $1
            ORDER BY p.created_at DESC
            LIMIT $2
            """,
            status_filter,
            limit,
        )
    else:
        rows = await fetch(
            """
            SELECT p.id, p.title, p.description, p.proposal_type, p.status,
                   p.votes_for, p.votes_against, p.created_at, p.closes_at, p.result_summary,
                   co.github_username, co.display_name, co.tier
            FROM dao_proposals p
            JOIN contributors co ON co.id = p.proposer_id
            ORDER BY p.created_at DESC
            LIMIT $1
            """,
            limit,
        )

    proposals = [_serialize(_record_to_dict(row)) for row in rows]
    return {"proposals": proposals}


# ---------------------------------------------------------------------------
# POST /api/dao/proposals
# ---------------------------------------------------------------------------

@router.post("/proposals", status_code=201)
async def submit_proposal(
    github_username: str = Body(...),
    body: SubmitProposalRequest = Body(...),
    user_id: str = Depends(get_current_user_id),
):
    """Submit a DAO proposal. Requires builder+ tier."""
    contributor = await fetchrow(
        "SELECT id, tier FROM contributors WHERE github_username = $1",
        github_username.lower().strip(),
    )
    if not contributor:
        raise HTTPException(status_code=403, detail="You must be a registered contributor to submit proposals")

    tier = contributor["tier"]
    if TIER_ORDER.get(tier, 0) < TIER_ORDER["builder"]:
        raise HTTPException(
            status_code=403,
            detail=f"Builder tier or higher required to submit proposals. Your tier: {tier}",
        )

    closes_at = datetime.now(timezone.utc) + timedelta(days=14)
    row = await fetchrow(
        """
        INSERT INTO dao_proposals (proposer_id, title, description, proposal_type, closes_at)
        VALUES ($1::uuid, $2, $3, $4, $5)
        RETURNING id, title, status, created_at, closes_at
        """,
        str(contributor["id"]),
        body.title,
        body.description,
        body.proposal_type,
        closes_at,
    )

    logger.info(f"DAO: proposal submitted by {github_username}: '{body.title}'")
    return {
        "proposal": _serialize(_record_to_dict(row)),
        "message": "Proposal submitted. Voting opens immediately.",
    }


# ---------------------------------------------------------------------------
# POST /api/dao/proposals/{proposal_id}/vote
# ---------------------------------------------------------------------------

@router.post("/proposals/{proposal_id}/vote")
async def vote_on_proposal(
    proposal_id: str,
    github_username: str = Body(...),
    vote: str = Body(...),  # "for" or "against"
    user_id: str = Depends(get_current_user_id),
):
    """Vote on a DAO proposal. Requires builder+ tier."""
    contributor = await fetchrow(
        "SELECT id, tier FROM contributors WHERE github_username = $1",
        github_username.lower().strip(),
    )
    if not contributor:
        raise HTTPException(status_code=403, detail="Must be a registered contributor to vote")

    tier = contributor["tier"]
    if TIER_ORDER.get(tier, 0) < TIER_ORDER["builder"]:
        raise HTTPException(
            status_code=403,
            detail=f"Builder tier or higher required to vote on proposals. Your tier: {tier}",
        )

    proposal = await fetchrow(
        "SELECT id, status FROM dao_proposals WHERE id = $1::uuid",
        proposal_id,
    )
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if proposal["status"] not in ("open", "voting"):
        raise HTTPException(status_code=400, detail=f"Proposal is not open for voting (status: {proposal['status']})")

    if vote == "for":
        await execute(
            "UPDATE dao_proposals SET votes_for = votes_for + 1, status = 'voting' WHERE id = $1::uuid",
            proposal_id,
        )
    elif vote == "against":
        await execute(
            "UPDATE dao_proposals SET votes_against = votes_against + 1, status = 'voting' WHERE id = $1::uuid",
            proposal_id,
        )
    else:
        raise HTTPException(status_code=400, detail="vote must be 'for' or 'against'")

    updated = await fetchrow(
        "SELECT votes_for, votes_against FROM dao_proposals WHERE id = $1::uuid",
        proposal_id,
    )
    return {
        "votes_for": updated["votes_for"],
        "votes_against": updated["votes_against"],
        "your_vote": vote,
    }


# ---------------------------------------------------------------------------
# GET /api/dao/ltv-stats
# ---------------------------------------------------------------------------

@router.get("/ltv-stats")
async def get_ltv_stats(limit: int = 50):
    """Show which contributions are still generating monthly LTV CP."""
    rows = await fetch(
        """
        SELECT
            c.id,
            c.contribution_type,
            c.title,
            c.submitted_at,
            c.final_cp,
            c.ltv_cp_total,
            c.ltv_monthly_rate,
            c.months_active,
            c.ltv_last_evaluated_at,
            c.is_ltv_active,
            co.github_username,
            co.display_name,
            co.tier,
            co.is_founding_steward
        FROM contributions c
        JOIN contributors co ON co.id = c.contributor_id
        WHERE c.status = 'accepted'
          AND c.is_ltv_active = TRUE
        ORDER BY c.ltv_monthly_rate DESC
        LIMIT $1
        """,
        limit,
    )

    total_ltv_active = await fetchval(
        "SELECT COUNT(*) FROM contributions WHERE status = 'accepted' AND is_ltv_active = TRUE"
    )
    total_ltv_cp_distributed = await fetchval(
        "SELECT COALESCE(SUM(ltv_cp_total), 0) FROM contributions WHERE status = 'accepted'"
    )

    contributions = []
    for row in rows:
        d = _serialize(_record_to_dict(row))
        d["github_avatar_url"] = f"https://github.com/{d['github_username']}.png?size=60"
        contributions.append(d)

    return {
        "active_ltv_contributions": contributions,
        "total_ltv_active": total_ltv_active,
        "total_ltv_cp_distributed": total_ltv_cp_distributed,
    }


# ---------------------------------------------------------------------------
# GET /api/dao/contributor/{github_username}/ltv
# ---------------------------------------------------------------------------

@router.get("/contributor/{github_username}/ltv")
async def get_contributor_ltv(github_username: str):
    """Personal LTV history — month-by-month CP accrual for a contributor."""
    contributor = await fetchrow(
        """
        SELECT id, github_username, display_name, total_cp, tier,
               is_founding_steward, founding_steward_number
        FROM contributors
        WHERE github_username = $1
        """,
        github_username.lower().strip(),
    )
    if not contributor:
        raise HTTPException(status_code=404, detail=f"Contributor '{github_username}' not found")

    contributor_dict = _serialize(_record_to_dict(contributor))

    # All LTV ledger entries
    ltv_ledger = await fetch(
        """
        SELECT l.cp_amount, l.reason, l.created_at,
               c.title as contribution_title, c.contribution_type
        FROM cp_ledger l
        LEFT JOIN contributions c ON c.id = l.contribution_id
        WHERE l.contributor_id = $1::uuid
          AND l.reason ILIKE 'LTV month%%'
        ORDER BY l.created_at DESC
        LIMIT 100
        """,
        str(contributor["id"]),
    )

    # Active LTV contributions
    active_contributions = await fetch(
        """
        SELECT id, contribution_type, title, final_cp, ltv_cp_total,
               ltv_monthly_rate, months_active, is_ltv_active, ltv_last_evaluated_at
        FROM contributions
        WHERE contributor_id = $1::uuid
          AND status = 'accepted'
        ORDER BY is_ltv_active DESC, ltv_monthly_rate DESC
        LIMIT 30
        """,
        str(contributor["id"]),
    )

    # Total LTV CP earned (vs initial contribution CP)
    total_initial_cp = await fetchval(
        "SELECT COALESCE(SUM(final_cp), 0) FROM contributions WHERE contributor_id = $1::uuid AND status = 'accepted'",
        str(contributor["id"]),
    )
    total_ltv_cp = await fetchval(
        "SELECT COALESCE(SUM(ltv_cp_total), 0) FROM contributions WHERE contributor_id = $1::uuid AND status = 'accepted'",
        str(contributor["id"]),
    )

    # Month-by-month timeline (last 12 months)
    monthly_timeline = await fetch(
        """
        SELECT
            DATE_TRUNC('month', l.created_at) as month,
            SUM(l.cp_amount) as cp_earned
        FROM cp_ledger l
        WHERE l.contributor_id = $1::uuid
          AND l.created_at > NOW() - INTERVAL '12 months'
        GROUP BY DATE_TRUNC('month', l.created_at)
        ORDER BY month ASC
        """,
        str(contributor["id"]),
    )

    return {
        "contributor": contributor_dict,
        "ltv_summary": {
            "total_initial_cp": int(total_initial_cp or 0),
            "total_ltv_cp": int(total_ltv_cp or 0),
            "total_cp": contributor_dict["total_cp"],
        },
        "active_contributions": [_serialize(_record_to_dict(c)) for c in active_contributions],
        "ltv_ledger": [_serialize(_record_to_dict(l)) for l in ltv_ledger],
        "monthly_timeline": [
            {"month": row["month"].isoformat() if row["month"] else None, "cp_earned": int(row["cp_earned"] or 0)}
            for row in monthly_timeline
        ],
    }


# ---------------------------------------------------------------------------
# GET /api/dao/founding-stewards
# ---------------------------------------------------------------------------

@router.get("/founding-stewards")
async def get_founding_stewards():
    """The first 10 people to reach Steward tier — permanent hall of fame."""
    rows = await fetch(
        """
        SELECT
            co.id,
            co.github_username,
            co.display_name,
            co.telegram_username,
            co.total_cp,
            co.tier,
            co.joined_at,
            co.bio,
            co.founding_steward_number,
            (
                SELECT COUNT(*) FROM contributions
                WHERE contributor_id = co.id AND status = 'accepted'
            ) AS accepted_contributions,
            (
                SELECT COALESCE(SUM(ltv_cp_total), 0) FROM contributions
                WHERE contributor_id = co.id AND status = 'accepted'
            ) AS total_ltv_cp
        FROM contributors co
        WHERE co.is_founding_steward = TRUE
        ORDER BY co.founding_steward_number ASC
        """
    )

    stewards = []
    for row in rows:
        d = _serialize(_record_to_dict(row))
        d["github_avatar_url"] = f"https://github.com/{d['github_username']}.png?size=120"
        stewards.append(d)

    slots_remaining = max(0, 10 - len(stewards))

    return {
        "founding_stewards": stewards,
        "total": len(stewards),
        "slots_remaining": slots_remaining,
        "description": (
            "The first 10 people to reach Steward tier (3,000 CP) are permanently recognized "
            "as Founding Stewards of Ascension Technologies DAO. "
            "Founding Stewards help govern the direction of Ascension Technologies and Connectome."
        ),
    }


# ---------------------------------------------------------------------------
# GET /api/dao/stats  (bonus: system-wide stats)
# ---------------------------------------------------------------------------

@router.get("/stats")
async def get_dao_stats():
    """Public DAO system stats."""
    total_contributors = await fetchval("SELECT COUNT(*) FROM contributors") or 0
    total_contributions = await fetchval("SELECT COUNT(*) FROM contributions WHERE status = 'accepted'") or 0
    total_cp = await fetchval("SELECT COALESCE(SUM(cp_amount), 0) FROM cp_ledger") or 0
    open_proposals = await fetchval("SELECT COUNT(*) FROM dao_proposals WHERE status IN ('open', 'voting')") or 0

    tier_counts = await fetch(
        "SELECT tier, COUNT(*) as count FROM contributors GROUP BY tier"
    )
    tiers = {row["tier"]: row["count"] for row in tier_counts}

    return {
        "total_contributors": total_contributors,
        "total_accepted_contributions": total_contributions,
        "total_cp_awarded": total_cp,
        "open_proposals": open_proposals,
        "tiers": tiers,
    }


# ---------------------------------------------------------------------------
# Hardcoded high-priority DAO tasks
# ---------------------------------------------------------------------------

HARDCODED_TASKS = [
    {
        "id": "task-001",
        "title": "Add a new Ora coaching module",
        "cp_reward": 500,
        "difficulty": "medium",
        "skills": ["python", "fastapi"],
        "description": "Build a new coaching agent module in ora/agents/",
        "source": "internal",
    },
    {
        "id": "task-002",
        "title": "Improve mobile Feed UI",
        "cp_reward": 300,
        "difficulty": "easy",
        "skills": ["react-native"],
        "description": "Enhance card animations and transitions in mobile app",
        "source": "internal",
    },
    {
        "id": "task-003",
        "title": "Write onboarding docs",
        "cp_reward": 200,
        "difficulty": "easy",
        "skills": ["writing"],
        "description": "Write clear contributor docs for new devs",
        "source": "internal",
    },
]


# ---------------------------------------------------------------------------
# GET /api/dao/tasks
# ---------------------------------------------------------------------------

@router.get("/tasks")
async def get_open_tasks():
    """Return open contribution tasks (hardcoded + GitHub issues)."""
    tasks = list(HARDCODED_TASKS)  # copy

    # Pull GitHub issues labelled 'good first issue' or 'bounty' from both repos
    repos = ["AvielCarlos/connectome-backend", "AvielCarlos/connectome-web"]
    difficulty_map = {"good first issue": "easy", "bounty": "medium"}

    for repo in repos:
        for label in ["good first issue", "bounty"]:
            try:
                result = subprocess.run(
                    [
                        "gh", "issue", "list",
                        "--repo", repo,
                        "--label", label,
                        "--state", "open",
                        "--json", "number,title,body,url,labels",
                        "--limit", "10",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    issues = json.loads(result.stdout)
                    for issue in issues:
                        issue_labels = [lb.get("name", "") for lb in (issue.get("labels") or [])]
                        diff = next(
                            (difficulty_map[l] for l in issue_labels if l in difficulty_map),
                            "medium",
                        )
                        tasks.append({
                            "id": f"gh-{repo.split('/')[1]}-{issue['number']}",
                            "title": issue["title"],
                            "cp_reward": 300 if diff == "medium" else 150,
                            "difficulty": diff,
                            "skills": [],
                            "description": (issue.get("body") or "")[:400],
                            "github_url": issue["url"],
                            "source": "github",
                            "repo": repo,
                        })
            except Exception as exc:
                logger.warning(f"DAO tasks: could not fetch GitHub issues for {repo}/{label}: {exc}")

    return {"tasks": tasks, "total": len(tasks)}


# ---------------------------------------------------------------------------
# POST /api/dao/claim/{task_id}
# ---------------------------------------------------------------------------

@router.post("/claim/{task_id}")
async def claim_task(
    task_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Claim a task. Stored in Redis with 48h TTL."""
    redis_key = f"dao:claimed:{task_id}"
    existing = await redis_get(redis_key)
    if existing:
        if existing == user_id:
            return {"status": "already_claimed_by_you", "task_id": task_id}
        raise HTTPException(
            status_code=409,
            detail="This task has already been claimed by another contributor.",
        )

    await redis_set(redis_key, user_id, ttl_seconds=48 * 3600)
    logger.info(f"DAO: task {task_id} claimed by user {user_id}")
    return {
        "status": "claimed",
        "task_id": task_id,
        "message": "Task claimed! You have 48 hours to submit a PR. When done, use the Submit button.",
    }


# ---------------------------------------------------------------------------
# POST /api/dao/submit/{task_id}
# ---------------------------------------------------------------------------

class TaskSubmitRequest(BaseModel):
    pr_url: str
    notes: Optional[str] = None


@router.post("/submit/{task_id}")
async def submit_task(
    task_id: str,
    body: TaskSubmitRequest,
    user_id: str = Depends(get_current_user_id),
):
    """
    Submit a completed task:
    - Create a GitHub issue in connectome-backend for tracking
    - Do not award CP immediately; final CP is awarded only after review/merge
    """
    # Create a GitHub issue to track the submission
    issue_title = f"DAO submission: {task_id}"
    issue_body = (
        f"**Task ID:** {task_id}\n"
        f"**PR URL:** {body.pr_url}\n"
        f"**Submitted by:** user:{user_id}\n"
        f"**Notes:** {body.notes or 'N/A'}\n\n"
        "_This issue was automatically created by the DAO submission system. "
        "Ora will review and award final CP on merge._"
    )
    try:
        subprocess.run(
            [
                "gh", "issue", "create",
                "--repo", "AvielCarlos/connectome-backend",
                "--title", issue_title,
                "--body", issue_body,
                "--label", "dao-submission",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:
        logger.warning(f"DAO submit: could not create GitHub issue: {exc}")

    # No immediate CP for task submissions.
    # Reviewed, merged, or explicitly adopted work earns CP; raw submissions and ideas do not.
    return {
        "status": "submitted",
        "task_id": task_id,
        "cp_awarded": 0,
        "message": "Submission received! CP will be awarded when your PR is reviewed and merged.",
    }


# ---------------------------------------------------------------------------
# GET /api/dao/cp/history  — User's CP transaction history
# ---------------------------------------------------------------------------

@router.get("/cp/history")
async def get_cp_history(
    limit: int = 50,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """
    Return the authenticated user's CP transaction history.
    Ordered newest-first. Includes current balance summary.
    """
    uid = UUID(user_id)

    try:
        rows = await fetch(
            """
            SELECT id, amount, reason, reference_id, created_at
            FROM cp_transactions
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            uid,
            limit,
        )
    except Exception as e:
        logger.warning(f"cp_transactions query failed (table may not exist yet): {e}")
        rows = []

    # Also get current balance
    balance_row = await fetchrow(
        "SELECT cp_balance, total_cp_earned FROM user_cp_balance WHERE user_id = $1",
        uid,
    )
    cp_balance = int(balance_row["cp_balance"] or 0) if balance_row else 0
    total_earned = int(balance_row["total_cp_earned"] or 0) if balance_row else 0

    transactions = []
    for r in rows:
        tx = dict(r)
        tx["id"] = str(tx["id"])
        if tx.get("created_at") and hasattr(tx["created_at"], "isoformat"):
            tx["created_at"] = tx["created_at"].isoformat()
        transactions.append(tx)

    return {
        "cp_balance": cp_balance,
        "total_cp_earned": total_earned,
        "transactions": transactions,
        "count": len(transactions),
    }
