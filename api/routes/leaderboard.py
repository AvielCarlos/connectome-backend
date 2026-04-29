"""
Leaderboard API — weekly and all-time XP rankings.

Weekly leaderboard resets every Monday 00:00 UTC by using PostgreSQL
`date_trunc('week', NOW())`, which is Monday-anchored for ISO weeks.
"""

from uuid import UUID

from fastapi import APIRouter, Depends

from api.middleware import get_current_user_id
from core.database import fetch

router = APIRouter(prefix="/api/leaderboard", tags=["leaderboard"])


LEADERBOARD_SELECT = """
WITH weekly AS (
    SELECT
        x.user_id,
        COALESCE(SUM(x.amount), 0)::int AS xp_earned_this_week
    FROM xp_log x
    WHERE x.created_at >= date_trunc('week', NOW())
    GROUP BY x.user_id
), totals AS (
    SELECT
        x.user_id,
        COALESCE(SUM(x.amount), 0)::int AS total_xp
    FROM xp_log x
    GROUP BY x.user_id
), top_goals AS (
    SELECT DISTINCT ON (x.user_id)
        x.user_id,
        json_build_object(
            'node_id', n.id,
            'title', n.title,
            'xp', SUM(x.amount)::int
        ) AS top_ioo_goal
    FROM xp_log x
    JOIN ioo_nodes n ON n.id = CASE
        WHEN x.ref_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        THEN x.ref_id::uuid
        ELSE NULL
    END
    WHERE x.created_at >= date_trunc('week', NOW())
      AND x.reason IN ('ioo_node_complete', 'challenge_completed', 'challenge_sent_completed')
      AND x.ref_id IS NOT NULL
    GROUP BY x.user_id, n.id, n.title
    ORDER BY x.user_id, SUM(x.amount) DESC, n.title
), ranked AS (
    SELECT
        u.id AS user_id,
        COALESCE(u.display_name, u.profile->>'display_name', split_part(u.email, '@', 1), 'Anonymous') AS display_name,
        u.avatar_url,
        COALESCE(w.xp_earned_this_week, 0)::int AS xp_earned_this_week,
        COALESCE(t.total_xp, 0)::int AS total_xp,
        tg.top_ioo_goal,
        RANK() OVER (ORDER BY COALESCE(w.xp_earned_this_week, 0) DESC, COALESCE(t.total_xp, 0) DESC, u.created_at ASC) AS weekly_rank,
        RANK() OVER (ORDER BY COALESCE(t.total_xp, 0) DESC, u.created_at ASC) AS all_time_rank
    FROM users u
    LEFT JOIN weekly w ON w.user_id = u.id
    LEFT JOIN totals t ON t.user_id = u.id
    LEFT JOIN top_goals tg ON tg.user_id = u.id
)
"""


def _row_to_entry(row, rank_field: str) -> dict:
    return {
        "rank": int(row[rank_field]),
        "user_id": str(row["user_id"]),
        "display_name": row["display_name"],
        "avatar_url": row["avatar_url"],
        "xp_earned_this_week": int(row["xp_earned_this_week"] or 0),
        "total_xp": int(row["total_xp"] or 0),
        "top_ioo_goal": row["top_ioo_goal"],
    }


@router.get("/weekly")
async def weekly_leaderboard(user_id: str = Depends(get_current_user_id)):
    """Top 50 users by XP earned this week."""
    rows = await fetch(
        LEADERBOARD_SELECT
        + """
        SELECT * FROM ranked
        WHERE xp_earned_this_week > 0 OR total_xp > 0
        ORDER BY weekly_rank, total_xp DESC
        LIMIT 50
        """
    )
    return {"leaderboard": [_row_to_entry(row, "weekly_rank") for row in rows]}


@router.get("/weekly/friends")
async def weekly_friends_leaderboard(user_id: str = Depends(get_current_user_id)):
    """Current user's accepted friends ranked by XP earned this week."""
    rows = await fetch(
        LEADERBOARD_SELECT
        + """
        , my_friends AS (
            SELECT CASE
                WHEN requester_id = $1 THEN addressee_id
                ELSE requester_id
            END AS friend_id
            FROM friend_connections
            WHERE status = 'accepted'
              AND (requester_id = $1 OR addressee_id = $1)
        )
        SELECT ranked.*
        FROM ranked
        JOIN my_friends mf ON mf.friend_id = ranked.user_id
        ORDER BY weekly_rank, total_xp DESC
        LIMIT 50
        """,
        UUID(user_id),
    )
    return {"leaderboard": [_row_to_entry(row, "weekly_rank") for row in rows]}


@router.get("/all-time")
async def all_time_leaderboard(user_id: str = Depends(get_current_user_id)):
    """Top 50 users by lifetime XP."""
    rows = await fetch(
        LEADERBOARD_SELECT
        + """
        SELECT * FROM ranked
        WHERE total_xp > 0
        ORDER BY all_time_rank
        LIMIT 50
        """
    )
    return {"leaderboard": [_row_to_entry(row, "all_time_rank") for row in rows]}
