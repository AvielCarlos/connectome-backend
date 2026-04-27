"""
Ground Truth Routes
Safeguard 3: Direct user prompts to anchor Ora's exit classifications.

POST /api/ground-truth/prompt  — Ask Ora if a clarifying question should be shown
POST /api/ground-truth/answer  — Store user's answer and update classification
GET  /api/ground-truth/calibration — Calibration stats for Ora's exit predictions
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.middleware import get_current_user_id
from core.database import fetch, fetchrow, execute, fetchval
from ora.user_model import update_user_embedding

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ground-truth", tags=["ground-truth"])

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

ANSWER_OPTIONS = ["Too long", "Not interesting", "Wrong topic", "Just browsing", "Other"]

# Map human-readable option → stored answer key → exit_classification category
ANSWER_OPTION_MAP = {
    "Too long": "too_long",
    "Not interesting": "not_interesting",
    "Wrong topic": "wrong_topic",
    "Just browsing": "just_browsing",
    "Other": "other",
}

ANSWER_TO_CATEGORY = {
    "too_long": "attention_lost",
    "not_interesting": "content_mismatch",
    "wrong_topic": "content_mismatch",
    "just_browsing": "unknown",
    "other": "unknown",
}


class GroundTruthPromptRequest(BaseModel):
    user_id: str


class GroundTruthPromptResponse(BaseModel):
    should_ask: bool
    interaction_id: Optional[str] = None
    exit_classification_id: Optional[str] = None
    question: Optional[str] = None
    options: Optional[list] = None


class GroundTruthAnswerRequest(BaseModel):
    user_id: str
    interaction_id: str
    exit_classification_id: str
    answer: str  # raw answer key: 'too_long' | 'not_interesting' | 'wrong_topic' | 'just_browsing' | 'other'


class GroundTruthAnswerResponse(BaseModel):
    ok: bool


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/prompt", response_model=GroundTruthPromptResponse)
async def ground_truth_prompt(body: GroundTruthPromptRequest):
    """
    Ask Ora whether a clarifying question should be shown to the user.

    Finds the most recent exit_classification where:
      - confidence < 0.7 OR consistency_flagged = true
      - No ground_truth_label yet exists for this interaction
      - The interaction is fresh (< 30 minutes old)

    Returns should_ask=false if nothing qualifies.
    """
    try:
        user_uuid = UUID(body.user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id")

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)

    # Find most recent qualifying exit classification
    row = await fetchrow(
        """
        SELECT ec.id AS ec_id,
               ec.interaction_id,
               ec.confidence,
               ec.consistency_flagged
        FROM exit_classifications ec
        JOIN interactions i ON i.id = ec.interaction_id
        WHERE ec.user_id = $1
          AND (ec.confidence < 0.7 OR ec.consistency_flagged = TRUE)
          AND i.created_at >= $2
          AND NOT EXISTS (
            SELECT 1 FROM ground_truth_labels gt
            WHERE gt.exit_classification_id = ec.id
          )
        ORDER BY i.created_at DESC
        LIMIT 1
        """,
        user_uuid,
        cutoff,
    )

    if not row:
        return GroundTruthPromptResponse(should_ask=False)

    return GroundTruthPromptResponse(
        should_ask=True,
        interaction_id=str(row["interaction_id"]),
        exit_classification_id=str(row["ec_id"]),
        question="Why did you leave that screen?",
        options=ANSWER_OPTIONS,
    )


@router.post("/answer", response_model=GroundTruthAnswerResponse)
async def ground_truth_answer(body: GroundTruthAnswerRequest):
    """
    Record a user's ground-truth answer and update the corresponding
    exit_classification with high-confidence confirmed values.
    """
    try:
        user_uuid = UUID(body.user_id)
        interaction_uuid = UUID(body.interaction_id)
        ec_uuid = UUID(body.exit_classification_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID in request")

    # Normalize answer
    answer_key = body.answer.lower().replace(" ", "_")
    # Accept both human-readable and key forms
    if body.answer in ANSWER_OPTION_MAP:
        answer_key = ANSWER_OPTION_MAP[body.answer]

    valid_keys = set(ANSWER_OPTION_MAP.values())
    if answer_key not in valid_keys:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid answer. Valid options: {list(valid_keys)}",
        )

    # Store ground truth label
    await execute(
        """
        INSERT INTO ground_truth_labels
            (user_id, interaction_id, exit_classification_id, user_answer)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT DO NOTHING
        """,
        user_uuid,
        interaction_uuid,
        ec_uuid,
        answer_key,
    )

    # Update the exit_classification with confirmed signal
    confirmed_category = ANSWER_TO_CATEGORY.get(answer_key, "unknown")
    await execute(
        """
        UPDATE exit_classifications
        SET confidence = 0.95,
            category = $1,
            reason = $2
        WHERE id = $3
        """,
        confirmed_category,
        f"User confirmed: {answer_key}",
        ec_uuid,
    )

    # Update user embedding with confirmed feedback signal
    # Map confirmed category to a rating signal
    CATEGORY_RATING = {
        "content_mismatch": 1,
        "attention_lost": 2,
        "unknown": None,
    }
    feedback_rating = CATEGORY_RATING.get(confirmed_category)
    if feedback_rating:
        # Look up agent_type from the associated screen spec
        ec_row = await fetchrow(
            """
            SELECT s.agent_type
            FROM exit_classifications ec
            JOIN screen_specs s ON s.id = ec.screen_spec_id
            WHERE ec.id = $1
            """,
            ec_uuid,
        )
        agent_type = ec_row["agent_type"] if ec_row else "unknown"
        try:
            await update_user_embedding(body.user_id, feedback_rating, agent_type)
        except Exception as e:
            logger.warning(f"Ground truth embedding update failed: {e}")

    logger.info(
        f"Ground truth recorded: user={body.user_id[:8]} "
        f"answer={answer_key} → category={confirmed_category}"
    )
    return GroundTruthAnswerResponse(ok=True)


@router.get("/calibration")
async def get_calibration(user_id: Optional[str] = None):
    """
    Return calibration stats comparing Ora's predictions against ground truth.
    Optionally scoped to a single user_id query param.
    """
    from ora.brain import get_brain
    brain = get_brain()
    stats = await brain.feedback_analyst.get_calibration_stats(user_id=user_id)
    return stats
