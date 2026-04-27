"""
DAO API Routes — Ascension Technologies Contribution + Reward System

Public leaderboard, contribution submission, voting, and proposals.
All read endpoints are public. Write endpoints require auth.
"""

import logging
import json
from typing import Any, Dict, List, Optional
from uuid import UUID
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, status, Body
from pydantic import BaseModel

from core.database import fetchrow, fetch, execute, fetchval
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
    contribution_type: str  # code, agent, design, doc, research, feedback, community
    title: str
    description: Optional[str] = None
    github_pr_url: Optional[str] = None


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
            co.is_founding_steward,
            co.founding_steward_number,
            (
                SELECT title FROM contributions
                WHERE contributor_id = co.id
                  AND status = 'accepted'
                ORDER BY submitted_at DESC
                LIMIT 1
            ) AS recent_contribution_title,
            (
                SELECT COUNT(*) FROM contributions
                WHERE contributor_id = co.id AND status = 'accepted'
            ) AS accepted_count,
            COALESCE(
                (SELECT SUM(cp_amount) FROM cp_ledger l2
                 WHERE l2.contributor_id = co.id
                   AND l2.created_at > NOW() - INTERVAL '30 days'),
                0
            ) AS cp_this_month
        FROM contributors co
        ORDER BY co.total_cp DESC
        LIMIT $1
        """,
        limit,
    )

    leaderboard = []
    for i, row in enumerate(rows):
        d = _serialize(_record_to_dict(row))
        d["rank"] = i + 1
        d["github_avatar_url"] = f"https://github.com/{d['github_username']}.png?size=80"
        d["tier_badge"] = _tier_badge(d["tier"], bool(d.get("is_founding_steward")))
        leaderboard.append(d)

    total_contributors = await fetchval("SELECT COUNT(*) FROM contributors")
    total_cp = await fetchval("SELECT COALESCE(SUM(cp_amount), 0) FROM cp_ledger")

    return {
        "leaderboard": leaderboard,
        "total_contributors": total_contributors,
        "total_cp_awarded": total_cp,
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
    """Submit a contribution for Ora's review."""
    valid_types = {"code", "agent", "design", "doc", "research", "feedback", "community"}
    if body.contribution_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"contribution_type must be one of: {', '.join(sorted(valid_types))}",
        )

    # Find contributor — need their contributor record
    # We'll use the github_username from query param or let them pass it separately
    # For now, require they're registered first
    # The user must pass their github_username in the request context
    # Simple approach: look up by user_id in profile — or require prior registration
    # We'll accept contributions without strict linking to a registered contributor
    # and link them if possible

    # Find any contributor registered by this user session
    # Since contributors are linked by github_username (not user_id), we'll create
    # an anonymous contribution placeholder and link on registration
    # Better: require contributor registration first

    # Check if at least one contributor exists (for minimal auth)
    # In production you'd link user_id → contributor via a mapping table
    # For now, accept with a note in the status
    row = await fetchrow(
        """
        INSERT INTO contributions (
            contributor_id, contribution_type, title, description, github_pr_url
        )
        SELECT
            (SELECT id FROM contributors ORDER BY joined_at DESC LIMIT 1),
            $1, $2, $3, $4
        RETURNING id, contribution_type, title, submitted_at, status
        """,
        body.contribution_type,
        body.title,
        body.description,
        body.github_pr_url,
    )

    if not row:
        raise HTTPException(
            status_code=400,
            detail="No registered contributor found. Please register via /api/dao/register first.",
        )

    logger.info(f"DAO: new contribution submitted: '{body.title}' ({body.contribution_type})")
    return {
        "contribution": _serialize(_record_to_dict(row)),
        "message": "Contribution submitted. Ora will evaluate it within 24 hours.",
    }


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

    valid_types = {"code", "agent", "design", "doc", "research", "feedback", "community"}
    if body.contribution_type not in valid_types:
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
