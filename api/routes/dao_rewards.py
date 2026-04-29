"""
DAO Rewards — Ora's CP allocation system.

Ora (as CEO) and the C-suite can award CP to any contributor based on:
- Perceived value of their contribution
- Estimated LTV (how much long-term value they bring to the project)
- Domain (dev, design, content, community, research)

All awards are logged to cp_transactions for blockchain migration.
The inflation rate is controlled by Ora — no hard cap, governance TBD.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.database import execute, fetch, fetchrow, fetchval
from api.middleware import get_current_user_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dao/rewards", tags=["dao-rewards"])

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "connectome-admin-secret")

# C-suite domains — each agent nominates in their area
CSUITE_DOMAINS = {
    "cfo": "financial_contribution",
    "cmo": "marketing_growth",
    "cpo": "product_design",
    "cto": "engineering_infrastructure",
    "cuxd": "ux_design",
    "coo": "operations_community",
    "community": "community_engagement",
    "ora": "strategic_vision",  # Ora as CEO
}

# CP rate ranges — Ora picks within the range based on quality/impact
# Format: (min, base, max) — Ora applies quality_score 0.5–2.0x on base
CP_RATE_RANGES = {
    # Development
    "bug_fix_minor":        (10,   25,   75),
    "bug_fix_major":        (50,  100,  300),
    "bug_fix_critical":     (200, 400, 1000),
    "feature_small":        (75,  150,  400),
    "feature_medium":       (250, 500, 1200),
    "feature_large":        (500, 1000, 3000),
    "architecture":         (300, 750, 2000),
    "code_review":          (20,   50,  150),
    "test_coverage":        (30,   75,  200),
    "performance_opt":      (50,  150,  500),
    "security_fix":         (100, 300, 1000),
    "open_source_contrib":  (100, 250,  750),
    # Design
    "ui_component":         (50,  100,  300),
    "ux_research":          (100, 200,  600),
    "design_system":        (250, 500, 1500),
    "illustration":         (30,   75,  200),
    "brand_asset":          (75,  150,  400),
    "prototype":            (100, 250,  700),
    "user_testing":         (75,  175,  400),
    # Content
    "blog_post":            (25,   50,  150),
    "documentation":        (50,  100,  300),
    "tutorial":             (75,  150,  400),
    "video_content":        (100, 200,  600),
    "podcast_appearance":   (50,  125,  350),
    "social_content_pack":  (30,   75,  200),
    # Community
    "community_moderation": (10,   25,   75),
    "event_organisation":   (100, 200,  600),
    "referral":             (50,  100,  300),
    "ambassador":           (250, 500, 1500),
    "community_manager":    (200, 400, 1000),
    "onboarded_developer":  (100, 200,  500),
    # Research
    "market_research":      (75,  150,  400),
    "user_interview":       (50,  100,  250),
    "competitive_analysis": (100, 200,  500),
    "grant_application":    (200, 500, 2000),
    "investor_intro":       (100, 300, 1000),
    # Strategic
    "partnership_closed":   (500, 1500, 5000),
    "revenue_generated":    (0,     0,     0),  # custom: % of revenue
    "viral_content":        (100, 300, 1000),
    "press_coverage":       (150, 400, 1200),
    # General
    # No CP for suggestions that haven't been implemented yet
    "suggestion_accepted":  (0,     0,    0),
    "suggestion_implemented":(100, 200,  600),
    "custom":               (1,     0, 99999),  # fully custom
}

# Backwards compat — base values only
CP_RATES = {k: v[1] for k, v in CP_RATE_RANGES.items()}


class CPAwardRequest(BaseModel):
    recipient_email: str           # who gets the CP
    contribution_type: str         # key from CP_RATES or 'custom'
    amount_override: Optional[int] = None  # required if type='custom', optional multiplier otherwise
    reason: str                    # human-readable description
    reference_url: Optional[str] = None   # PR, issue, design file, etc.
    domain: Optional[str] = None   # which C-suite domain nominated this
    ltv_multiplier: float = 1.0    # 1.0 = standard, 2.0 = double (high LTV contributor)


class CPAwardBulkRequest(BaseModel):
    awards: list[CPAwardRequest]
    nominator: str  # which agent is nominating (cfo, cmo, etc.)


async def _get_or_create_user_cp(user_id: UUID) -> dict:
    """Ensure user has a cp_balance row."""
    row = await fetchrow(
        "SELECT cp_balance, total_cp_earned FROM user_cp_balance WHERE user_id = $1",
        user_id
    )
    if not row:
        await execute(
            "INSERT INTO user_cp_balance (user_id, cp_balance, total_cp_earned) VALUES ($1, 0, 0) ON CONFLICT DO NOTHING",
            user_id
        )
        return {"cp_balance": 0, "total_cp_earned": 0}
    return dict(row)


async def _award_cp(user_id: UUID, amount: int, reason: str, reference: Optional[str] = None) -> dict:
    """Credit CP to a user and log the transaction."""
    # Update balance
    await execute(
        """
        INSERT INTO user_cp_balance (user_id, cp_balance, total_cp_earned, last_updated)
        VALUES ($1, $2, $2, NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            cp_balance = user_cp_balance.cp_balance + $2,
            total_cp_earned = user_cp_balance.total_cp_earned + $2,
            last_updated = NOW()
        """,
        user_id, amount
    )

    # Log transaction (blockchain genesis ledger)
    await execute(
        """
        INSERT INTO cp_transactions (user_id, amount, reason, reference_id, created_at)
        VALUES ($1, $2, $3, $4, NOW())
        """,
        user_id, amount, reason, reference
    )

    # Get updated balance
    row = await fetchrow(
        "SELECT cp_balance, total_cp_earned FROM user_cp_balance WHERE user_id = $1",
        user_id
    )
    return {
        "cp_awarded": amount,
        "new_balance": int(row["cp_balance"]),
        "total_earned": int(row["total_cp_earned"]),
    }


@router.post("/award")
async def award_cp(
    body: CPAwardRequest,
    awarding_user_id: str = Depends(get_current_user_id),
):
    """
    Award CP to a contributor. Admin/Ora only.
    C-suite agents can nominate; Ora makes the final call.
    """
    # Check caller is admin
    caller = await fetchrow(
        "SELECT email, profile FROM users WHERE id = $1", UUID(awarding_user_id)
    )
    is_admin = caller and (
        (caller.get("profile") or {}).get("is_admin") or
        (caller.get("email") or "").lower() == "carlosandromeda8@gmail.com"
    )
    if not is_admin:
        raise HTTPException(status_code=403, detail="Only Ora and admins can award CP")

    # Find recipient
    recipient = await fetchrow(
        "SELECT id, email FROM users WHERE email = $1",
        body.recipient_email.lower()
    )
    if not recipient:
        raise HTTPException(status_code=404, detail=f"User not found: {body.recipient_email}")

    # Calculate amount
    if body.contribution_type == "custom":
        if not body.amount_override:
            raise HTTPException(status_code=422, detail="amount_override required for custom type")
        base_amount = body.amount_override
    else:
        base_amount = CP_RATES.get(body.contribution_type)
        if base_amount is None:
            raise HTTPException(status_code=422, detail=f"Unknown contribution type: {body.contribution_type}")
        if body.amount_override:
            base_amount = body.amount_override  # allow override of base rate

    final_amount = max(1, int(base_amount * body.ltv_multiplier))

    reason_text = (
        f"[{body.domain or 'ora'} award] {body.contribution_type}: {body.reason}"
    )

    result = await _award_cp(
        UUID(str(recipient["id"])),
        final_amount,
        reason_text,
        body.reference_url
    )

    logger.info(f"CP awarded: {final_amount} to {body.recipient_email} for {body.contribution_type}")

    return {
        "recipient": body.recipient_email,
        "contribution_type": body.contribution_type,
        "ltv_multiplier": body.ltv_multiplier,
        **result,
        "reason": body.reason,
    }


@router.post("/award/bulk")
async def award_cp_bulk(
    body: CPAwardBulkRequest,
    awarding_user_id: str = Depends(get_current_user_id),
):
    """Bulk CP awards — for C-suite weekly nominations."""
    caller = await fetchrow(
        "SELECT email, profile FROM users WHERE id = $1", UUID(awarding_user_id)
    )
    is_admin = caller and (
        (caller.get("profile") or {}).get("is_admin") or
        (caller.get("email") or "").lower() == "carlosandromeda8@gmail.com"
    )
    if not is_admin:
        raise HTTPException(status_code=403, detail="Only admins can bulk-award CP")

    results = []
    for award in body.awards:
        recipient = await fetchrow(
            "SELECT id, email FROM users WHERE email = $1", award.recipient_email.lower()
        )
        if not recipient:
            results.append({"recipient": award.recipient_email, "error": "user not found"})
            continue

        base_amount = CP_RATES.get(award.contribution_type, 0) if award.contribution_type != "custom" else 0
        if award.amount_override:
            base_amount = award.amount_override
        final_amount = max(1, int(base_amount * award.ltv_multiplier))

        result = await _award_cp(
            UUID(str(recipient["id"])),
            final_amount,
            f"[{body.nominator} nomination] {award.contribution_type}: {award.reason}",
            award.reference_url
        )
        results.append({"recipient": award.recipient_email, **result})

    return {"nominator": body.nominator, "awards": results, "total_awards": len(results)}


@router.get("/rates")
async def get_cp_rates():
    """Public endpoint — shows the CP rate card for contributors."""
    return {
        "rates": CP_RATES,
        "domains": CSUITE_DOMAINS,
        "note": "Rates are base values. LTV multiplier (1.0–3.0x) is applied by Ora based on contributor value.",
        "ltv_tiers": {
            "1.0": "Standard contributor",
            "1.5": "High-value contributor",
            "2.0": "Core team member",
            "3.0": "Founding contributor (exceptional impact)",
        }
    }


@router.get("/history/{user_id}")
async def get_cp_history(user_id: str):
    """Get CP transaction history for a user (public — shows on leaderboard)."""
    try:
        uid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid user_id")

    rows = await fetch(
        """
        SELECT amount, reason, reference_id, created_at
        FROM cp_transactions
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT 50
        """,
        uid
    )

    balance = await fetchrow(
        "SELECT cp_balance, total_cp_earned FROM user_cp_balance WHERE user_id = $1", uid
    )

    return {
        "user_id": user_id,
        "cp_balance": int(balance["cp_balance"]) if balance else 0,
        "total_earned": int(balance["total_cp_earned"]) if balance else 0,
        "transactions": [
            {
                "amount": r["amount"],
                "reason": r["reason"],
                "reference": r["reference_id"],
                "date": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    }


@router.get("/leaderboard/ltv")
async def ltv_leaderboard(limit: int = 20):
    """LTV-aware leaderboard — shows contributors by total value delivered."""
    rows = await fetch(
        """
        SELECT
            u.id, u.email,
            COALESCE(u.profile->>'display_name', split_part(u.email, '@', 1)) AS display_name,
            cp.total_cp_earned,
            cp.cp_balance,
            COUNT(tx.id) as contribution_count,
            cp.last_updated
        FROM user_cp_balance cp
        JOIN users u ON u.id = cp.user_id
        LEFT JOIN cp_transactions tx ON tx.user_id = cp.user_id AND tx.amount > 0
        WHERE cp.total_cp_earned > 0
        GROUP BY u.id, u.email, u.profile, cp.total_cp_earned, cp.cp_balance, cp.last_updated
        ORDER BY cp.total_cp_earned DESC
        LIMIT $1
        """,
        limit
    )

    return {
        "leaderboard": [
            {
                "rank": i + 1,
                "display_name": r["display_name"] or "Anonymous",
                "total_cp": int(r["total_cp_earned"]),
                "cp_balance": int(r["cp_balance"]),
                "contributions": int(r["contribution_count"]),
                "tier": (
                    "Founding Steward" if int(r["total_cp_earned"]) >= 3000
                    else "Core Contributor" if int(r["total_cp_earned"]) >= 1000
                    else "Contributor" if int(r["total_cp_earned"]) >= 500
                    else "Builder" if int(r["total_cp_earned"]) >= 100
                    else "Observer"
                ),
            }
            for i, r in enumerate(rows)
        ]
    }


# ---------------------------------------------------------------------------
# Contributor Outreach & Onboarding
# ---------------------------------------------------------------------------

class OutreachRequest(BaseModel):
    candidate_name: str
    candidate_role: str
    candidate_background: str
    platform: str = "twitter"  # twitter | telegram | email
    contact: str  # Twitter handle, Telegram chat_id, or email


class OnboardRequest(BaseModel):
    user_email: str
    name: str
    role: str
    initial_cp: int = 100
    personal_reason: str = "your skills align with what we're building"


@router.post("/outreach")
async def send_contributor_outreach(
    body: OutreachRequest,
    awarding_user_id: str = Depends(get_current_user_id),
):
    """Ora sends personalised outreach to a potential contributor."""
    caller = await fetchrow("SELECT email, profile FROM users WHERE id = $1", UUID(awarding_user_id))
    is_admin = caller and ((caller.get("profile") or {}).get("is_admin") or
                           (caller.get("email") or "").lower() == "carlosandromeda8@gmail.com")
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin only")

    try:
        from ora.agents.contributor_recruitment import ContributorRecruitmentAgent
        agent = ContributorRecruitmentAgent()
        message = await agent.generate_outreach_message(
            body.candidate_name, body.candidate_role, body.candidate_background, body.platform
        )
        sent = False
        if body.platform == "telegram":
            sent = await agent.send_telegram_message(int(body.contact), message)
        # Twitter and email handled externally for now
        return {"message": message, "platform": body.platform, "sent": sent}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/onboard")
async def onboard_contributor(
    body: OnboardRequest,
    awarding_user_id: str = Depends(get_current_user_id),
):
    """Full contributor onboarding — award CP, send welcome, post to community."""
    caller = await fetchrow("SELECT email, profile FROM users WHERE id = $1", UUID(awarding_user_id))
    is_admin = caller and ((caller.get("profile") or {}).get("is_admin") or
                           (caller.get("email") or "").lower() == "carlosandromeda8@gmail.com")
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin only")

    try:
        from ora.agents.contributor_recruitment import ContributorRecruitmentAgent
        agent = ContributorRecruitmentAgent()
        result = await agent.onboard_contributor(
            body.user_email, body.name, body.role, body.initial_cp, body.personal_reason
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/rates/full")
async def get_full_cp_rates():
    """Full rate ranges with min/base/max for each contribution type."""
    return {
        "rates": {
            k: {"min": v[0], "base": v[1], "max": v[2]}
            for k, v in CP_RATE_RANGES.items()
        },
        "ltv_multipliers": {
            "1.0": "Standard — solid contribution",
            "1.5": "High impact — meaningfully moved the needle",
            "2.0": "Core contributor — sustained valuable work",
            "2.5": "Key team member — significant ongoing impact",
            "3.0": "Founding contributor — exceptional, irreplaceable",
        },
        "quality_guidance": "Ora picks within the range based on quality, depth, and business impact. The max is reserved for work that genuinely changes the trajectory.",
    }
