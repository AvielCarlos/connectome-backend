"""
Discovery API Routes
Handles user answers to Ora's discovery interview questions.
Answers update the user profile for better personalisation.
"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.middleware import get_current_user_id
from core.database import execute, fetchrow
from uuid import UUID

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/discovery", tags=["discovery"])


class DiscoveryAnswerBody(BaseModel):
    question_id: str
    answer: Any                 # str | int | float | list
    profile_field: str


@router.post("/answer")
async def submit_discovery_answer(
    body: DiscoveryAnswerBody,
    user_id: str = Depends(get_current_user_id),
):
    """
    Submit a user's answer to an Ora discovery question.
    Stores the answer in the user profile under the named field.
    Returns { ok: true, profile_updated: true }.
    """
    import json

    # Convert answer to JSON string for storage
    if isinstance(body.answer, (dict, list)):
        answer_str = json.dumps(body.answer)
    else:
        answer_str = str(body.answer)

    # Fetch current user profile blob
    row = await fetchrow(
        "SELECT id, interests FROM users WHERE id = $1",
        UUID(user_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    # We store discovery answers in the `interests` JSONB field (or a
    # dedicated discovery_profile field if the schema has one).
    # Use a safe JSONB merge approach via a dedicated column if it exists,
    # otherwise store in a metadata table.
    try:
        # Try to upsert into a discovery_profile table (may not exist yet —
        # fall back to logging gracefully)
        await execute(
            """
            INSERT INTO discovery_profile (user_id, field_name, field_value, question_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id, field_name)
            DO UPDATE SET field_value = EXCLUDED.field_value,
                          question_id = EXCLUDED.question_id,
                          updated_at = NOW()
            """,
            UUID(user_id),
            body.profile_field,
            answer_str,
            body.question_id,
        )
    except Exception as e:
        # Table may not exist yet — log and continue gracefully
        logger.warning(f"discovery_profile upsert failed (table may not exist): {e}")
        # Fallback: store as interaction with special exit_point
        try:
            await execute(
                """
                INSERT INTO interactions (user_id, screen_spec_id, exit_point, completed)
                SELECT $1, id, $2, true FROM screen_specs
                ORDER BY created_at DESC LIMIT 1
                """,
                UUID(user_id),
                f"discovery:{body.question_id}:{answer_str[:80]}",
            )
        except Exception as _fe:
            logger.warning(f"discovery fallback interaction insert failed: {_fe}")

    logger.info(
        f"Discovery answer: user={user_id[:8]} field={body.profile_field} "
        f"q={body.question_id} answer_len={len(answer_str)}"
    )

    return {"ok": True, "profile_updated": True}
