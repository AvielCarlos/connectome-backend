"""
Feedback API Routes
Real-time feedback processing — the learning loop.
Includes experiment signal collection and implicit behaviour signals.
"""

import logging
import json
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.models import FeedbackSubmit, FeedbackResponse
from api.middleware import get_current_user_id
from ora.brain import get_brain
from core.database import execute, fetchrow
from uuid import UUID

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/feedback", tags=["feedback"])

GLOBAL_FEEDBACK_CP = 10
GLOBAL_FEEDBACK_CATEGORIES = {"Bug", "Confusing", "Idea", "Design", "Praise", "Other"}


async def _ensure_global_feedback_schema() -> None:
    """Idempotent schema hardening for the lightweight global feedback loop."""
    await execute(
        """
        CREATE TABLE IF NOT EXISTS app_feedback (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID REFERENCES users(id) ON DELETE CASCADE,
            category TEXT NOT NULL DEFAULT 'Other',
            message TEXT NOT NULL,
            route TEXT,
            screenshot_data_url TEXT,
            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    await execute("ALTER TABLE app_feedback ADD COLUMN IF NOT EXISTS route TEXT")
    await execute("ALTER TABLE app_feedback ADD COLUMN IF NOT EXISTS screenshot_data_url TEXT")
    await execute("ALTER TABLE app_feedback ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb")
    await execute("CREATE INDEX IF NOT EXISTS idx_app_feedback_user_created ON app_feedback(user_id, created_at DESC)")

    await execute("ALTER TABLE contributors ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id)")
    await execute("CREATE INDEX IF NOT EXISTS idx_contributors_user_id ON contributors(user_id)")
    await execute("ALTER TABLE contributions ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id)")
    await execute("ALTER TABLE contributions ADD COLUMN IF NOT EXISTS external_link TEXT")
    await execute("ALTER TABLE contributions ADD COLUMN IF NOT EXISTS evidence_text TEXT")
    await execute("ALTER TABLE contributions ADD COLUMN IF NOT EXISTS attachment_urls JSONB DEFAULT '[]'")
    await execute("ALTER TABLE contributions ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'manual'")
    await execute("ALTER TABLE contributions ADD COLUMN IF NOT EXISTS source_id TEXT")


async def _get_or_create_feedback_contributor(user_uuid: UUID) -> Any:
    contributor = await fetchrow("SELECT id FROM contributors WHERE user_id = $1", user_uuid)
    if contributor:
        return contributor

    user_row = await fetchrow("SELECT email, display_name, profile FROM users WHERE id = $1", user_uuid)
    user_data = dict(user_row) if user_row else {}
    email = user_data.get("email") or ""
    profile = user_data.get("profile") or {}
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except Exception:
            profile = {}
    display = (
        user_data.get("display_name")
        or (profile.get("display_name") if isinstance(profile, dict) else None)
        or (email.split("@")[0] if email else f"user_{str(user_uuid)[:8]}")
    )

    return await fetchrow(
        """
        INSERT INTO contributors (github_username, display_name, email, tier, user_id)
        VALUES ($1, $2, $3, 'contributor', $4)
        ON CONFLICT (github_username) DO UPDATE SET
            user_id = EXCLUDED.user_id,
            display_name = COALESCE(contributors.display_name, EXCLUDED.display_name),
            email = COALESCE(contributors.email, EXCLUDED.email)
        RETURNING id
        """,
        f"user_{str(user_uuid)[:8]}",
        display,
        email,
        user_uuid,
    )


async def _handle_global_feedback(body: FeedbackSubmit, user_id: str) -> FeedbackResponse:
    message = (body.message or "").strip()
    if len(message) < 3:
        raise HTTPException(status_code=422, detail="message is required")

    category = body.category or "Other"
    if category not in GLOBAL_FEEDBACK_CATEGORIES:
        raise HTTPException(status_code=422, detail="invalid feedback category")

    user_uuid = UUID(user_id)
    await _ensure_global_feedback_schema()

    metadata = body.metadata if isinstance(body.metadata, dict) else {}
    feedback = await fetchrow(
        """
        INSERT INTO app_feedback (user_id, category, message, route, screenshot_data_url, metadata)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        RETURNING id
        """,
        user_uuid,
        category,
        message,
        body.route,
        body.screenshot_data_url,
        json.dumps(metadata),
    )
    feedback_id = feedback["id"] if feedback else None

    contributor = await _get_or_create_feedback_contributor(user_uuid)
    contribution = None
    try:
        contribution = await fetchrow(
            """
            INSERT INTO contributions (
                contributor_id, user_id, contribution_type, title, description,
                status, base_cp, multiplier, final_cp, evidence_text, source, source_id, impact_data
            )
            VALUES ($1, $2, 'feedback', $3, $4, 'accepted', $5, 1.0, $5, $6, 'feedback', $7, $8::jsonb)
            RETURNING id
            """,
            contributor["id"] if contributor else None,
            user_uuid,
            f"{category} feedback on {body.route or 'Ora'}",
            message,
            GLOBAL_FEEDBACK_CP,
            message,
            str(feedback_id) if feedback_id else None,
            json.dumps({"route": body.route, "category": category, "feedback_id": str(feedback_id) if feedback_id else None}),
        )
    except Exception as err:
        logger.warning(f"Feedback contribution write failed (non-fatal): {err}")

    contribution_id = str(contribution["id"]) if contribution else (str(feedback_id) if feedback_id else None)

    # Award CP. This is non-critical after the feedback record itself exists.
    try:
        await execute(
            """
            INSERT INTO user_cp_balance (user_id, cp_balance, total_cp_earned, last_updated)
            VALUES ($1, $2, $2, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                cp_balance = user_cp_balance.cp_balance + $2,
                total_cp_earned = user_cp_balance.total_cp_earned + $2,
                last_updated = NOW()
            """,
            user_uuid, GLOBAL_FEEDBACK_CP,
        )
        await execute(
            "INSERT INTO cp_transactions (user_id, amount, reason, reference_id, created_at) VALUES ($1, $2, $3, $4, NOW())",
            user_uuid, GLOBAL_FEEDBACK_CP, "feedback_submit", contribution_id,
        )
        if contributor and contribution:
            await execute(
                "INSERT INTO cp_ledger (contributor_id, contribution_id, cp_amount, reason) VALUES ($1, $2, $3, $4)",
                contributor["id"], contribution["id"], GLOBAL_FEEDBACK_CP, "global_feedback_submit",
            )
            await execute(
                "UPDATE contributors SET total_cp = COALESCE(total_cp, 0) + $1 WHERE id = $2",
                GLOBAL_FEEDBACK_CP, contributor["id"],
            )
    except Exception as err:
        logger.warning(f"Feedback CP award failed (non-fatal): {err}")

    cp_row = await fetchrow("SELECT cp_balance, total_cp_earned FROM user_cp_balance WHERE user_id = $1", user_uuid)
    return FeedbackResponse(
        ok=True,
        fulfilment_delta=0.0,
        message="Feedback submitted +10 CP",
        cp_earned=GLOBAL_FEEDBACK_CP,
        cp_balance=int(cp_row["cp_balance"] or 0) if cp_row else None,
        total_dao_cp=int(cp_row["total_cp_earned"] or 0) if cp_row else None,
        contribution_id=contribution_id,
    )


async def _record_ioo_outcome_if_applicable(
    user_id: str,
    screen_spec_id: str,
    rating: Optional[int],
) -> None:
    """
    If the rated card was sourced from the IOO graph, record its outcome so
    cross-user path weights are updated.
    """
    if not rating or not screen_spec_id:
        return
    try:
        import json as _json
        row = await fetchrow(
            "SELECT spec FROM screen_specs WHERE id = $1",
            UUID(screen_spec_id) if len(screen_spec_id) == 36 else None,
        )
        if not row:
            return
        spec = row["spec"]
        if isinstance(spec, str):
            spec = _json.loads(spec)
        meta = spec.get("metadata", {}) if isinstance(spec, dict) else {}
        if meta.get("source") != "ioo_graph" or not meta.get("node_id"):
            return
        node_id = meta["node_id"]
        from ora.agents.ioo_graph_agent import get_graph_agent as _get_ioo
        await _get_ioo().record_node_outcome(
            user_id=str(user_id),
            node_id=str(node_id),
            success=(rating >= 4),
            hours_taken=0,
        )
        logger.info(
            f"IOO outcome recorded: user={user_id[:8]} node={str(node_id)[:8]} "
            f"success={rating >= 4} rating={rating}"
        )
    except Exception as _err:
        logger.warning(f"IOO outcome recording skipped: {_err}")


@router.post("", response_model=FeedbackResponse, response_model_exclude={"xp_earned"})
@router.post("/", response_model=FeedbackResponse, response_model_exclude={"xp_earned"})
async def submit_feedback(
    body: FeedbackSubmit,
    user_id: str = Depends(get_current_user_id),
):
    """
    Submit feedback.
    - Global app feedback (message/category/route) stores screenshot context and awards CP.
    - Legacy card feedback (screen_spec_id/rating) triggers the learning loop.
    """
    if body.message is not None:
        return await _handle_global_feedback(body, user_id)

    if not body.screen_spec_id:
        raise HTTPException(status_code=422, detail="screen_spec_id or message is required")

    brain = get_brain()

    try:
        insight = await brain.process_feedback(
            user_id=user_id,
            screen_spec_id=body.screen_spec_id,
            rating=body.rating,
            time_on_screen_ms=body.time_on_screen_ms,
            exit_point=body.exit_point,
            completed=body.completed,
        )
    except Exception as e:
        logger.error(f"Feedback processing error for user {user_id[:8]}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to process feedback")

    # Update IOO graph weights if this was a graph-sourced card.
    # TODO(IOO): replace the MVP 1-5 rating proxy with explicit feed responses:
    # - not_interested -> down-rank/refine similar nodes in the user vector
    # - do_later       -> save/schedule/resurface as a live opportunity
    # - do_now         -> trigger IOO Execution Protocol and record outcome
    await _record_ioo_outcome_if_applicable(user_id, body.screen_spec_id, body.rating)

    # Build a human-readable message based on the insight
    signal = insight.get("signal_type", "neutral")
    delta = insight.get("fulfilment_delta", 0.0)

    if signal == "positive":
        msg = "Great! Ora will show you more like this."
    elif signal == "negative":
        msg = "Noted. Ora will adjust your feed."
    else:
        msg = "Thanks for the feedback."

    return FeedbackResponse(
        ok=True,
        fulfilment_delta=delta,
        message=msg,
    )


@router.get("/trend")
async def get_user_trend(user_id: str = Depends(get_current_user_id)):
    """Get the current user's trend analysis from the feedback analyst."""
    brain = get_brain()
    trend = await brain.feedback_analyst.get_user_trend(user_id)
    return trend


# ---------------------------------------------------------------------------
# Implicit signal batch endpoint (TikTok-style feed)
# ---------------------------------------------------------------------------

# Maps behavioural signal types to a 1–5 rating for the learning loop
_IMPLICIT_RATING: dict = {
    "skip_fast":  1,
    "skip_slow":  2,
    "tap":        3,
    "action_tap": 4,
    "dwell":      4,   # > 8s
    "long_press": 3,
    "swipe_back": 5,   # strong positive
}


class ImplicitSignalItem(BaseModel):
    screen_spec_id: str
    signal_type: str
    dwell_ms: Optional[int] = None
    metadata: Optional[Any] = None


class ImplicitSignalBatch(BaseModel):
    signals: List[ImplicitSignalItem]


@router.post("/implicit")
async def submit_implicit_signals(
    body: ImplicitSignalBatch,
    user_id: str = Depends(get_current_user_id),
):
    """
    Process a batch of implicit behavioural signals from the TikTok-style feed.
    Each signal is converted to a numeric rating and fed into the learning loop.
    
    Signal type → rating mapping:
      skip_fast  → 1  (< 2s dwell)
      skip_slow  → 2  (2–8s dwell)
      tap        → 3
      dwell      → 4  (> 8s, implied engagement)
      action_tap → 4
      swipe_back → 5  (strong positive)
      long_press → 3
    """
    brain = get_brain()
    processed = 0

    for signal in body.signals:
        rating = _IMPLICIT_RATING.get(signal.signal_type)
        if not rating:
            # Unknown signal type — skip gracefully
            continue

        # Long dwell (> 15s) bumps to 4 regardless of base signal
        if signal.dwell_ms and signal.dwell_ms > 15_000 and rating < 4:
            rating = 4

        try:
            # Reuse the existing feedback learning loop
            await brain.process_feedback(
                user_id=user_id,
                screen_spec_id=signal.screen_spec_id,
                rating=rating,
                time_on_screen_ms=signal.dwell_ms,
                exit_point=f"implicit_{signal.signal_type}",
                completed=signal.signal_type in ("dwell", "swipe_back", "action_tap"),
            )

            # Update IOO graph weights for graph-sourced implicit signals
            await _record_ioo_outcome_if_applicable(user_id, signal.screen_spec_id, rating)

            # Also store the raw signal type on the interaction row
            try:
                await execute(
                    """
                    UPDATE interactions
                    SET implicit_signal = $1
                    WHERE user_id = $2
                      AND screen_spec_id = $3
                      AND id = (
                        SELECT id FROM interactions
                        WHERE user_id = $2 AND screen_spec_id = $3
                        ORDER BY created_at DESC
                        LIMIT 1
                      )
                    """,
                    signal.signal_type,
                    UUID(user_id),
                    UUID(signal.screen_spec_id),
                )
            except Exception:
                pass  # Non-critical — the rating was already recorded above

            processed += 1
        except Exception as e:
            logger.warning(f"Implicit signal processing error: {e}")
            # Continue processing remaining signals

    return {"ok": True, "processed": processed, "total": len(body.signals)}


# ---------------------------------------------------------------------------
# Experiment signal collection
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ActionTool feedback endpoint
# ---------------------------------------------------------------------------

class ActionFeedbackBody(BaseModel):
    tool_name: str
    args_summary: Optional[str] = ""
    success: bool = True


@router.post("/action")
async def submit_action_feedback(
    body: ActionFeedbackBody,
    user_id: str = Depends(get_current_user_id),
):
    """
    Record that an Ora action tool was successfully executed.
    Stored as an interaction with a special exit_point so the
    feedback analyst can learn which tools drive real-world behaviour.
    """
    exit_point = f"ora_action:{body.tool_name}"

    try:
        # Insert a lightweight interaction row (no screen_spec required)
        # Use the most recent screen spec for this user as a reference
        await execute(
            """
            INSERT INTO interactions (user_id, screen_spec_id, exit_point, completed)
            SELECT $1, id, $2, true FROM screen_specs
            ORDER BY created_at DESC LIMIT 1
            """,
            UUID(user_id),
            exit_point,
        )
    except Exception as e:
        logger.warning(f"Action feedback insert failed: {e}")

    logger.info(
        f"Action feedback: user={user_id[:8]} tool={body.tool_name} "
        f"args={body.args_summary} success={body.success}"
    )
    return {"ok": True}


class ExperimentSignalBody(BaseModel):
    user_id: Optional[str] = None   # falls back to auth user_id
    screen_spec_id: Optional[str] = None
    mechanism_type: str
    raw_signal: Any


@router.post("/experiment/{experiment_id}")
async def submit_experiment_signal(
    experiment_id: str,
    body: ExperimentSignalBody,
    auth_user_id: str = Depends(get_current_user_id),
):
    """
    Submit a signal for an active feedback experiment.
    Normalizes any signal type to 0.0–1.0 and stores it.
    Returns: {ok: true, normalized_score: float}
    """
    brain = get_brain()
    uid = body.user_id or auth_user_id

    try:
        normalized = await brain.feedback_experimenter.collect_experiment_signal(
            experiment_id=experiment_id,
            user_id=uid,
            mechanism_type=body.mechanism_type,
            raw_signal=body.raw_signal,
            screen_spec_id=body.screen_spec_id,
        )
    except Exception as e:
        logger.error(f"Experiment signal error [{experiment_id[:8]}]: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to record experiment signal")

    return {"ok": True, "normalized_score": normalized}
