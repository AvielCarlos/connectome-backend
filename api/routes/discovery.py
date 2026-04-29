"""
Discovery API Routes
Handles user answers to Ora's discovery interview questions.
Answers update the user profile for better personalisation.
"""

import logging
import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.middleware import get_current_user_id
from core.database import execute, fetchrow, fetchval
from uuid import UUID

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/discovery", tags=["discovery"])


class DiscoveryAnswerBody(BaseModel):
    question_id: str
    answer: Any                 # str | int | float | list
    profile_field: str


ONBOARDING_QUESTIONS = [
    {
        "question_id": "onboarding_name",
        "field_name": "name",
        "question": "What's your name? I'd love to know who I'm talking to 😊",
    },
    {
        "question_id": "onboarding_growth_areas",
        "field_name": "growth_areas",
        "question": "What are the 2-3 areas of your life you most want to grow or improve right now? For example: health, career, relationships, creativity, finances, spirituality, adventure.",
    },
    {
        "question_id": "onboarding_one_year_vision",
        "field_name": "one_year_vision",
        "question": "What does a great life look like for you in 1 year? Paint me a picture.",
    },
    {
        "question_id": "onboarding_biggest_challenge",
        "field_name": "biggest_challenge",
        "question": "What's your biggest challenge or obstacle right now?",
    },
    {
        "question_id": "onboarding_weekly_commitment",
        "field_name": "weekly_commitment",
        "question": "How much time per week can you realistically commit to working on yourself — 30 mins? 2 hours? More?",
    },
    {
        "question_id": "onboarding_constraints",
        "field_name": "constraints",
        "question": "Any constraints I should know about — location, budget, health, family situation?",
    },
]


class OnboardingRequest(BaseModel):
    conversation: list[dict] = Field(default_factory=list)  # [{role: "user"|"ora", content: "..."}]


class OnboardingResponse(BaseModel):
    message: str
    is_complete: bool
    question_index: int
    total_questions: int = 6


def _get_openai():
    """Lazy OpenAI client — returns None if no key/package configured."""
    from core.config import settings
    if not settings.has_openai:
        return None
    try:
        from openai import AsyncOpenAI
        return AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    except Exception as e:
        logger.warning(f"Could not init OpenAI for onboarding: {e}")
        return None


def _user_turns(conversation: list[dict]) -> list[str]:
    return [
        str(m.get("content", "")).strip()
        for m in conversation
        if m.get("role") == "user" and str(m.get("content", "")).strip()
    ]


async def _ora_onboarding_message(
    conversation: list[dict],
    next_question: Optional[str],
    is_complete: bool,
) -> str:
    if is_complete:
        return "I've got what I need to start building your path. Let's go! ◈"

    fallback = next_question or ONBOARDING_QUESTIONS[0]["question"]
    user_turns = _user_turns(conversation)
    if not user_turns:
        return fallback

    client = _get_openai()
    if not client:
        return f"Thank you — that helps me understand you better. {fallback}"

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.7,
            max_tokens=120,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are Ora, a warm, concise AI guide for human flourishing. "
                        "Reply with exactly one short acknowledgement sentence, then ask the provided next intake question. "
                        "Keep it natural, grounded, and under 55 words. Do not add extra questions."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "conversation": conversation[-8:],
                            "next_question": fallback,
                        }
                    ),
                },
            ],
        )
        message = response.choices[0].message.content.strip()
        return message or f"Thank you — that helps me understand you better. {fallback}"
    except Exception as e:
        logger.warning(f"Ora onboarding OpenAI response failed: {e}")
        return f"Thank you — that helps me understand you better. {fallback}"


async def _store_onboarding_answers(user_id: str, answers: list[str]) -> None:
    uid = UUID(user_id)
    await execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_completed BOOLEAN DEFAULT FALSE")
    await execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_completed_at TIMESTAMP")
    await execute(
        """
        CREATE TABLE IF NOT EXISTS discovery_profile (
            user_id UUID REFERENCES users(id) ON DELETE CASCADE,
            field_name TEXT NOT NULL,
            field_value TEXT,
            question_id TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (user_id, field_name)
        )
        """
    )

    answer_map = {}
    for idx, answer in enumerate(answers[: len(ONBOARDING_QUESTIONS)]):
        q = ONBOARDING_QUESTIONS[idx]
        answer_map[q["field_name"]] = answer
        await execute(
            """
            INSERT INTO discovery_profile (user_id, field_name, field_value, question_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id, field_name)
            DO UPDATE SET field_value = EXCLUDED.field_value,
                          question_id = EXCLUDED.question_id,
                          updated_at = NOW()
            """,
            uid,
            q["field_name"],
            answer,
            q["question_id"],
        )

    row = await fetchrow("SELECT profile FROM users WHERE id = $1", uid)
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    profile = {}
    raw_profile = row["profile"]
    if raw_profile:
        profile = json.loads(raw_profile) if isinstance(raw_profile, str) else dict(raw_profile)
    profile["onboarding_intake"] = answer_map
    if answer_map.get("name"):
        profile["display_name"] = answer_map["name"]

    await execute(
        """
        UPDATE users
        SET profile = $1,
            display_name = COALESCE(NULLIF($2, ''), display_name),
            onboarding_completed = TRUE,
            onboarding_completed_at = NOW(),
            last_active = NOW()
        WHERE id = $3
        """,
        json.dumps(profile),
        answer_map.get("name", ""),
        uid,
    )

    try:
        client = _get_openai()
        if client:
            intake_text = "\n".join(
                f"{ONBOARDING_QUESTIONS[i]['field_name']}: {answer}"
                for i, answer in enumerate(answers[: len(ONBOARDING_QUESTIONS)])
            )
            embedding_response = await client.embeddings.create(
                model="text-embedding-3-small",
                input=intake_text[:8000],
            )
            embedding = embedding_response.data[0].embedding
            embedding_str = "[" + ",".join(f"{v:.6f}" for v in embedding) + "]"
            await execute(
                "UPDATE users SET embedding = $1::vector WHERE id = $2",
                embedding_str,
                uid,
            )
    except Exception as e:
        logger.warning(f"Could not seed user feed embedding after onboarding: {e}")

    try:
        from core.redis_client import redis_delete
        await redis_delete(f"user_model:{user_id}")
    except Exception as e:
        logger.warning(f"Could not invalidate user model cache after onboarding: {e}")

    try:
        from ora.agents.ioo_graph_agent import get_graph_agent
        await get_graph_agent().build_user_ioo_vector(user_id)
    except Exception as e:
        logger.warning(f"Could not update IOO vector after onboarding: {e}")


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


@router.post("/onboarding", response_model=OnboardingResponse)
async def onboarding_intake(
    body: OnboardingRequest,
    user_id: str = Depends(get_current_user_id),
):
    """
    Short Ora intake interview. 6 questions, then done.
    Answers are stored in discovery_profile and used to seed IOO recommendations.
    """
    user_turns = _user_turns(body.conversation)
    answered_count = min(len(user_turns), len(ONBOARDING_QUESTIONS))

    row = await fetchrow("SELECT id, onboarding_completed FROM users WHERE id = $1", UUID(user_id))
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    if bool(row["onboarding_completed"]):
        return OnboardingResponse(
            message="I've got what I need to start building your path. Let's go! ◈",
            is_complete=True,
            question_index=len(ONBOARDING_QUESTIONS),
        )

    if answered_count >= len(ONBOARDING_QUESTIONS):
        await _store_onboarding_answers(user_id, user_turns)
        return OnboardingResponse(
            message="I've got what I need to start building your path. Let's go! ◈",
            is_complete=True,
            question_index=len(ONBOARDING_QUESTIONS),
        )

    next_question = ONBOARDING_QUESTIONS[answered_count]["question"]
    message = await _ora_onboarding_message(
        body.conversation,
        next_question=next_question,
        is_complete=False,
    )
    return OnboardingResponse(
        message=message,
        is_complete=False,
        question_index=answered_count,
    )


@router.get("/onboarding/status")
async def onboarding_status(user_id: str = Depends(get_current_user_id)):
    row = await fetchrow("SELECT onboarding_completed FROM users WHERE id = $1", UUID(user_id))
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    completed = bool(row["onboarding_completed"])
    try:
        count = await fetchval("SELECT COUNT(*) FROM discovery_profile WHERE user_id = $1", UUID(user_id))
    except Exception as e:
        logger.warning(f"Could not count discovery_profile rows for onboarding status: {e}")
        count = 0
    return {"completed": completed, "question_index": int(count or 0)}
