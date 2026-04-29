"""
Friends API — requests, social graph, activity, and IOO challenges.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from api.middleware import get_current_user_id
from core.database import execute, fetch, fetchrow

router = APIRouter(prefix="/api/friends", tags=["friends"])


class ChallengeCreate(BaseModel):
    friend_id: str
    node_id: str
    message: Optional[str] = Field(default=None, max_length=500)
    deadline_days: int = Field(default=7, ge=1, le=365)


def _uuid(value: str, field: str = "id") -> UUID:
    try:
        return UUID(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {field}")


def _user_public(row) -> dict:
    return {
        "user_id": str(row["user_id"]),
        "display_name": row["display_name"],
        "avatar_url": row["avatar_url"],
        "xp_earned_this_week": int(row["xp_earned_this_week"] or 0),
        "weekly_rank": int(row["weekly_rank"]) if row["weekly_rank"] else None,
    }


async def _ensure_user_exists(other_id: UUID):
    exists = await fetchrow("SELECT id FROM users WHERE id = $1", other_id)
    if not exists:
        raise HTTPException(status_code=404, detail="User not found")


async def _are_friends(user_id: UUID, friend_id: UUID) -> bool:
    row = await fetchrow(
        """
        SELECT 1 FROM friend_connections
        WHERE status = 'accepted'
          AND ((requester_id = $1 AND addressee_id = $2)
            OR (requester_id = $2 AND addressee_id = $1))
        """,
        user_id,
        friend_id,
    )
    return bool(row)


@router.post("/request/{friend_user_id}")
async def send_friend_request(
    friend_user_id: str,
    user_id: str = Depends(get_current_user_id),
):
    requester_id = UUID(user_id)
    addressee_id = _uuid(friend_user_id, "user_id")
    if requester_id == addressee_id:
        raise HTTPException(status_code=400, detail="Cannot friend yourself")
    await _ensure_user_exists(addressee_id)

    reverse = await fetchrow(
        """
        SELECT id, status FROM friend_connections
        WHERE requester_id = $1 AND addressee_id = $2
        """,
        addressee_id,
        requester_id,
    )
    if reverse and reverse["status"] == "pending":
        await execute(
            """
            UPDATE friend_connections
            SET status = 'accepted', updated_at = NOW()
            WHERE id = $1
            """,
            reverse["id"],
        )
        return {"ok": True, "status": "accepted"}

    row = await fetchrow(
        """
        INSERT INTO friend_connections (requester_id, addressee_id, status)
        VALUES ($1, $2, 'pending')
        ON CONFLICT (requester_id, addressee_id)
        DO UPDATE SET status = CASE
                WHEN friend_connections.status = 'blocked' THEN 'blocked'
                ELSE 'pending'
            END,
            updated_at = NOW()
        RETURNING status
        """,
        requester_id,
        addressee_id,
    )
    if row["status"] == "blocked":
        raise HTTPException(status_code=403, detail="Friend request blocked")
    return {"ok": True, "status": row["status"]}


@router.post("/accept/{friend_user_id}")
async def accept_friend_request(
    friend_user_id: str,
    user_id: str = Depends(get_current_user_id),
):
    addressee_id = UUID(user_id)
    requester_id = _uuid(friend_user_id, "user_id")
    row = await fetchrow(
        """
        UPDATE friend_connections
        SET status = 'accepted', updated_at = NOW()
        WHERE requester_id = $1 AND addressee_id = $2 AND status = 'pending'
        RETURNING id
        """,
        requester_id,
        addressee_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Pending friend request not found")
    return {"ok": True, "status": "accepted"}


@router.post("/decline/{friend_user_id}")
async def decline_or_remove_friend(
    friend_user_id: str,
    user_id: str = Depends(get_current_user_id),
):
    me = UUID(user_id)
    other = _uuid(friend_user_id, "user_id")
    row = await fetchrow(
        """
        UPDATE friend_connections
        SET status = 'declined', updated_at = NOW()
        WHERE (requester_id = $1 AND addressee_id = $2)
           OR (requester_id = $2 AND addressee_id = $1)
        RETURNING id
        """,
        me,
        other,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Friend connection not found")
    return {"ok": True, "status": "declined"}


@router.get("")
async def list_friends(user_id: str = Depends(get_current_user_id)):
    rows = await fetch(
        """
        WITH weekly AS (
            SELECT user_id, COALESCE(SUM(amount), 0)::int AS xp_earned_this_week
            FROM xp_log
            WHERE created_at >= date_trunc('week', NOW())
            GROUP BY user_id
        ), ranked AS (
            SELECT
                u.id AS user_id,
                COALESCE(w.xp_earned_this_week, 0)::int AS xp_earned_this_week,
                RANK() OVER (ORDER BY COALESCE(w.xp_earned_this_week, 0) DESC, u.created_at ASC) AS weekly_rank
            FROM users u
            LEFT JOIN weekly w ON w.user_id = u.id
        ), my_friends AS (
            SELECT CASE WHEN requester_id = $1 THEN addressee_id ELSE requester_id END AS friend_id
            FROM friend_connections
            WHERE status = 'accepted' AND (requester_id = $1 OR addressee_id = $1)
        )
        SELECT
            u.id AS user_id,
            COALESCE(u.display_name, u.profile->>'display_name', split_part(u.email, '@', 1), 'Anonymous') AS display_name,
            u.avatar_url,
            ranked.xp_earned_this_week,
            ranked.weekly_rank
        FROM my_friends mf
        JOIN users u ON u.id = mf.friend_id
        JOIN ranked ON ranked.user_id = u.id
        ORDER BY ranked.xp_earned_this_week DESC, display_name
        """,
        UUID(user_id),
    )
    return {"friends": [_user_public(row) for row in rows]}


@router.get("/activity")
async def friends_activity(user_id: str = Depends(get_current_user_id)):
    rows = await fetch(
        """
        WITH my_friends AS (
            SELECT CASE WHEN requester_id = $1 THEN addressee_id ELSE requester_id END AS friend_id
            FROM friend_connections
            WHERE status = 'accepted' AND (requester_id = $1 OR addressee_id = $1)
        )
        SELECT
            p.id,
            p.user_id,
            COALESCE(u.display_name, u.profile->>'display_name', split_part(u.email, '@', 1), 'Anonymous') AS display_name,
            u.avatar_url,
            p.node_id,
            n.title AS node_title,
            p.goal_id,
            p.status,
            p.started_at,
            p.completed_at,
            p.created_at
        FROM ioo_user_progress p
        JOIN my_friends mf ON mf.friend_id = p.user_id
        JOIN users u ON u.id = p.user_id
        LEFT JOIN ioo_nodes n ON n.id = p.node_id
        WHERE p.created_at >= NOW() - INTERVAL '7 days'
           OR p.completed_at >= NOW() - INTERVAL '7 days'
           OR p.started_at >= NOW() - INTERVAL '7 days'
        ORDER BY COALESCE(p.completed_at, p.started_at, p.created_at) DESC
        LIMIT 100
        """,
        UUID(user_id),
    )
    return {
        "activity": [
            {
                "id": str(row["id"]),
                "user_id": str(row["user_id"]),
                "display_name": row["display_name"],
                "avatar_url": row["avatar_url"],
                "node_id": str(row["node_id"]),
                "node_title": row["node_title"],
                "goal_id": str(row["goal_id"]) if row["goal_id"] else None,
                "status": row["status"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    }


@router.get("/requests")
async def pending_friend_requests(user_id: str = Depends(get_current_user_id)):
    rows = await fetch(
        """
        SELECT
            fc.id,
            fc.requester_id AS user_id,
            COALESCE(u.display_name, u.profile->>'display_name', split_part(u.email, '@', 1), 'Anonymous') AS display_name,
            u.avatar_url,
            fc.created_at
        FROM friend_connections fc
        JOIN users u ON u.id = fc.requester_id
        WHERE fc.addressee_id = $1 AND fc.status = 'pending'
        ORDER BY fc.created_at DESC
        """,
        UUID(user_id),
    )
    return {
        "requests": [
            {
                "id": str(row["id"]),
                "user_id": str(row["user_id"]),
                "display_name": row["display_name"],
                "avatar_url": row["avatar_url"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    }


@router.post("/challenge", status_code=status.HTTP_201_CREATED)
async def challenge_friend(
    body: ChallengeCreate,
    user_id: str = Depends(get_current_user_id),
):
    challenger_id = UUID(user_id)
    challengee_id = _uuid(body.friend_id, "friend_id")
    node_id = _uuid(body.node_id, "node_id")
    if challenger_id == challengee_id:
        raise HTTPException(status_code=400, detail="Cannot challenge yourself")
    if not await _are_friends(challenger_id, challengee_id):
        raise HTTPException(status_code=403, detail="You can only challenge accepted friends")

    node = await fetchrow("SELECT id, title FROM ioo_nodes WHERE id = $1 AND is_active = TRUE", node_id)
    if not node:
        raise HTTPException(status_code=404, detail="IOO node not found")

    deadline = datetime.now(timezone.utc) + timedelta(days=body.deadline_days)
    row = await fetchrow(
        """
        INSERT INTO ioo_challenges (challenger_id, challengee_id, node_id, message, deadline)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id, created_at
        """,
        challenger_id,
        challengee_id,
        node_id,
        body.message,
        deadline,
    )
    return {
        "ok": True,
        "challenge": {
            "id": str(row["id"]),
            "challenger_id": str(challenger_id),
            "challengee_id": str(challengee_id),
            "node_id": str(node_id),
            "node_title": node["title"],
            "message": body.message,
            "deadline": deadline,
            "status": "active",
            "created_at": row["created_at"],
        },
    }


@router.get("/challenges")
async def list_challenges(user_id: str = Depends(get_current_user_id)):
    rows = await fetch(
        """
        SELECT
            c.*,
            n.title AS node_title,
            COALESCE(challenger.display_name, challenger.profile->>'display_name', split_part(challenger.email, '@', 1), 'Anonymous') AS challenger_name,
            challenger.avatar_url AS challenger_avatar_url,
            COALESCE(challengee.display_name, challengee.profile->>'display_name', split_part(challengee.email, '@', 1), 'Anonymous') AS challengee_name,
            challengee.avatar_url AS challengee_avatar_url
        FROM ioo_challenges c
        JOIN ioo_nodes n ON n.id = c.node_id
        JOIN users challenger ON challenger.id = c.challenger_id
        JOIN users challengee ON challengee.id = c.challengee_id
        WHERE c.challenger_id = $1 OR c.challengee_id = $1
        ORDER BY c.created_at DESC
        LIMIT 100
        """,
        UUID(user_id),
    )
    return {
        "challenges": [
            {
                "id": str(row["id"]),
                "challenger_id": str(row["challenger_id"]),
                "challenger_name": row["challenger_name"],
                "challenger_avatar_url": row["challenger_avatar_url"],
                "challengee_id": str(row["challengee_id"]),
                "challengee_name": row["challengee_name"],
                "challengee_avatar_url": row["challengee_avatar_url"],
                "node_id": str(row["node_id"]),
                "node_title": row["node_title"],
                "message": row["message"],
                "deadline": row["deadline"],
                "status": row["status"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    }


async def award_completed_challenges(completing_user_id: str, node_id: str) -> list[dict]:
    """Mark active IOO challenges complete for this node and award bonus XP once."""
    completed = await fetch(
        """
        UPDATE ioo_challenges
        SET status = 'completed'
        WHERE status = 'active'
          AND node_id = $1
          AND (challenger_id = $2 OR challengee_id = $2)
        RETURNING id, challenger_id, challengee_id, node_id
        """,
        UUID(node_id),
        UUID(completing_user_id),
    )
    awards = []
    for row in completed:
        await execute(
            "INSERT INTO xp_log (user_id, amount, reason, ref_id) VALUES ($1, 50, $2, $3)",
            row["challenger_id"],
            "challenge_sent_completed",
            str(row["node_id"]),
        )
        await execute(
            "INSERT INTO xp_log (user_id, amount, reason, ref_id) VALUES ($1, 100, $2, $3)",
            row["challengee_id"],
            "challenge_completed",
            str(row["node_id"]),
        )
        awards.append(
            {
                "challenge_id": str(row["id"]),
                "challenger_id": str(row["challenger_id"]),
                "challengee_id": str(row["challengee_id"]),
                "node_id": str(row["node_id"]),
                "challenger_xp": 50,
                "challengee_xp": 100,
            }
        )
    return awards
