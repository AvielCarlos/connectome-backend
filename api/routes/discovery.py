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
    {"question_id": "onboarding_name", "field_name": "name", "question": "What's your name? I'd love to know who I'm talking to 😊"},
    {
        "question_id": "domain_selection",
        "field_name": "domain_selection",
        "question": (
            "Okay [name], I see your life as having 3 dimensions — and fulfilment means tending to all of them:\n\n"
            "🌱 iVive — Maintenance and growth of self: physical, mental, spiritual, creative, financial, skills, habits\n"
            "🌊 Eviva — Contributing to the collective and receiving reward: work, volunteering, DAO, service, building for others\n"
            "🚀 Aventi — Everything that makes life feel alive: fun, adventure, events, dating, travel, friendships, discovery, spontaneity\n\n"
            "Which of these feel most alive to you right now? (You can mention all that resonate)"
        ),
    },
    {
        "question_id": "ivive_selection",
        "field_name": "ivive_interests",
        "question": (
            "iVive is the maintenance and growth of you — body, mind, spirit, creativity, finances, skills, and habits. What would support you most here?\n"
            "• 💪 Get physically stronger and fitter\n"
            "• 🧠 Improve my mental health and emotional wellbeing\n"
            "• ✨ Deepen my spiritual practice or sense of purpose\n"
            "• 💤 Sleep better and have more energy\n"
            "• 🎨 Develop a creative practice\n"
            "• 💰 Get my finances stable and growing\n"
            "• 📚 Learn something that makes me more capable\n"
            "• 🧘 Build rituals and habits that ground me\n"
            "• ✍️ Write your own..."
        ),
    },
    {
        "question_id": "eviva_selection",
        "field_name": "eviva_interests",
        "question": (
            "Eviva is about how you show up for the world — and how the world rewards you for it. What feels most meaningful here?\n"
            "• 💼 Build a career that actually means something\n"
            "• 🙌 Volunteer for a cause you care about\n"
            "• 🏗 Contribute to an open-source or community project\n"
            "• 🌱 Start something that gives back\n"
            "• 💰 Create income from skills that serve others\n"
            "• 🏛 Participate in governance or civic life\n"
            "• 🤲 Mentor someone or teach what you know\n"
            "• ✍️ Write your own..."
        ),
    },
    {
        "question_id": "aventi_selection",
        "field_name": "aventi_interests",
        "question": (
            "Aventi is everything that makes life feel alive and engaged — fun, play, pleasure, dating, friendship, spontaneity, and experience. What would feel amazing here?\n"
            "• 🌍 Travel somewhere new this year\n"
            "• 🎉 Go to more events — concerts, festivals, markets\n"
            "• 💘 Date intentionally and find real connection\n"
            "• 🤝 Invest in a friendship you've been neglecting\n"
            "• 🏄 Try a thrilling new physical experience\n"
            "• 🌙 Make weeknights an adventure, not just weekends\n"
            "• 🎲 Say yes to spontaneous plans more often\n"
            "• ✍️ Write your own..."
        ),
    },
    {"question_id": "onboarding_constraints", "field_name": "constraints", "question": "Any constraints I should know about — location, budget, health, family situation, access, or anything that would shape the path?"},
]

DOMAIN_ALIASES = {
    "ivive": ["ivive", "self", "health", "fitness", "vitality", "energy", "nutrition", "sleep", "strong", "mental", "therapy", "emotional", "stress", "spiritual", "purpose", "meaning", "inner", "creative", "creativity", "finance", "budget", "saving", "skill", "learning", "habit", "ritual"],
    "eviva": ["eviva", "career", "work", "job", "volunteer", "service", "contribution", "collective", "community project", "open source", "dao", "civic", "governance", "income", "recognition", "mentor", "teach", "build for others"],
    "aventi": ["aventi", "adventure", "fun", "travel", "experience", "spontaneity", "spontaneous", "dating", "romance", "friendship", "friend", "event", "concert", "festival", "market", "play", "pleasure", "discovery"],
}

IVIVE_ALIASES = {
    "Get physically stronger and fitter": ["strong", "fit", "fitness", "physical"],
    "Improve my mental health and emotional wellbeing": ["mental", "therapy", "emotional", "wellbeing", "stress"],
    "Deepen my spiritual practice or sense of purpose": ["spiritual", "purpose", "meaning", "inner peace"],
    "Sleep better and have more energy": ["sleep", "energy"],
    "Develop a creative practice": ["creative", "creativity", "art", "music", "writing"],
    "Get my finances stable and growing": ["finance", "finances", "budget", "saving", "invest"],
    "Learn something that makes me more capable": ["skill", "learn", "capable"],
    "Build rituals and habits that ground me": ["ritual", "habit", "routine", "ground"],
}

EVIVA_ALIASES = {
    "Build a career that actually means something": ["career", "work", "job", "meaning"],
    "Volunteer for a cause you care about": ["volunteer", "cause", "service"],
    "Contribute to an open-source or community project": ["open-source", "open source", "community project", "contribute"],
    "Start something that gives back": ["gives back", "give back", "start something"],
    "Create income from skills that serve others": ["income", "skills", "serve others"],
    "Participate in governance or civic life": ["governance", "civic", "dao", "vote"],
    "Mentor someone or teach what you know": ["mentor", "teach", "teaching"],
}

AVENTI_ALIASES = {
    "Travel somewhere new this year": ["travel", "new places", "country", "trip"],
    "Go to more events — concerts, festivals, markets": ["event", "concert", "festival", "market"],
    "Date intentionally and find real connection": ["date", "dating", "romance", "romantic", "connection"],
    "Invest in a friendship you've been neglecting": ["friend", "friendship", "neglect"],
    "Try a thrilling new physical experience": ["thrilling", "surf", "ski", "skydive", "physical experience"],
    "Make weeknights an adventure, not just weekends": ["weeknight", "weeknights", "weekend"],
    "Say yes to spontaneous plans more often": ["spontaneous", "say yes", "plans"],
}


def _extract_selected_domains(answer: str) -> list[str]:
    text = answer.lower()
    selected = []
    canonical = {"ivive": "iVive", "eviva": "Eviva", "aventi": "Aventi"}
    for key, aliases in DOMAIN_ALIASES.items():
        if any(alias in text for alias in aliases):
            selected.append(canonical[key])
    return selected


def _extract_picks(answer: str, aliases: dict[str, list[str]]) -> list[str]:
    text = answer.lower()
    picks = []
    for label, terms in aliases.items():
        if label.lower() in text or any(term in text for term in terms):
            picks.append(label)
    if not picks and answer.strip():
        picks.append(answer.strip()[:240])
    return picks



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
                        "Ora uses three domains: iVive (maintenance and growth of self: physical, mental, spiritual, creative, financial, skills, habits), Eviva (contribution to the collective and reward: work, service, DAO/open-source/civic participation, income, recognition), and Aventi (aliveness: fun, adventure, events, dating, travel, friendships, discovery, spontaneity). "
                        "Contributing to Connectome or Ascension Technologies and earning CP/recognition is Eviva. "
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
