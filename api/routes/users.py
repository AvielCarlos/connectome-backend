"""
Users API Routes
Registration, login, profile management.
"""

import logging
import json
from uuid import UUID
from datetime import datetime, timezone

from core.config import settings
from fastapi import APIRouter, HTTPException, status, Depends

from typing import Optional, List
from pydantic import BaseModel
from core.models import UserCreate, UserLogin, TokenResponse, UserProfile, UserUpdate
from core.database import fetchrow, execute
from api.middleware import (
    hash_password,
    verify_password,
    create_access_token,
    get_current_user_id,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/users", tags=["users"])


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: UserCreate):
    """Register a new user."""
    # Check if email already exists
    existing = await fetchrow(
        "SELECT id FROM users WHERE email = $1", body.email
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    hashed = hash_password(body.password)
    profile = {}
    if body.display_name:
        profile["display_name"] = body.display_name

    import json
    row = await fetchrow(
        """
        INSERT INTO users (email, hashed_password, profile, last_active)
        VALUES ($1, $2, $3, NOW())
        RETURNING id
        """,
        body.email,
        hashed,
        json.dumps(profile),
    )

    user_id = str(row["id"])
    token = create_access_token(user_id)
    logger.info(f"New user registered: {user_id[:8]}")

    return TokenResponse(access_token=token, user_id=user_id)


@router.post("/login", response_model=TokenResponse)
async def login(body: UserLogin):
    """Authenticate and return JWT token."""
    row = await fetchrow(
        "SELECT id, hashed_password FROM users WHERE email = $1", body.email
    )
    if not row or not verify_password(body.password, row["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    user_id = str(row["id"])
    await execute(
        "UPDATE users SET last_active = NOW() WHERE id = $1", UUID(user_id)
    )

    token = create_access_token(user_id)
    return TokenResponse(access_token=token, user_id=user_id)


@router.get("/me", response_model=UserProfile)
async def get_profile(user_id: str = Depends(get_current_user_id)):
    """Get the current user's profile."""
    row = await fetchrow(
        """
        SELECT id, email, subscription_tier, fulfilment_score,
               profile, created_at, last_active
        FROM users WHERE id = $1
        """,
        UUID(user_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        _email = (row["email"] or "").lower()
        _admin_list = getattr(settings, "admin_email_list", ["carlosandromeda8@gmail.com"])
        _is_admin = _email in _admin_list
    except Exception:
        _is_admin = False
    try:
        _raw = row["profile"]
        if isinstance(_raw, str):
            import json as _json
            profile_data = _json.loads(_raw) if _raw else {}
        elif _raw is None:
            profile_data = {}
        else:
            profile_data = dict(_raw)
    except Exception:
        profile_data = {}
    profile_data["is_admin"] = _is_admin
    return UserProfile(
        id=row["id"],
        email=row["email"],
        is_admin=_is_admin,
        subscription_tier=row["subscription_tier"],
        fulfilment_score=row["fulfilment_score"] or 0.0,
        profile=profile_data,
        created_at=row["created_at"],
        last_active=row["last_active"],
    )


@router.patch("/me", response_model=UserProfile)
async def update_profile(
    body: UserUpdate,
    user_id: str = Depends(get_current_user_id),
):
    """Update user profile fields."""
    import json

    row = await fetchrow(
        "SELECT profile FROM users WHERE id = $1", UUID(user_id)
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    profile = dict(row["profile"]) if row["profile"] else {}

    if body.display_name is not None:
        profile["display_name"] = body.display_name
    if body.bio is not None:
        profile["bio"] = body.bio
    if body.interests is not None:
        profile["interests"] = body.interests
    if body.goals_text is not None:
        profile["goals_text"] = body.goals_text
    if body.location is not None:
        profile["location"] = body.location

    updated = await fetchrow(
        """
        UPDATE users
        SET profile = $1, last_active = NOW()
        WHERE id = $2
        RETURNING id, email, subscription_tier, fulfilment_score,
                  profile, created_at, last_active
        """,
        json.dumps(profile),
        UUID(user_id),
    )

    # Invalidate user model cache
    from core.redis_client import redis_delete
    await redis_delete(f"user_model:{user_id}")

    return UserProfile(
        id=updated["id"],
        email=updated["email"],
        subscription_tier=updated["subscription_tier"],
        fulfilment_score=updated["fulfilment_score"] or 0.0,
        profile=dict(updated["profile"]) if updated["profile"] else {},
        created_at=updated["created_at"],
        last_active=updated["last_active"],
    )


# ---------------------------------------------------------------------------
# Push Token Registration
# ---------------------------------------------------------------------------

class PushTokenBody(BaseModel):
    push_token: str


@router.post("/push-token", status_code=200)
async def register_push_token(
    body: PushTokenBody,
    user_id: str = Depends(get_current_user_id),
):
    """
    Register or update the user's Expo push notification token.
    The mobile client should call this on app launch after requesting
    notification permissions.
    """
    token = body.push_token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Push token cannot be empty")

    # Basic validation: Expo tokens start with ExponentPushToken[
    if not token.startswith("ExponentPushToken[") and not token.startswith("ExpoPushToken["):
        logger.warning(f"Unusual push token format from user {user_id[:8]}: {token[:40]}")

    await execute(
        """
        UPDATE users
        SET push_token = $1, push_token_updated_at = NOW()
        WHERE id = $2
        """,
        token,
        UUID(user_id),
    )

    # Invalidate user model cache
    from core.redis_client import redis_delete
    await redis_delete(f"user_model:{user_id}")

    logger.info(f"Push token registered for user {user_id[:8]}")
    return {"ok": True, "registered": True}


# ---------------------------------------------------------------------------
# Onboarding completion
# ---------------------------------------------------------------------------

class OnboardingCompleteBody(BaseModel):
    domain_preference: Optional[str] = None   # iVive | Eviva | Aventi
    initial_goal: Optional[str] = None
    display_name: Optional[str] = None
    interests: Optional[List[str]] = None


@router.post("/onboarding-complete", status_code=200)
async def complete_onboarding(
    body: OnboardingCompleteBody,
    user_id: str = Depends(get_current_user_id),
):
    """
    Mark onboarding as completed and apply any preferences collected during the flow.
    Called once after the user finishes the onboarding screen.
    """
    import json as _json

    uid = UUID(user_id)

    # Load existing profile
    row = await fetchrow("SELECT profile FROM users WHERE id = $1", uid)
    profile = {}
    if row and row["profile"]:
        raw = row["profile"]
        profile = _json.loads(raw) if isinstance(raw, str) else dict(raw)

    if body.display_name:
        profile["display_name"] = body.display_name
    if body.domain_preference:
        profile["preferred_domain"] = body.domain_preference
    if body.interests:
        profile["interests"] = body.interests
    if body.initial_goal:
        profile["onboarding_goal"] = body.initial_goal

    await execute(
        """
        UPDATE users
        SET profile = $1,
            onboarding_completed = TRUE,
            onboarding_completed_at = NOW(),
            last_active = NOW()
        WHERE id = $2
        """,
        _json.dumps(profile),
        uid,
    )

    # Create the initial goal if provided
    if body.initial_goal and body.initial_goal.strip():
        await execute(
            """
            INSERT INTO goals (user_id, title, domain, status)
            VALUES ($1, $2, $3, 'active')
            ON CONFLICT DO NOTHING
            """,
            uid,
            body.initial_goal.strip()[:200],
            body.domain_preference or "iVive",
        )
        logger.info(f"Created onboarding goal for user {user_id[:8]}: {body.initial_goal[:40]}")

    # Invalidate user model cache
    from core.redis_client import redis_delete
    await redis_delete(f"user_model:{user_id}")

    logger.info(f"Onboarding completed for user {user_id[:8]}")
    return {"ok": True, "onboarding_completed": True}


@router.get("/onboarding-status")
async def get_onboarding_status(user_id: str = Depends(get_current_user_id)):
    """Check whether the user has completed onboarding."""
    row = await fetchrow(
        "SELECT onboarding_completed FROM users WHERE id = $1",
        UUID(user_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return {"onboarding_completed": bool(row["onboarding_completed"])}


@router.delete("/me", status_code=204)
async def delete_account(user_id: str = Depends(get_current_user_id)):
    """Permanently delete the authenticated user's account and all associated data."""
    uid = UUID(user_id)
    # Delete in dependency order
    await execute("DELETE FROM ground_truth_labels WHERE user_id = $1", uid)
    await execute("DELETE FROM exit_classifications WHERE user_id = $1", uid)
    await execute("DELETE FROM scheduled_notifications WHERE user_id = $1", uid)
    await execute("DELETE FROM session_summaries WHERE user_id = $1", uid)
    await execute("DELETE FROM revenue_events WHERE user_id = $1", uid)
    await execute("DELETE FROM interactions WHERE user_id = $1", uid)
    await execute("DELETE FROM goals WHERE user_id = $1", uid)
    await execute("DELETE FROM users WHERE id = $1", uid)
    # Clear cache
    from core.redis_client import redis_delete
    await redis_delete(f"user_model:{user_id}")
    logger.info(f"Account deleted: {user_id}")


@router.get("/forecast")
async def get_fulfilment_forecast(
    user_id: str = Depends(get_current_user_id),
):
    """
    Ora generates a weekly fulfilment forecast based on recent interactions and goals.
    """
    import json
    from datetime import datetime, timezone, timedelta
    from ora.brain import get_brain
    from core.database import fetch

    uid = UUID(user_id)

    # Load recent interactions (last 7 days)
    interactions = await fetch(
        """
        SELECT exit_point, rating, created_at
        FROM interactions
        WHERE user_id = $1
          AND created_at > NOW() - INTERVAL '7 days'
        ORDER BY created_at DESC
        LIMIT 50
        """,
        uid,
    )

    # Load goals
    goals = await fetch(
        "SELECT title, domain, progress, status FROM goals WHERE user_id = $1 LIMIT 10",
        uid,
    )

    # Load user profile for context
    row = await fetchrow(
        "SELECT profile FROM users WHERE id = $1", uid
    )
    if row and row["profile"]:
        raw_profile = row["profile"]
        if isinstance(raw_profile, str):
            import json as _json
            profile = _json.loads(raw_profile)
        else:
            profile = dict(raw_profile)
    else:
        profile = {}

    brain = get_brain()

    # Default forecast for no-OpenAI mode
    default_forecast = {
        "week_prediction": "This week you're likely to feel a pull toward deeper engagement. Lean into the content that challenges you slightly.",
        "neglected_domain": "iVive",
        "neglected_domain_note": "Your inner growth domain has been quiet. Even 5 minutes of reflection today could shift your trajectory.",
        "top_recommendation": "Try one iVive activity today — a journal entry, a mindfulness moment, or a reflective conversation.",
        "trend": "stable",
        "fulfilment_delta_7d": 0.02,
    }

    if not brain._openai:
        return default_forecast

    try:
        # Summarise interaction data
        interaction_summary = f"{len(interactions)} interactions in the last 7 days."
        if interactions:
            ratings = [r["rating"] for r in interactions if r.get("rating")]
            if ratings:
                avg = sum(ratings) / len(ratings)
                interaction_summary += f" Average rating: {avg:.1f}/5."

        # Summarise goals
        goal_summary = ", ".join([f"{g['title']} ({g['domain']}, {int(g['progress']*100)}%)" for g in goals]) or "No goals set"

        # Find neglected domain
        domain_interactions = {"iVive": 0, "Eviva": 0, "Aventi": 0}
        for row_ in interactions:
            ep = str(row_.get("exit_point", ""))
            for domain in domain_interactions:
                if domain.lower() in ep.lower():
                    domain_interactions[domain] += 1

        neglected = min(domain_interactions, key=domain_interactions.get)

        system = (
            "You are Ora, a personal AI focused on human fulfilment. "
            "Generate a weekly forecast based on the user's recent engagement data. "
            "Be specific, warm, and insightful. Return JSON only."
        )
        user_msg = (
            f"User: {profile.get('display_name', 'Anonymous')}\n"
            f"Interactions: {interaction_summary}\n"
            f"Goals: {goal_summary}\n"
            f"Least engaged domain: {neglected}\n\n"
            "Generate a forecast JSON with keys: "
            "week_prediction (string), neglected_domain (string), "
            "neglected_domain_note (string), top_recommendation (string), "
            "trend ('improving'|'declining'|'stable'), fulfilment_delta_7d (float -0.1 to 0.1)"
        )

        resp = await brain._openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=300,
            temperature=0.7,
            response_format={"type": "json_object"},
        )

        return json.loads(resp.choices[0].message.content)

    except Exception as e:
        logger.warning(f"Forecast generation failed: {e}")
        return default_forecast


# ---------------------------------------------------------------------------
# Retention status + weekly summary endpoints
# ---------------------------------------------------------------------------

@router.get("/retention-status")
async def get_retention_status(user_id: str = Depends(get_current_user_id)):
    """
    Returns the user's engagement/retention status.
    Tells the mobile app whether to show a check-in banner or weekly summary.
    """
    from datetime import timedelta
    row = await fetchrow(
        "SELECT last_active, last_daily_checkin_at, last_weekly_summary_at FROM users WHERE id = $1",
        UUID(user_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    now = datetime.now(timezone.utc)
    last_active = row["last_active"]
    days_since = 0
    if last_active:
        last_active_utc = last_active.replace(tzinfo=timezone.utc) if last_active.tzinfo is None else last_active
        days_since = max(0, (now - last_active_utc).days)

    needs_checkin = (
        row["last_daily_checkin_at"] is None
        or (now - (row["last_daily_checkin_at"].replace(tzinfo=timezone.utc) if row["last_daily_checkin_at"].tzinfo is None else row["last_daily_checkin_at"])).total_seconds() > 86400
    )

    # Generate a contextual check-in message if needed
    checkin_message = None
    if needs_checkin:
        from core.database import fetch as db_fetch
        import json as _j
        profile_row = await fetchrow("SELECT profile FROM users WHERE id = $1", UUID(user_id))
        profile = profile_row["profile"] if profile_row else {}
        if isinstance(profile, str):
            try:
                profile = _j.loads(profile)
            except Exception:
                profile = {}
        goals = await db_fetch(
            "SELECT title, progress, status FROM goals WHERE user_id = $1 AND status = 'active' LIMIT 3",
            UUID(user_id),
        )
        goals_list = [{"title": g["title"], "progress": g["progress"] or 0.0, "status": g["status"]} for g in goals]
        from core.notification_worker import _generate_daily_checkin_message
        checkin_message = await _generate_daily_checkin_message(UUID(user_id), profile, goals_list)

    # Check if a weekly summary was generated recently
    weekly_summary_available = False
    if row["last_weekly_summary_at"]:
        last_ws = row["last_weekly_summary_at"]
        if last_ws.tzinfo is None:
            last_ws = last_ws.replace(tzinfo=timezone.utc)
        weekly_summary_available = (now - last_ws).total_seconds() < 7 * 86400

    return {
        "days_since_last_session": days_since,
        "needs_checkin": needs_checkin,
        "checkin_message": checkin_message,
        "weekly_summary_available": weekly_summary_available,
    }


@router.get("/weekly-summary")
async def get_weekly_summary(user_id: str = Depends(get_current_user_id)):
    """
    Returns the most recent weekly summary for this user, if available.
    """
    from core.database import fetchrow as db_fetchrow
    row = await db_fetchrow(
        """
        SELECT week_start, week_end, screens_seen, goals_progressed,
               top_interests, ora_narrative, fulfilment_change, created_at
        FROM weekly_summaries
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT 1
        """,
        UUID(user_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="No weekly summary available yet")

    import json as _j
    top_interests = row["top_interests"]
    if isinstance(top_interests, str):
        try:
            top_interests = _j.loads(top_interests)
        except Exception:
            top_interests = []

    return {
        "week_start": str(row["week_start"]),
        "week_end": str(row["week_end"]),
        "screens_seen": row["screens_seen"] or 0,
        "goals_progressed": row["goals_progressed"] or 0,
        "top_interests": top_interests or [],
        "ora_narrative": row["ora_narrative"] or "",
        "fulfilment_change": row["fulfilment_change"] or 0.0,
    }


# ---------------------------------------------------------------------------
# PATCH /api/users/privacy  (Integration G)
# ---------------------------------------------------------------------------

class PrivacyUpdateRequest(BaseModel):
    privacy_level: str  # "standard" | "sensitive" | "minimal"


@router.patch("/privacy")
async def update_privacy_level(
    payload: PrivacyUpdateRequest,
    user_id: str = Depends(get_current_user_id),
):
    """
    Update the user's privacy level for Ora's memory system.

    - standard: Ora uses all context (default)
    - sensitive: Ora uses goals + ratings only, never repeats back sensitive content
    - minimal: Ora uses no personal context, fresh each conversation
    """
    allowed = {"standard", "sensitive", "minimal"}
    if payload.privacy_level not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"privacy_level must be one of: {sorted(allowed)}",
        )

    row = await fetchrow("SELECT profile FROM users WHERE id = $1", UUID(user_id))
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    raw_profile = row["profile"] or {}
    profile = json.loads(raw_profile) if isinstance(raw_profile, str) else dict(raw_profile)
    profile["privacy_level"] = payload.privacy_level

    await execute(
        "UPDATE users SET profile = $1::jsonb WHERE id = $2",
        json.dumps(profile),
        UUID(user_id),
    )

    # Invalidate user model cache
    try:
        from core.redis_client import redis_delete
        await redis_delete(f"user_model:{user_id}")
        await redis_delete(f"user:{user_id}:context")
    except Exception:
        pass

    logger.info(f"User {user_id[:8]}: privacy_level set to '{payload.privacy_level}'")
    return {"ok": True, "privacy_level": payload.privacy_level}
