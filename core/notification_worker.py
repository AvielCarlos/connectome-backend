"""
Notification Worker
Background asyncio task that polls Redis every 60 seconds for due
scheduled_notifications and delivers them via the Expo Push API.

Delivery flow:
  1. Poll Redis sorted set `notifications:pending` for items due now
  2. Fetch the notification row from DB (includes user_id)
  3. Look up the user's expo push_token
  4. POST to https://exp.host/--/api/v2/push/send (batch of up to 100)
  5. Mark as sent in DB, remove from Redis
  6. Log Expo receipt ticket IDs for later receipt checking
"""

import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID
from typing import List, Dict, Any, Optional

from core.database import fetchrow, execute, fetch
from core.redis_client import get_redis

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
EXPO_BATCH_SIZE = 100  # Expo accepts up to 100 messages per request

_worker_task: asyncio.Task = None


# ---------------------------------------------------------------------------
# Expo Push Delivery
# ---------------------------------------------------------------------------

async def _send_expo_notifications(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Send a batch of push messages to Expo.
    Each message: { to, title, body, data, sound, badge }
    Returns Expo ticket list (one per message).
    """
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                EXPO_PUSH_URL,
                json=messages,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip, deflate",
                    "Content-Type": "application/json",
                    "Expo-SDK-Version": "50",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("data", [])
            else:
                logger.warning(f"[NotifWorker] Expo API error {resp.status_code}: {resp.text[:200]}")
                return []
    except ImportError:
        logger.warning("[NotifWorker] httpx not installed — cannot deliver push notifications. Run: pip install httpx")
        return []
    except Exception as e:
        logger.warning(f"[NotifWorker] Expo push delivery failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Main processing logic
# ---------------------------------------------------------------------------

async def _process_due_notifications() -> int:
    """
    Check the Redis sorted set `notifications:pending` for entries due now.
    Fetches push tokens, batches Expo deliveries, marks as sent.
    Returns the number of notifications processed.
    """
    r = await get_redis()
    now_ts = datetime.now(timezone.utc).timestamp()

    # zrangebyscore returns all members with score (= scheduled_for timestamp) <= now
    due_ids = await r.zrangebyscore("notifications:pending", 0, now_ts)
    if not due_ids:
        return 0

    # Collect rows for batch delivery
    pending_rows = []
    for notification_id in due_ids:
        try:
            row = await fetchrow(
                """
                SELECT n.id, n.user_id, n.goal_id, n.message, n.scheduled_for, n.sent,
                       u.push_token, u.profile
                FROM scheduled_notifications n
                JOIN users u ON u.id = n.user_id
                WHERE n.id = $1 AND n.sent = FALSE
                """,
                UUID(notification_id),
            )
            if row:
                pending_rows.append(row)
            else:
                # Already sent or deleted — clean up Redis
                await r.zrem("notifications:pending", notification_id)
        except Exception as e:
            logger.warning(f"[NotifWorker] Failed to fetch notification {notification_id}: {e}")

    if not pending_rows:
        return 0

    # Separate into push-deliverable and log-only
    expo_messages = []
    expo_notif_ids = []  # parallel list matching expo_messages

    for row in pending_rows:
        push_token = row.get("push_token")
        notif_id = str(row["id"])
        message = row.get("message") or "Ora has something for you."

        # Extract display_name for personalisation
        import json as _json
        profile = row.get("profile") or {}
        if isinstance(profile, str):
            try:
                profile = _json.loads(profile)
            except Exception:
                profile = {}
        display_name = profile.get("display_name", "")
        title = f"Hey {display_name} ✦" if display_name else "Ora ✦"

        if push_token and push_token.startswith("ExponentPushToken["):
            expo_messages.append({
                "to": push_token,
                "title": title,
                "body": message,
                "sound": "default",
                "data": {
                    "notification_id": notif_id,
                    "goal_id": str(row["goal_id"]) if row.get("goal_id") else None,
                    "type": "reengagement",
                },
                "badge": 1,
            })
            expo_notif_ids.append(notif_id)
        else:
            # No push token — log-only delivery
            user_short = str(row["user_id"])[:8]
            goal_short = str(row["goal_id"])[:8] if row.get("goal_id") else "no-goal"
            msg_preview = message[:100]
            logger.info(
                f"📲 [NotifWorker] LOG-ONLY (no push token) → "
                f"user={user_short} goal={goal_short} | {msg_preview}"
            )
            # Mark as sent anyway
            await _mark_sent(notif_id, r)

    # Batch send to Expo (in chunks of EXPO_BATCH_SIZE)
    processed = 0
    for i in range(0, len(expo_messages), EXPO_BATCH_SIZE):
        batch_msgs = expo_messages[i : i + EXPO_BATCH_SIZE]
        batch_ids = expo_notif_ids[i : i + EXPO_BATCH_SIZE]

        tickets = await _send_expo_notifications(batch_msgs)

        for j, notif_id in enumerate(batch_ids):
            ticket = tickets[j] if j < len(tickets) else {}
            status = ticket.get("status", "unknown")
            ticket_id = ticket.get("id", "")

            if status == "ok":
                logger.info(
                    f"📲 [NotifWorker] SENT → id={notif_id[:8]} "
                    f"expo_ticket={ticket_id[:16] if ticket_id else 'n/a'}"
                )
            elif status == "error":
                err_detail = ticket.get("details", {}).get("error", "")
                logger.warning(
                    f"[NotifWorker] Expo error for {notif_id[:8]}: "
                    f"{ticket.get('message', '')} ({err_detail})"
                )
                # DeviceNotRegistered: clear token from user
                if err_detail == "DeviceNotRegistered":
                    try:
                        row_for_clear = next(
                            (r_ for r_ in pending_rows if str(r_["id"]) == notif_id), None
                        )
                        if row_for_clear:
                            await execute(
                                "UPDATE users SET push_token = NULL WHERE id = $1",
                                row_for_clear["user_id"],
                            )
                            logger.info(f"[NotifWorker] Cleared stale token for user {str(row_for_clear['user_id'])[:8]}")
                    except Exception:
                        pass
            else:
                logger.info(f"📲 [NotifWorker] SENT (no receipt) → id={notif_id[:8]}")

            await _mark_sent(notif_id, r)
            processed += 1

    # Also count log-only
    processed += len(pending_rows) - len(expo_notif_ids)
    return processed


async def _mark_sent(notification_id: str, r) -> None:
    """Mark a notification as sent in DB and remove from Redis queue."""
    try:
        await execute(
            "UPDATE scheduled_notifications SET sent = TRUE WHERE id = $1",
            UUID(notification_id),
        )
        await r.zrem("notifications:pending", notification_id)
    except Exception as e:
        logger.warning(f"[NotifWorker] Failed to mark sent {notification_id[:8]}: {e}")


# ---------------------------------------------------------------------------
# Retention mechanics helpers
# ---------------------------------------------------------------------------

async def _schedule_notification(
    user_id,
    message: str,
    goal_id=None,
    delay_seconds: float = 0,
    notif_type: str = "retention",
):
    """
    Utility: insert a scheduled_notification row + add to Redis sorted set.
    delay_seconds: how far in the future to schedule (0 = now).
    """
    import uuid as _uuid
    from datetime import timedelta

    scheduled_for = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)

    row = await fetchrow(
        """
        INSERT INTO scheduled_notifications
            (user_id, goal_id, message, scheduled_for, sent)
        VALUES ($1, $2, $3, $4, FALSE)
        RETURNING id
        """,
        user_id,
        goal_id,
        message,
        scheduled_for,
    )
    if row:
        notif_id = str(row["id"])
        r = await get_redis()
        await r.zadd("notifications:pending", {notif_id: scheduled_for.timestamp()})
        logger.info(f"[NotifWorker] Scheduled {notif_type} notification for user {str(user_id)[:8]}")


async def _generate_daily_checkin_message(user_id, profile: dict, goals: list) -> str:
    """
    Generate a personalised morning check-in message from Ora.
    Falls back to a mock if OpenAI unavailable.
    """
    from core.config import settings

    display_name = profile.get("display_name", "")
    name_part = f" {display_name}" if display_name else ""

    if not goals:
        return (
            f"Good morning{name_part}. No active goals yet — "
            "want to set one today? Even a small intention makes a difference."
        )

    # Pick the most progressed active goal
    active = [g for g in goals if g.get("status") == "active"]
    if not active:
        return f"Good morning{name_part}. All goals are done — that's rare. Time to set a new one."

    top_goal = max(active, key=lambda g: g.get("progress", 0.0))
    pct = round((top_goal.get("progress") or 0.0) * 100)

    if settings.has_openai:
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
            prompt = f"""You are Ora, a warm AI coach. Write a 1-2 sentence morning check-in message.
User goal: "{top_goal.get('title', 'their goal')}" ({pct}% done)
Name: "{display_name or 'the user'}"
Be specific to the goal. No generic motivational phrases. Be brief and warm."""
            resp = await client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.75,
                max_tokens=100,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.debug(f"Daily check-in LLM failed: {e}")

    # Mock fallback
    return (
        f"Good morning{name_part}. "
        f"'{top_goal.get('title', 'your goal')}' is {pct}% done. "
        f"What’s one thing you can do today to move it forward?"
    )


async def _generate_weekly_summary(
    user_id, profile: dict, screens_seen: int, goals_progressed: int, top_interests: list
) -> str:
    """
    Ora writes a brief personal week-in-review.
    """
    from core.config import settings

    display_name = profile.get("display_name", "")
    name_part = f" {display_name}" if display_name else ""

    interests_str = ", ".join(top_interests[:3]) if top_interests else "various topics"

    if settings.has_openai:
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
            prompt = f"""You are Ora, a thoughtful AI coach. Write a brief 3-4 sentence personal week-in-review.
User data (this week):
- Screens engaged with: {screens_seen}
- Goals with new progress: {goals_progressed}
- Top interests engaged: {interests_str}
- Name: "{display_name or 'the user'}"

Be specific to the data. Warm, direct tone. Acknowledge the week’s theme and name one priority for next week.
Do NOT use the word 'journey'. Keep it under 80 words."""
            resp = await client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.75,
                max_tokens=120,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.debug(f"Weekly summary LLM failed: {e}")

    # Mock fallback
    return (
        f"Your week in review{name_part}: you engaged with {screens_seen} screens "
        f"and made progress on {goals_progressed} goal(s). "
        f"The theme this week: {interests_str}. "
        f"Next week, pick one thing to go deeper on."
    )


async def run_daily_checkins():
    """
    Send morning check-in messages to users with active goals
    who haven't received one today.
    Targets: users active in the last 7 days, with push tokens, between 07:00-09:00 local.
    For simplicity, we send in UTC morning (07:00-09:00 UTC).
    """
    now = datetime.now(timezone.utc)
    hour = now.hour
    if not (7 <= hour < 9):
        return  # Only send in the morning window

    today = now.date()

    # Find eligible users: active in 7 days, has push token, active goals, no checkin today
    try:
        users = await fetch(
            """
            SELECT u.id, u.profile, u.push_token, u.last_daily_checkin_at
            FROM users u
            WHERE u.push_token IS NOT NULL
              AND u.last_active > NOW() - INTERVAL '7 days'
              AND (u.last_daily_checkin_at IS NULL
                   OR DATE(u.last_daily_checkin_at) < $1)
            LIMIT 100
            """,
            today,
        )
    except Exception as e:
        logger.warning(f"[DailyCheckin] User query failed: {e}")
        return

    sent_count = 0
    for user in users:
        user_id = user["id"]
        try:
            import json as _j
            profile = user.get("profile") or {}
            if isinstance(profile, str):
                profile = _j.loads(profile)

            # Get active goals
            goals = await fetch(
                "SELECT title, progress, status FROM goals WHERE user_id = $1 AND status = 'active' LIMIT 5",
                user_id,
            )
            goals_list = [{"title": g["title"], "progress": g["progress"] or 0.0, "status": g["status"]} for g in goals]

            message = await _generate_daily_checkin_message(user_id, profile, goals_list)

            await _schedule_notification(user_id, message, notif_type="daily_checkin")
            await execute(
                "UPDATE users SET last_daily_checkin_at = NOW() WHERE id = $1",
                user_id,
            )
            sent_count += 1
        except Exception as e:
            logger.warning(f"[DailyCheckin] Failed for user {str(user_id)[:8]}: {e}")

    if sent_count:
        logger.info(f"[DailyCheckin] Sent {sent_count} morning check-in(s)")


async def run_weekly_summaries():
    """
    Send weekly summaries on Sunday evening (17:00-19:00 UTC).
    Covers screens, goals, and Ora's narrative for the week.
    """
    now = datetime.now(timezone.utc)
    # Sunday = 6 in Python weekday()
    if now.weekday() != 6 or not (17 <= now.hour < 19):
        return

    from datetime import timedelta, date as _date

    week_start = (_date.today() - timedelta(days=6)).isoformat()
    week_end = _date.today().isoformat()

    try:
        users = await fetch(
            """
            SELECT u.id, u.profile, u.push_token, u.last_weekly_summary_at
            FROM users u
            WHERE u.push_token IS NOT NULL
              AND u.last_active > NOW() - INTERVAL '14 days'
              AND (u.last_weekly_summary_at IS NULL
                   OR u.last_weekly_summary_at < NOW() - INTERVAL '6 days')
            LIMIT 100
            """,
        )
    except Exception as e:
        logger.warning(f"[WeeklySummary] User query failed: {e}")
        return

    sent_count = 0
    for user in users:
        user_id = user["id"]
        try:
            import json as _j
            profile = user.get("profile") or {}
            if isinstance(profile, str):
                profile = _j.loads(profile)

            # Count screens this week
            screens_seen = await fetchrow(
                """
                SELECT COUNT(*) as cnt FROM interactions
                WHERE user_id = $1 AND created_at > NOW() - INTERVAL '7 days'
                """,
                user_id,
            )
            screens_count = int(screens_seen["cnt"] or 0) if screens_seen else 0

            # Count goals with progress this week
            goals_progressed = await fetchrow(
                """
                SELECT COUNT(*) as cnt FROM goals
                WHERE user_id = $1 AND progress > 0 AND created_at > NOW() - INTERVAL '30 days'
                """,
                user_id,
            )
            goals_count = int(goals_progressed["cnt"] or 0) if goals_progressed else 0

            # Top interests: most-seen agent types this week
            interest_rows = await fetch(
                """
                SELECT ss.agent_type, COUNT(*) as cnt
                FROM interactions i JOIN screen_specs ss ON ss.id = i.screen_spec_id
                WHERE i.user_id = $1 AND i.rating >= 4 AND i.created_at > NOW() - INTERVAL '7 days'
                GROUP BY ss.agent_type ORDER BY cnt DESC LIMIT 3
                """,
                user_id,
            )
            top_interests = [
                r["agent_type"].replace("Agent", "").replace("_", " ").lower()
                for r in interest_rows if r.get("agent_type")
            ]

            narrative = await _generate_weekly_summary(user_id, profile, screens_count, goals_count, top_interests)

            # Store in weekly_summaries table
            await execute(
                """
                INSERT INTO weekly_summaries
                    (user_id, week_start, week_end, screens_seen, goals_progressed, top_interests, ora_narrative)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                user_id,
                week_start,
                week_end,
                screens_count,
                goals_count,
                __import__('json').dumps(top_interests),
                narrative,
            )

            # Send as push notification
            short_summary = narrative[:200] + ("…" if len(narrative) > 200 else "")
            await _schedule_notification(
                user_id, short_summary, notif_type="weekly_summary"
            )
            await execute(
                "UPDATE users SET last_weekly_summary_at = NOW() WHERE id = $1",
                user_id,
            )
            sent_count += 1
        except Exception as e:
            logger.warning(f"[WeeklySummary] Failed for user {str(user_id)[:8]}: {e}")

    if sent_count:
        logger.info(f"[WeeklySummary] Sent {sent_count} weekly summaries")


async def run_reengagement_notifications():
    """
    Send re-engagement pushes to users who haven't been back in 2, 5, or 10 days.
    Each tier gets a different Ora message tone.
    """
    now = datetime.now(timezone.utc)
    # Only run once per day (morning UTC)
    if now.hour != 10:
        return

    tiers = [
        (2, "You haven’t been back in a couple days"),
        (5, "It’s been a few days since you opened Connectome"),
        (10, "It’s been 10 days — Ora hasn’t forgotten you"),
    ]

    for days_away, context_phrase in tiers:
        from datetime import timedelta
        try:
            cutoff_from = now - timedelta(days=days_away + 1)
            cutoff_to = now - timedelta(days=days_away)

            users = await fetch(
                """
                SELECT u.id, u.profile, u.push_token
                FROM users u
                WHERE u.push_token IS NOT NULL
                  AND u.last_active IS NOT NULL
                  AND u.last_active BETWEEN $1 AND $2
                LIMIT 50
                """,
                cutoff_from,
                cutoff_to,
            )
        except Exception as e:
            logger.warning(f"[ReEngagement] Query failed (tier={days_away}): {e}")
            continue

        for user in users:
            user_id = user["id"]
            try:
                import json as _j
                profile = user.get("profile") or {}
                if isinstance(profile, str):
                    profile = _j.loads(profile)
                display_name = profile.get("display_name", "")
                name_part = f" {display_name}" if display_name else ""

                # Get their most progressed active goal for personalisation
                top_goal = await fetchrow(
                    "SELECT title, progress FROM goals WHERE user_id = $1 AND status = 'active' ORDER BY progress DESC LIMIT 1",
                    user_id,
                )

                if top_goal:
                    pct = round((top_goal["progress"] or 0.0) * 100)
                    message = (
                        f"{context_phrase}{name_part}. "
                        f"'{top_goal['title']}' is waiting at {pct}% — you were so close."
                    )
                else:
                    message = (
                        f"{context_phrase}{name_part}. Ora has new things waiting for you."
                    )

                await _schedule_notification(user_id, message, notif_type="reengagement")
            except Exception as e:
                logger.warning(f"[ReEngagement] Failed for user {str(user_id)[:8]}: {e}")

    logger.debug("[ReEngagement] Check complete")


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

async def run_notification_worker():
    """
    Main worker loop. Polls every 60 seconds.
    Also runs retention mechanics (daily check-ins, weekly summaries,
    re-engagement notifications) on appropriate schedules.
    Handles cancellation gracefully.
    """
    logger.info("🔔 Notification worker started (polling every 60s)")
    _retention_tick = 0
    while True:
        try:
            count = await _process_due_notifications()
            if count > 0:
                logger.info(f"[NotifWorker] Processed {count} notification(s)")

            # Run retention mechanics every 10 minutes (every 10th tick)
            _retention_tick += 1
            if _retention_tick % 10 == 0:
                try:
                    await run_daily_checkins()
                except Exception as _e:
                    logger.debug(f"[DailyCheckin] Error: {_e}")
                try:
                    await run_weekly_summaries()
                except Exception as _e:
                    logger.debug(f"[WeeklySummary] Error: {_e}")
                try:
                    await run_reengagement_notifications()
                except Exception as _e:
                    logger.debug(f"[ReEngagement] Error: {_e}")

        except asyncio.CancelledError:
            logger.info("🔕 Notification worker stopped")
            break
        except Exception as e:
            logger.warning(f"[NotifWorker] Unexpected error: {e}")

        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.info("🔕 Notification worker stopped during sleep")
            break


def start_notification_worker() -> asyncio.Task:
    """
    Start the notification worker as a background asyncio task.
    Call this from the app lifespan startup.
    Returns the Task handle (store it to cancel on shutdown).
    """
    global _worker_task
    _worker_task = asyncio.create_task(run_notification_worker())
    return _worker_task


def stop_notification_worker():
    """
    Cancel the background notification worker.
    Call this from the app lifespan shutdown.
    """
    global _worker_task
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
        _worker_task = None
        logger.info("Notification worker cancellation requested")
