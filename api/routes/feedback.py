"""
Feedback API Routes
Real-time feedback processing — the learning loop.
Includes experiment signal collection and implicit behaviour signals.
"""

import logging
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


@router.post("/", response_model=FeedbackResponse)
async def submit_feedback(
    body: FeedbackSubmit,
    user_id: str = Depends(get_current_user_id),
):
    """
    Submit feedback for a screen.
    Triggers the full learning loop: embedding update, rating update, A/B tracking.
    Also updates IOO graph weights when the card was graph-sourced.
    """
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

    # Update IOO graph weights if this was a graph-sourced card
    await _record_ioo_outcome_if_applicable(user_id, body.screen_spec_id, body.rating)

    # Award XP for giving feedback — feedback fuels the learning loop
    try:
        await execute(
            "INSERT INTO xp_log (user_id, amount, reason, ref_id) VALUES ($1, $2, $3, $4)",
            UUID(user_id), 10, "feedback_submit",
            UUID(body.screen_spec_id) if body.screen_spec_id and len(body.screen_spec_id) == 36 else None,
        )
    except Exception:
        pass  # Non-critical — don't block feedback response

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
