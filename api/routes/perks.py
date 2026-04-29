"""
Subscriber perk unlocks — what users get at different XP levels.
Free users get taste of perks to encourage upgrade.
"""
from typing import Any, Dict, List
from uuid import UUID

from fastapi import APIRouter, Depends

from api.middleware import get_current_user_id
from api.routes.gamification import xp_for_level, xp_to_level
from core.database import fetchrow, fetchval

router = APIRouter(prefix="/api/perks", tags=["perks"])

LEVEL_PERKS = {
    "level_5_feed_unlock":     (5,  "free",       "Unlock 5 extra feed cards/day"),
    "level_10_streak_shield":  (10, "free",       "Unlock 1 Streak Shield — protect your streak for 1 miss"),
    "level_15_ioo_filter":     (15, "free",       "Unlock IOO difficulty filter in feed"),
    "level_20_ora_memory":     (20, "free",       "Ora remembers your preferences more deeply"),
    "explorer_daily_cards":    (1,  "explorer",   "50 feed cards/day (vs 10 free)"),
    "explorer_xp_boost":       (1,  "explorer",   "1.5x XP on everything"),
    "explorer_goal_coaching":  (5,  "explorer",   "Unlock deep goal coaching sessions with Ora"),
    "explorer_level_rewards":  (10, "explorer",   "Monthly bonus XP drops based on activity"),
    "sovereign_unlimited":     (1,  "sovereign",  "Unlimited feed cards"),
    "sovereign_xp_boost":      (1,  "sovereign",  "2.5x XP on everything"),
    "sovereign_priority_ora":  (1,  "sovereign",  "Priority Ora responses + longer context"),
    "sovereign_exclusive_ioo": (5,  "sovereign",  "Access exclusive high-difficulty IOO nodes"),
    "sovereign_early_access":  (1,  "sovereign",  "Early access to every new feature"),
    "sovereign_cp_bonus":      (10, "sovereign",  "10% bonus CP on all DAO contributions"),
}

TIER_RANK = {"free": 0, "explorer": 1, "sovereign": 2}


def _tier_meets(user_tier: str, required_tier: str) -> bool:
    return TIER_RANK.get(user_tier or "free", 0) >= TIER_RANK.get(required_tier or "free", 0)


def _perk_payload(perk_id: str, min_level: int, tier_required: str, description: str, total_xp: int) -> Dict[str, Any]:
    required_xp = xp_for_level(min_level)
    return {
        "id": perk_id,
        "min_level": min_level,
        "tier_required": tier_required,
        "description": description,
        "xp_required": required_xp,
        "xp_remaining": max(0, required_xp - total_xp),
    }


async def _user_progress(user_id: str) -> Dict[str, Any]:
    total_xp = await fetchval(
        "SELECT COALESCE(SUM(amount), 0) FROM xp_log WHERE user_id = $1",
        UUID(user_id),
    )
    user_row = await fetchrow(
        "SELECT subscription_tier FROM users WHERE id = $1",
        UUID(user_id),
    )
    total = int(total_xp or 0)
    user_data = dict(user_row) if user_row else {}
    tier = (user_data.get("subscription_tier") or "free").lower()
    return {"total_xp": total, "level": xp_to_level(total), "subscription_tier": tier}


@router.get("/my-perks")
async def get_my_perks(user_id: str = Depends(get_current_user_id)):
    """Return user's active perks and what they're close to unlocking."""
    progress = await _user_progress(user_id)
    total_xp = progress["total_xp"]
    level = progress["level"]
    tier = progress["subscription_tier"]

    active: List[Dict[str, Any]] = []
    almost_unlocked: List[Dict[str, Any]] = []
    upgrade_to_unlock: List[Dict[str, Any]] = []

    for perk_id, (min_level, tier_required, description) in LEVEL_PERKS.items():
        perk = _perk_payload(perk_id, min_level, tier_required, description, total_xp)
        tier_ok = _tier_meets(tier, tier_required)
        level_ok = level >= min_level

        if level_ok and tier_ok:
            active.append(perk)
        elif not tier_ok:
            upgrade_to_unlock.append({**perk, "level_met": level_ok})
        elif 0 < perk["xp_remaining"] <= 500:
            almost_unlocked.append(perk)

    return {
        "user": progress,
        "active_perks": active,
        "almost_unlocked": almost_unlocked,
        "upgrade_to_unlock": upgrade_to_unlock,
    }


@router.get("/all")
async def get_all_perks():
    """Full perk catalogue for marketing and upgrade CTAs."""
    return {
        "perks": [
            _perk_payload(perk_id, min_level, tier_required, description, 0)
            for perk_id, (min_level, tier_required, description) in LEVEL_PERKS.items()
        ],
        "tiers": ["free", "explorer", "sovereign"],
    }
