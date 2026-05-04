"""
Gamification API — Streaks, XP, Badges, Collections

Implements:
  - Duolingo-style daily streaks with loss-aversion mechanics
  - XP system with reasons + totals
  - Achievement badges auto-computed from behavior
  - Pinterest/Airbnb-style save collections

Endpoints:
  GET  /api/gamification/status        — streak + XP + badges for current user
  POST /api/gamification/checkin       — record daily activity, award XP + badges
  GET  /api/gamification/collections   — list user's collections
  POST /api/gamification/collections   — create a collection
  POST /api/gamification/collections/{id}/items   — add item to collection
  GET  /api/gamification/collections/{id}/items   — list items in collection
  DELETE /api/gamification/collections/{id}/items/{item_id} — remove item
"""

import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.middleware import get_current_user_id
from core.database import execute, fetch, fetchrow, fetchval

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/gamification", tags=["gamification"])

# ─── XP amounts ──────────────────────────────────────────────────────────────
XP_TABLE = {
    "daily_login":         30,
    "card_view":            5,
    "card_rate":           15,
    "card_save":           20,
    "goal_create":         50,
    "goal_step":           40,
    "goal_complete":      200,
    "ioo_node_complete":  100,
    "journal_entry":       25,
    "chat_message":        10,
    "collection_create":   15,
    # Contribution + social
    "feedback_submit":     10,
    "contribution_submit": 50,
    "challenge_completed": 100,
    "challenge_sent_completed": 50,
}

# ─── Badge definitions ────────────────────────────────────────────────────────
BADGE_DEFS = [
    # key, name, emoji, condition_description
    ("first_steps",   "First Steps",      "👣", "First goal created"),
    ("on_fire",       "On Fire",          "🔥", "7-day streak"),
    ("unstoppable",   "Unstoppable",      "⚡", "30-day streak"),
    ("explorer",      "Explorer",         "🧭", "Saved 10 cards"),
    ("curator",       "Curator",          "✨", "Created 3+ collections"),
    ("achiever",      "Achiever",         "🏆", "Completed first goal"),
    ("thoughtful",    "Thoughtful",       "📓", "5 journal entries"),
    ("connected",     "Connected",        "🔗", "500+ XP total"),
    ("aura_friend",    "Aura's Friend",     "◈",  "50 chat messages"),
    ("century",       "Century",          "💯", "100-day streak"),
    ("path_maker",    "Path Maker",       "🗺",  "Mapped IOO graph"),
]


# ─── Models ───────────────────────────────────────────────────────────────────

class CheckinRequest(BaseModel):
    reason: str = "daily_login"   # context for XP award
    ref_id: Optional[str] = None
    context: Dict[str, Any] = Field(default_factory=dict)

class CollectionCreate(BaseModel):
    name: str
    emoji: str = "✦"
    color: str = "#00d4aa"

class CollectionItemAdd(BaseModel):
    screen_spec_id: str
    card_title: Optional[str] = None
    card_body: Optional[str] = None
    card_domain: Optional[str] = None
    card_color: Optional[str] = None
    # Durable IOO graph link, when the saved card originated from an IOO node.
    # Kept nullable so existing non-IOO and historical saves remain compatible.
    node_id: Optional[str] = None


# ─── Helpers ─────────────────────────────────────────────────────────────────

TIER_MULTIPLIERS = {"free": 1.0, "explorer": 1.5, "sovereign": 2.5}


def xp_to_level(total_xp: int) -> int:
    """XP required for each level follows exponential growth."""
    if total_xp < 200:
        return 1
    return min(100, int(1 + math.log(total_xp / 100, 1.5)))


def xp_for_level(level: int) -> int:
    """XP needed to reach this level."""
    if level <= 1:
        return 0
    return int(100 * (1.5 ** (level - 1)))


def _tier_multiplier(tier: str) -> float:
    return TIER_MULTIPLIERS.get((tier or "free").lower(), 1.0)


def _optional_uuid(value: Optional[str]) -> Optional[UUID]:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="node_id must be a valid UUID")


def _streak_multiplier(streak: int) -> float:
    if streak >= 30:
        return 1.5
    if streak >= 7:
        return 1.2
    if streak >= 3:
        return 1.1
    return 1.0


async def _current_xp_multiplier(user_id: str) -> Dict[str, Any]:
    """Return the user's active base multiplier from tier + streak."""
    user_row = await fetchrow(
        "SELECT subscription_tier, streak_current FROM users WHERE id = $1",
        UUID(user_id),
    )
    data = dict(user_row) if user_row else {}
    tier = (data.get("subscription_tier") or "free").lower()
    streak = int(data.get("streak_current") or 0)
    multiplier = _tier_multiplier(tier) * _streak_multiplier(streak)
    return {"tier": tier, "streak": streak, "multiplier": multiplier}


async def _award_xp(
    user_id: str,
    reason: str,
    ref_id: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
) -> int:
    """Award dynamically calculated XP for a reason. Returns amount awarded."""
    base = XP_TABLE.get(reason, 0)
    if base <= 0:
        return 0

    context = context or {}

    try:
        multiplier_info = await _current_xp_multiplier(user_id)
        multiplier = float(multiplier_info["multiplier"])

        first_time = await fetchval(
            "SELECT COUNT(*) FROM xp_log WHERE user_id = $1 AND reason = $2",
            UUID(user_id), reason,
        )
        if int(first_time or 0) == 0:
            multiplier *= 2.0

        if reason == "ioo_node_complete":
            difficulty = int(context.get("difficulty_level", 5) or 5)
            difficulty = max(1, min(10, difficulty))
            multiplier *= 0.5 + difficulty / 10.0

        amount = max(1, int(base * multiplier))
        ref_value = None
        if ref_id:
            ref_text = str(ref_id)
            ref_value = str(UUID(ref_text)) if len(ref_text) == 36 else ref_text

        await execute(
            "INSERT INTO xp_log (user_id, amount, reason, ref_id) VALUES ($1, $2, $3, $4)",
            UUID(user_id), amount, reason, ref_value,
        )

        total_xp = await _get_total_xp(user_id)
        await execute(
            "UPDATE users SET xp_level = $2 WHERE id = $1",
            UUID(user_id), xp_to_level(total_xp),
        )
        return amount
    except Exception as e:
        logger.warning(f"XP award failed: {e}")
        return 0


async def _get_total_xp(user_id: str) -> int:
    try:
        val = await fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM xp_log WHERE user_id = $1",
            UUID(user_id),
        )
        return int(val or 0)
    except Exception:
        return 0


async def _check_and_award_badges(user_id: str) -> List[Dict[str, Any]]:
    """Check all badge conditions and award any newly earned badges. Returns newly earned badges."""
    newly_earned = []

    try:
        # Gather stats
        total_xp = await _get_total_xp(user_id)

        streak_row = await fetchrow(
            "SELECT current_streak, longest_streak FROM user_streaks WHERE user_id = $1",
            UUID(user_id),
        )
        current_streak = streak_row["current_streak"] if streak_row else 0
        longest_streak = streak_row["longest_streak"] if streak_row else 0

        saved_count = await fetchval(
            "SELECT COUNT(*) FROM collection_items ci "
            "JOIN collections c ON c.id = ci.collection_id "
            "WHERE c.user_id = $1",
            UUID(user_id),
        ) or 0

        collection_count = await fetchval(
            "SELECT COUNT(*) FROM collections WHERE user_id = $1",
            UUID(user_id),
        ) or 0

        goals_created = await fetchval(
            "SELECT COUNT(*) FROM goals WHERE user_id = $1",
            UUID(user_id),
        ) or 0

        goals_completed = await fetchval(
            "SELECT COUNT(*) FROM goals WHERE user_id = $1 AND completed_at IS NOT NULL",
            UUID(user_id),
        ) or 0

        journal_count = await fetchval(
            "SELECT COUNT(*) FROM journal_entries WHERE user_id = $1",
            UUID(user_id),
        ) or 0

        chat_count = await fetchval(
            "SELECT COUNT(*) FROM xp_log WHERE user_id = $1 AND reason = 'chat_message'",
            UUID(user_id),
        ) or 0

        # Badge conditions
        conditions = {
            "first_steps":   goals_created >= 1,
            "on_fire":       current_streak >= 7 or longest_streak >= 7,
            "unstoppable":   current_streak >= 30 or longest_streak >= 30,
            "century":       current_streak >= 100 or longest_streak >= 100,
            "explorer":      saved_count >= 10,
            "curator":       collection_count >= 3,
            "achiever":      goals_completed >= 1,
            "thoughtful":    journal_count >= 5,
            "connected":     total_xp >= 500,
            "aura_friend":    chat_count >= 50,
        }

        # Award newly earned
        for key, earned in conditions.items():
            if not earned:
                continue
            defn = next((d for d in BADGE_DEFS if d[0] == key), None)
            if not defn:
                continue
            try:
                await execute(
                    """
                    INSERT INTO user_badges (user_id, badge_key, badge_name, badge_emoji)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (user_id, badge_key) DO NOTHING
                    """,
                    UUID(user_id), defn[0], defn[1], defn[2],
                )
                # Track if truly new by querying (approximate — race condition ok)
                newly_earned.append({"key": defn[0], "name": defn[1], "emoji": defn[2]})
            except Exception as e:
                logger.warning(f"Badge insert failed {key}: {e}")

    except Exception as e:
        logger.warning(f"Badge check failed: {e}")

    return newly_earned


async def _update_streak(user_id: str) -> Dict[str, Any]:
    """Update the user's daily streak. Returns streak info."""
    today = date.today()

    try:
        row = await fetchrow(
            "SELECT * FROM user_streaks WHERE user_id = $1",
            UUID(user_id),
        )

        if not row:
            # First activity ever
            await execute(
                """
                INSERT INTO user_streaks (user_id, current_streak, longest_streak, last_activity_date)
                VALUES ($1, 1, 1, $2)
                """,
                UUID(user_id), today,
            )
            await execute("UPDATE users SET streak_current = 1 WHERE id = $1", UUID(user_id))
            return {"current_streak": 1, "longest_streak": 1, "is_new_day": True, "streak_extended": True}

        last = row["last_activity_date"]
        current = row["current_streak"] or 0
        longest = row["longest_streak"] or 0

        if last == today:
            # Already checked in today
            return {
                "current_streak": current,
                "longest_streak": longest,
                "is_new_day": False,
                "streak_extended": False,
            }

        delta = (today - last).days if last else 999

        # Check freeze card
        frozen_until = row.get("streak_frozen_until")
        if frozen_until and last and delta == 2:
            # Frozen card covers one missed day
            new_streak = current + 1
            await execute(
                """
                UPDATE user_streaks
                SET current_streak = $1, longest_streak = GREATEST(longest_streak, $1),
                    last_activity_date = $2, streak_frozen_until = NULL, updated_at = NOW()
                WHERE user_id = $3
                """,
                new_streak, today, UUID(user_id),
            )
            await execute("UPDATE users SET streak_current = $2 WHERE id = $1", UUID(user_id), new_streak)
            return {
                "current_streak": new_streak,
                "longest_streak": max(longest, new_streak),
                "is_new_day": True,
                "streak_extended": True,
                "freeze_used": True,
            }

        if delta == 1:
            # Consecutive day
            new_streak = current + 1
            new_longest = max(longest, new_streak)
        else:
            # Streak broken
            new_streak = 1
            new_longest = longest  # preserve record

        await execute(
            """
            UPDATE user_streaks
            SET current_streak = $1, longest_streak = $2,
                last_activity_date = $3, updated_at = NOW()
            WHERE user_id = $4
            """,
            new_streak, new_longest, today, UUID(user_id),
        )
        await execute("UPDATE users SET streak_current = $2 WHERE id = $1", UUID(user_id), new_streak)

        return {
            "current_streak": new_streak,
            "longest_streak": new_longest,
            "is_new_day": True,
            "streak_extended": delta == 1,
            "streak_broken": delta > 1 and current > 1,
            "broken_from": current if delta > 1 else None,
        }

    except Exception as e:
        logger.error(f"Streak update failed: {e}")
        return {"current_streak": 0, "longest_streak": 0, "is_new_day": False, "streak_extended": False}


def _streak_at_risk(last_activity_date: Optional[date], current_streak: int) -> bool:
    """True if >22h since last activity and streak > 0 — show loss-aversion warning."""
    if not last_activity_date or current_streak <= 0:
        return False
    today = date.today()
    return last_activity_date < today and current_streak > 0


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("/status")
async def get_gamification_status(user_id: str = Depends(get_current_user_id)):
    """
    Full gamification status: streak, XP, badges, collection count.
    Called on app load / profile view.
    """
    try:
        streak_row = await fetchrow(
            "SELECT * FROM user_streaks WHERE user_id = $1",
            UUID(user_id),
        )
        current_streak = streak_row["current_streak"] if streak_row else 0
        longest_streak = streak_row["longest_streak"] if streak_row else 0
        last_date = streak_row["last_activity_date"] if streak_row else None

        total_xp = await _get_total_xp(user_id)
        level = xp_to_level(total_xp)
        current_level_xp = xp_for_level(level)
        next_level_xp = xp_for_level(level + 1)
        level_span = max(1, next_level_xp - current_level_xp)
        xp_progress_pct = max(0, min(100, int(((total_xp - current_level_xp) / level_span) * 100)))
        multiplier_info = await _current_xp_multiplier(user_id)
        tier = multiplier_info["tier"]

        badges = await fetch(
            "SELECT badge_key, badge_name, badge_emoji, earned_at FROM user_badges WHERE user_id = $1 ORDER BY earned_at DESC",
            UUID(user_id),
        )

        collection_count = await fetchval(
            "SELECT COUNT(*) FROM collections WHERE user_id = $1",
            UUID(user_id),
        ) or 0

        at_risk = _streak_at_risk(last_date, current_streak)

        # XP to next league milestone
        milestones = [100, 250, 500, 1000, 2500, 5000, 10000]
        next_milestone = next((m for m in milestones if m > total_xp), None)

        return {
            "streak": {
                "current": current_streak,
                "longest": longest_streak,
                "at_risk": at_risk,
                "last_activity": last_date.isoformat() if last_date else None,
            },
            "xp": {
                "total": total_xp,
                "next_milestone": next_milestone,
                "progress_to_next": (total_xp / next_milestone) if next_milestone else 1.0,
                "level": level,
                "xp_for_next_level": next_level_xp,
                "xp_progress_pct": xp_progress_pct,
                "multiplier": round(float(multiplier_info["multiplier"]), 2),
                "tier_bonus": "2.5x XP" if tier == "sovereign" else "1.5x XP" if tier == "explorer" else None,
            },
            "badges": [
                {
                    "key": b["badge_key"],
                    "name": b["badge_name"],
                    "emoji": b["badge_emoji"],
                    "earned_at": b["earned_at"].isoformat() if b["earned_at"] else None,
                }
                for b in badges
            ],
            "collections_count": int(collection_count),
            "level": level,
            "xp_for_next_level": next_level_xp,
            "xp_progress_pct": xp_progress_pct,
            "multiplier": round(float(multiplier_info["multiplier"]), 2),
            "tier_bonus": "2.5x XP" if tier == "sovereign" else "1.5x XP" if tier == "explorer" else None,
        }


    except Exception as e:
        logger.error(f"Gamification status error: {e}")
        return {
            "streak": {"current": 0, "longest": 0, "at_risk": False, "last_activity": None},
            "xp": {
                "total": 0,
                "next_milestone": 100,
                "progress_to_next": 0,
                "level": 1,
                "xp_for_next_level": xp_for_level(2),
                "xp_progress_pct": 0,
                "multiplier": 1.0,
                "tier_bonus": None,
            },
            "badges": [],
            "collections_count": 0,
            "level": 1,
            "xp_for_next_level": xp_for_level(2),
            "xp_progress_pct": 0,
            "multiplier": 1.0,
            "tier_bonus": None,
        }


@router.get("/weekly-recap")
async def get_weekly_recap(user_id: str = Depends(get_current_user_id)):
    """
    Seven-day progress recap for the home/goals surface.
    Aggregates XP, streak, goal movement, saved IOO cards, and journal cadence
    without requiring new tables.
    """
    since = datetime.now(timezone.utc) - timedelta(days=7)
    try:
        xp_rows = await fetch(
            """
            SELECT reason, COALESCE(SUM(amount), 0) AS amount, COUNT(*) AS count
            FROM xp_log
            WHERE user_id = $1 AND created_at >= $2
            GROUP BY reason
            ORDER BY amount DESC
            """,
            UUID(user_id), since,
        )
        total_xp = sum(int(r["amount"] or 0) for r in xp_rows)

        goals_created = await fetchval(
            "SELECT COUNT(*) FROM goals WHERE user_id = $1 AND created_at >= $2",
            UUID(user_id), since,
        ) or 0
        goals_completed = await fetchval(
            "SELECT COUNT(*) FROM goals WHERE user_id = $1 AND completed_at >= $2",
            UUID(user_id), since,
        ) or 0
        journal_entries = await fetchval(
            """
            SELECT COUNT(*) FROM aura_conversations
            WHERE user_id = $1 AND role = 'journal_entry' AND created_at >= $2
            """,
            UUID(user_id), since,
        ) or 0
        saved_nodes = await fetchval(
            """
            SELECT COUNT(*) FROM collection_items ci
            JOIN collections c ON c.id = ci.collection_id
            WHERE c.user_id = $1 AND ci.created_at >= $2
            """,
            UUID(user_id), since,
        ) or 0
        streak_row = await fetchrow(
            "SELECT current_streak, longest_streak FROM user_streaks WHERE user_id = $1",
            UUID(user_id),
        )
        current_streak = int(streak_row["current_streak"] or 0) if streak_row else 0

        highlights = []
        if total_xp:
            highlights.append(f"You earned {total_xp} XP this week.")
        if goals_completed:
            highlights.append(f"You completed {goals_completed} goal{'s' if goals_completed != 1 else ''}.")
        if saved_nodes:
            highlights.append(f"You saved {saved_nodes} IOO node{'s' if saved_nodes != 1 else ''} for later.")
        if journal_entries:
            highlights.append(f"You reflected {journal_entries} time{'s' if journal_entries != 1 else ''}.")
        if current_streak:
            highlights.append(f"Your current streak is {current_streak} day{'s' if current_streak != 1 else ''}.")

        if not highlights:
            highlights.append("This is a clean slate week — one tiny action today starts the graph moving.")

        return {
            "window_days": 7,
            "since": since.isoformat(),
            "total_xp": total_xp,
            "xp_by_reason": [{"reason": r["reason"], "amount": int(r["amount"] or 0), "count": int(r["count"] or 0)} for r in xp_rows],
            "goals_created": int(goals_created),
            "goals_completed": int(goals_completed),
            "journal_entries": int(journal_entries),
            "saved_nodes": int(saved_nodes),
            "current_streak": current_streak,
            "highlights": highlights[:4],
            "next_prompt": "Choose one node to touch today: a goal step, a journal reflection, or one saved opportunity.",
        }
    except Exception as e:
        logger.error(f"Weekly recap error: {e}")
        return {
            "window_days": 7,
            "since": since.isoformat(),
            "total_xp": 0,
            "xp_by_reason": [],
            "goals_created": 0,
            "goals_completed": 0,
            "journal_entries": 0,
            "saved_nodes": 0,
            "current_streak": 0,
            "highlights": ["This is a clean slate week — one tiny action today starts the graph moving."],
            "next_prompt": "Choose one node to touch today: a goal step, a journal reflection, or one saved opportunity.",
        }

@router.post("/checkin")
async def daily_checkin(
    body: CheckinRequest,
    user_id: str = Depends(get_current_user_id),
):
    """
    Record daily activity: update streak, award XP, check badges.
    Call this on app open / first meaningful action per session.
    """
    streak_result = await _update_streak(user_id)

    xp_awarded = 0
    if streak_result.get("is_new_day"):
        # Award daily login XP
        xp_awarded += await _award_xp(user_id, "daily_login")
        # Bonus XP for milestones
        streak = streak_result["current_streak"]
        if streak in (7, 14, 30, 60, 100, 365):
            bonus = {7: 50, 14: 75, 30: 150, 60: 250, 100: 500, 365: 2000}.get(streak, 0)
            if bonus:
                await execute(
                    "INSERT INTO xp_log (user_id, amount, reason, ref_id) VALUES ($1, $2, $3, $4)",
                    UUID(user_id), bonus, f"streak_milestone_{streak}", str(streak),
                )
                xp_awarded += bonus

    # Award XP for the triggering action (if different from login)
    if body.reason != "daily_login":
        xp_awarded += await _award_xp(user_id, body.reason, body.ref_id, body.context)

    # Check and award badges
    new_badges = await _check_and_award_badges(user_id)

    total_xp = await _get_total_xp(user_id)

    return {
        "streak": streak_result,
        "xp_awarded": xp_awarded,
        "total_xp": total_xp,
        "new_badges": new_badges,
        "message": _checkin_message(streak_result),
    }


def _checkin_message(streak_result: Dict[str, Any]) -> str:
    """Human-friendly message for checkin response."""
    streak = streak_result.get("current_streak", 0)
    if streak_result.get("streak_broken"):
        broken_from = streak_result.get("broken_from", 0)
        return f"Streak reset — you had {broken_from} days! Start a new one 🔥"
    if streak_result.get("streak_extended"):
        if streak == 7:
            return "🔥 7-day streak! You're on fire!"
        if streak == 30:
            return "⚡ 30 days straight! Unstoppable!"
        if streak == 100:
            return "💯 100-day streak! Legend status!"
        if streak > 1:
            return f"🔥 {streak}-day streak! Keep going!"
        return "Day 1! Every journey starts here. 🌱"
    if streak_result.get("at_risk", False):
        return f"⚠️ Your {streak}-day streak is at risk! Check in now."
    return f"Streak: {streak} days"


@router.post("/xp")
async def award_xp_endpoint(
    reason: str,
    ref_id: Optional[str] = None,
    user_id: str = Depends(get_current_user_id),
):
    """Award XP for an action. Called from other routes or client."""
    amount = await _award_xp(user_id, reason, ref_id)
    total = await _get_total_xp(user_id)
    new_badges = await _check_and_award_badges(user_id)
    return {"awarded": amount, "total_xp": total, "new_badges": new_badges}


# ─── Collections ─────────────────────────────────────────────────────────────

@router.get("/collections")
async def list_collections(user_id: str = Depends(get_current_user_id)):
    """List user's collections with item counts."""
    try:
        rows = await fetch(
            """
            SELECT c.id, c.name, c.emoji, c.color, c.created_at,
                   COUNT(ci.id) AS item_count
            FROM collections c
            LEFT JOIN collection_items ci ON ci.collection_id = c.id
            WHERE c.user_id = $1
            GROUP BY c.id
            ORDER BY c.created_at DESC
            """,
            UUID(user_id),
        )
        return [
            {
                "id": str(r["id"]),
                "name": r["name"],
                "emoji": r["emoji"],
                "color": r["color"],
                "item_count": int(r["item_count"] or 0),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"List collections error: {e}")
        return []


@router.post("/collections")
async def create_collection(
    body: CollectionCreate,
    user_id: str = Depends(get_current_user_id),
):
    """Create a new collection."""
    try:
        row = await fetchrow(
            """
            INSERT INTO collections (user_id, name, emoji, color)
            VALUES ($1, $2, $3, $4)
            RETURNING id, name, emoji, color, created_at
            """,
            UUID(user_id), body.name[:120], body.emoji[:8], body.color[:20],
        )
        await _award_xp(user_id, "collection_create")
        await _check_and_award_badges(user_id)
        return {
            "id": str(row["id"]),
            "name": row["name"],
            "emoji": row["emoji"],
            "color": row["color"],
            "item_count": 0,
        }
    except Exception as e:
        logger.error(f"Create collection error: {e}")
        raise HTTPException(status_code=500, detail="Failed to create collection")


@router.post("/collections/{collection_id}/items")
async def add_to_collection(
    collection_id: str,
    body: CollectionItemAdd,
    user_id: str = Depends(get_current_user_id),
):
    """Add a card to a collection."""
    try:
        # Verify ownership
        col = await fetchrow(
            "SELECT id FROM collections WHERE id = $1 AND user_id = $2",
            UUID(collection_id), UUID(user_id),
        )
        if not col:
            raise HTTPException(status_code=404, detail="Collection not found")

        await execute(
            """
            INSERT INTO collection_items
                (collection_id, screen_spec_id, card_title, card_body, card_domain, card_color, node_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (collection_id, screen_spec_id) DO UPDATE
            SET node_id = COALESCE(collection_items.node_id, EXCLUDED.node_id)
            """,
            UUID(collection_id),
            body.screen_spec_id[:120],
            (body.card_title or "")[:255],
            body.card_body or "",
            (body.card_domain or "")[:60],
            (body.card_color or "#00d4aa")[:20],
            _optional_uuid(body.node_id),
        )
        await _award_xp(user_id, "card_save", body.screen_spec_id)
        await _check_and_award_badges(user_id)
        return {"ok": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Add to collection error: {e}")
        raise HTTPException(status_code=500, detail="Failed to save to collection")


@router.get("/collections/{collection_id}/items")
async def list_collection_items(
    collection_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """List items in a collection."""
    try:
        col = await fetchrow(
            "SELECT id, name, emoji, color FROM collections WHERE id = $1 AND user_id = $2",
            UUID(collection_id), UUID(user_id),
        )
        if not col:
            raise HTTPException(status_code=404, detail="Collection not found")

        items = await fetch(
            "SELECT * FROM collection_items WHERE collection_id = $1 ORDER BY saved_at DESC",
            UUID(collection_id),
        )
        return {
            "collection": {
                "id": str(col["id"]),
                "name": col["name"],
                "emoji": col["emoji"],
                "color": col["color"],
            },
            "items": [
                {
                    "id": r["id"],
                    "screen_spec_id": r["screen_spec_id"],
                    "card_title": r["card_title"],
                    "card_body": r["card_body"],
                    "card_domain": r["card_domain"],
                    "card_color": r["card_color"],
                    "node_id": str(r["node_id"]) if r["node_id"] else None,
                    "saved_at": r["saved_at"].isoformat() if r["saved_at"] else None,
                }
                for r in items
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"List collection items error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch collection")


@router.get("/analytics/saved-node-conversions")
async def saved_node_conversions(
    user_id: str = Depends(get_current_user_id),
):
    """
    Compute the per-user lifecycle bridge from saved IOO cards to progress.

    This keeps collection rendering backward-compatible while giving Aura a
    direct analytics read on recommendation → save → start/complete/abandon.
    """
    try:
        rows = await fetch(
            """
            WITH saved_nodes AS (
                SELECT
                    ci.id AS collection_item_id,
                    c.user_id,
                    ci.node_id,
                    COALESCE(n.domain, ci.card_domain, 'unknown') AS domain,
                    COALESCE(n.generation_source, 'unknown') AS source,
                    n.neural_state,
                    n.pruned_at
                FROM collection_items ci
                JOIN collections c ON c.id = ci.collection_id
                LEFT JOIN ioo_nodes n ON n.id = ci.node_id
                WHERE c.user_id = $1
                  AND ci.node_id IS NOT NULL
            ),
            progress_by_node AS (
                SELECT
                    user_id,
                    node_id,
                    BOOL_OR(status IN ('started','completed')) AS has_started,
                    BOOL_OR(status = 'completed') AS has_completed,
                    BOOL_OR(status = 'abandoned') AS has_abandoned
                FROM ioo_user_progress
                WHERE user_id = $1
                  AND node_id IS NOT NULL
                GROUP BY user_id, node_id
            )
            SELECT
                s.node_id,
                MIN(s.domain) AS domain,
                MIN(s.source) AS source,
                COUNT(*)::int AS saved_count,
                COUNT(*) FILTER (WHERE COALESCE(p.has_started, false))::int AS started_count,
                COUNT(*) FILTER (WHERE COALESCE(p.has_completed, false))::int AS completed_count,
                COUNT(*) FILTER (WHERE COALESCE(p.has_abandoned, false))::int AS abandoned_count,
                COUNT(*) FILTER (WHERE s.neural_state = 'pruned' OR s.pruned_at IS NOT NULL)::int AS pruned_count
            FROM saved_nodes s
            LEFT JOIN progress_by_node p
              ON p.user_id = s.user_id
             AND p.node_id = s.node_id
            GROUP BY s.node_id
            ORDER BY saved_count DESC, completed_count DESC
            """,
            UUID(user_id),
        )
        return {
            "items": [
                {
                    "node_id": str(r["node_id"]),
                    "domain": r["domain"],
                    "source": r["source"],
                    "saved_count": r["saved_count"],
                    "started_count": r["started_count"],
                    "completed_count": r["completed_count"],
                    "abandoned_count": r["abandoned_count"],
                    "pruned_count": r["pruned_count"],
                }
                for r in rows
            ]
        }
    except Exception as e:
        logger.error(f"Saved-node analytics error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch saved-node analytics")


@router.delete("/collections/{collection_id}/items/{item_id}")
async def remove_from_collection(
    collection_id: str,
    item_id: int,
    user_id: str = Depends(get_current_user_id),
):
    """Remove an item from a collection."""
    try:
        # Verify ownership
        col = await fetchrow(
            "SELECT id FROM collections WHERE id = $1 AND user_id = $2",
            UUID(collection_id), UUID(user_id),
        )
        if not col:
            raise HTTPException(status_code=404, detail="Collection not found")

        await execute(
            "DELETE FROM collection_items WHERE collection_id = $1 AND id = $2",
            UUID(collection_id), item_id,
        )
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Remove from collection error: {e}")
        raise HTTPException(status_code=500, detail="Failed to remove item")


# ─── Quick-save (no collection picker — goes to default "Saved") ─────────────

@router.post("/save")
async def quick_save(
    body: CollectionItemAdd,
    user_id: str = Depends(get_current_user_id),
):
    """
    Quick-save a card to the user's default 'Saved' collection.
    Creates the collection if it doesn't exist.
    """
    try:
        # Get or create default "Saved" collection
        col = await fetchrow(
            "SELECT id FROM collections WHERE user_id = $1 AND name = 'Saved' LIMIT 1",
            UUID(user_id),
        )
        if not col:
            col = await fetchrow(
                """
                INSERT INTO collections (user_id, name, emoji, color)
                VALUES ($1, 'Saved', '✦', '#00d4aa')
                RETURNING id
                """,
                UUID(user_id),
            )

        collection_id = col["id"]
        await execute(
            """
            INSERT INTO collection_items
                (collection_id, screen_spec_id, card_title, card_body, card_domain, card_color, node_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (collection_id, screen_spec_id) DO UPDATE
            SET node_id = COALESCE(collection_items.node_id, EXCLUDED.node_id)
            """,
            collection_id,
            body.screen_spec_id[:120],
            (body.card_title or "")[:255],
            body.card_body or "",
            (body.card_domain or "")[:60],
            (body.card_color or "#00d4aa")[:20],
            _optional_uuid(body.node_id),
        )
        await _award_xp(user_id, "card_save", body.screen_spec_id)
        await _check_and_award_badges(user_id)
        return {"ok": True, "collection_id": str(collection_id)}

    except Exception as e:
        logger.error(f"Quick save error: {e}")
        raise HTTPException(status_code=500, detail="Failed to save card")
