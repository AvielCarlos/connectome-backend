"""
Discovery API Routes
Handles user answers to Ora's discovery interview questions.
Answers update the user profile for better personalisation.
"""

import logging
import json
import random
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.middleware import get_current_user_id
from core.database import execute, fetch, fetchrow, fetchval
from uuid import UUID

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/discovery", tags=["discovery"])


class DiscoveryAnswerBody(BaseModel):
    question_id: str
    answer: Any                 # str | int | float | list
    profile_field: str


ONBOARDING_VARIANTS = {
    "A": {
        "name": "Domain Explorer",
        "description": "Current flow, broadened across iVive, Eviva, and Aventi with current-state framing.",
        "weight": 25,
        "questions": [
            {"question_id": "onboarding_name", "field_name": "name", "kind": "identity", "question": "What's your name?"},
            {"question_id": "domain_selection", "field_name": "domain_selection", "kind": "current", "render_hint": "domain_cards", "question": "Nice to meet you, [name]! I see your life in three dimensions:\n🌱 iVive — your vitality, health, mind, inner world\n🌊 Eviva — your work, contribution, social life, love\n🚀 Aventi — your adventures, fun, travel, experiences\n\nWhich of these feel most alive for you right now? And which feel quiet or neglected?"},
            {"question_id": "top_domain_current_state", "field_name": "top_domain_current_state", "kind": "current", "question": "Tell me what you're actually doing day-to-day in [top domain]. What's working? What's stuck?"},
            {"question_id": "three_month_activation", "field_name": "three_month_activation", "kind": "aspiration", "question": "And what would feel most exciting to activate in the next 3 months? Across any domain."},
            {"question_id": "weekly_capacity", "field_name": "weekly_capacity", "kind": "capacity", "question": "How much time can you realistically dedicate each week?"},
            {"question_id": "top_values", "field_name": "value_weights", "kind": "values", "render_hint": "value_sliders", "question": "Last: rate your top-level values from 1–10. These are not fixed labels — Aura will learn and adapt them as you swipe, choose, and complete actions."},
            {"question_id": "onboarding_constraints", "field_name": "constraints", "kind": "constraint", "question": "Anything holding you back right now — financial, time, location, health?"},
        ],
    },
    "B": {
        "name": "Current Life Snapshot",
        "description": "Maps the user's current lived reality across all three domains before naming the highest-leverage shift.",
        "weight": 25,
        "questions": [
            {"question_id": "onboarding_name", "field_name": "name", "kind": "identity", "question": "What's your name?"},
            {"question_id": "ivive_snapshot", "field_name": "ivive_current_state", "kind": "current", "question": "Hey [name]! Let's map your life as it is RIGHT NOW. I'll ask about three areas:\nFirst — iVive. How are you feeling physically and mentally? What's your energy like day-to-day?"},
            {"question_id": "eviva_snapshot", "field_name": "eviva_current_state", "kind": "current", "question": "Now Eviva — your connection to the world. Work, relationships, contribution. What's flowing? What's missing?"},
            {"question_id": "aventi_snapshot", "field_name": "aventi_current_state", "kind": "current", "question": "And Aventi — the fun, aliveness side. Adventures, experiences, spontaneity. When did you last feel truly alive?"},
            {"question_id": "biggest_difference", "field_name": "biggest_difference", "kind": "aspiration", "question": "Based on everything, what's the ONE thing that would make the biggest difference in how your life feels?"},
            {"question_id": "top_values", "field_name": "value_weights", "kind": "values", "render_hint": "value_sliders", "question": "Rate your top-level values from 1–10. Aura will treat these as starting weights, then learn from what you swipe, select, and achieve."},
            {"question_id": "onboarding_constraints", "field_name": "constraints", "kind": "constraint", "question": "What's your biggest constraint right now — time, money, location, or something else?"},
        ],
    },
    "C": {
        "name": "Future Pull",
        "description": "Starts from a fulfilled 12-month vision, then grounds the gap in today's reality.",
        "weight": 25,
        "questions": [
            {"question_id": "onboarding_name", "field_name": "name", "kind": "identity", "question": "What's your name?"},
            {"question_id": "fulfilled_day", "field_name": "fulfilled_day_vision", "kind": "aspiration", "question": "[name], imagine waking up 12 months from now feeling completely fulfilled. Walk me through your day — what does it look like across iVive (your body/mind/soul), Eviva (your work/love/community), and Aventi (your adventures/fun/experiences)?"},
            {"question_id": "domain_gap", "field_name": "domain_gap", "kind": "mixed", "question": "What's the gap between that vision and your life today? Which domain feels furthest from where you want to be?"},
            {"question_id": "already_tried", "field_name": "already_tried", "kind": "current", "question": "What have you already tried in that area? What worked? What didn't?"},
            {"question_id": "weekly_capacity", "field_name": "weekly_capacity", "kind": "capacity", "question": "How many hours per week could you actually dedicate to making this shift?"},
            {"question_id": "top_values", "field_name": "value_weights", "kind": "values", "render_hint": "value_sliders", "question": "Rate your top-level values from 1–10. Start with your honest priorities; Aura will update them as your real choices reveal what matters."},
            {"question_id": "onboarding_constraints", "field_name": "constraints", "kind": "constraint", "question": "Any hard constraints — health, money, location, relationships — that I need to account for?"},
        ],
    },
    "D": {
        "name": "Energy Mapping",
        "description": "Uses quick self-ratings to identify low-energy and high-energy domains.",
        "weight": 25,
        "questions": [
            {"question_id": "onboarding_name", "field_name": "name", "kind": "identity", "question": "What's your name?"},
            {"question_id": "energy_scores", "field_name": "energy_scores", "kind": "current", "render_hint": "energy_sliders", "question": "Good to meet you, [name]. Let's do a quick energy audit. On a scale of 1-10:\n• iVive (body/mind/soul vitality): ___\n• Eviva (work/relationships/contribution): ___  \n• Aventi (fun/adventure/aliveness): ___\nTell me your scores and what's behind the numbers."},
            {"question_id": "lowest_score_cause", "field_name": "lowest_score_cause", "kind": "mixed", "question": "Your lowest score — what's actually causing that? And what would a 9 or 10 look like?"},
            {"question_id": "highest_score_pattern", "field_name": "highest_score_pattern", "kind": "current", "question": "Your highest score — what's working there that we could apply to the weaker areas?"},
            {"question_id": "change_capacity", "field_name": "change_capacity", "kind": "capacity", "question": "What's your capacity for change right now — time, energy, money?"},
            {"question_id": "top_values", "field_name": "value_weights", "kind": "values", "render_hint": "value_sliders", "question": "Now rate your top-level values from 1–10. This gives Aura a starting compass; your swipes, choices, and achievements will keep refining it."},
            {"question_id": "onboarding_constraints", "field_name": "constraints", "kind": "constraint", "question": "Any constraints or context I should know about?"},
        ],
    },
}

ONBOARDING_QUESTIONS = ONBOARDING_VARIANTS["A"]["questions"]

TOP_LEVEL_VALUES = [
    "enlightenment", "peace", "pleasure", "love", "vitality",
    "freedom", "mastery", "contribution", "abundance", "adventure",
]

VALUE_ALIASES = {
    "enlightenment": ["enlightenment", "awakening", "truth", "wisdom", "consciousness", "spiritual"],
    "peace": ["peace", "calm", "ease", "serenity", "safety", "regulation"],
    "pleasure": ["pleasure", "joy", "fun", "sensual", "enjoyment", "play"],
    "love": ["love", "relationship", "connection", "family", "friendship", "romance", "belonging"],
    "vitality": ["vitality", "energy", "health", "fitness", "body", "sleep", "nutrition"],
    "freedom": ["freedom", "sovereignty", "time", "travel", "choice", "independence"],
    "mastery": ["mastery", "skill", "learning", "craft", "practice", "discipline"],
    "contribution": ["contribution", "service", "impact", "community", "volunteer", "dao", "help"],
    "abundance": ["abundance", "money", "wealth", "income", "career", "business", "resources"],
    "adventure": ["adventure", "novelty", "discovery", "explore", "event", "travel", "experience"],
}

DOMAIN_ALIASES = {
    "ivive": ["ivive", "self", "health", "fitness", "vitality", "energy", "nutrition", "sleep", "strong", "mental", "therapy", "emotional", "stress", "spiritual", "purpose", "meaning", "inner", "creative", "creativity", "finance", "budget", "saving", "skill", "learning", "habit", "ritual", "body", "mind", "soul"],
    "eviva": ["eviva", "career", "work", "job", "volunteer", "service", "contribution", "collective", "community", "open source", "dao", "civic", "governance", "income", "recognition", "mentor", "teach", "build for others", "relationship", "love"],
    "aventi": ["aventi", "adventure", "fun", "travel", "experience", "spontaneity", "spontaneous", "dating", "romance", "friendship", "friend", "event", "concert", "festival", "market", "play", "pleasure", "discovery", "alive", "aliveness"],
}


def _extract_selected_domains(answer: str) -> list[str]:
    text = answer.lower()
    selected = []
    canonical = {"ivive": "iVive", "eviva": "Eviva", "aventi": "Aventi"}
    for key, aliases in DOMAIN_ALIASES.items():
        if any(alias in text for alias in aliases):
            selected.append(canonical[key])
    return selected


def _infer_domain(text: str) -> str:
    selected = _extract_selected_domains(text)
    return selected[0] if selected else "Whole Life"


def _parse_value_weights(text: str) -> dict[str, int]:
    """Parse slider-style onboarding text like 'peace: 8/10'."""
    import re
    weights: dict[str, int] = {}
    lowered = text.lower()
    for value in TOP_LEVEL_VALUES:
        aliases = [value, *VALUE_ALIASES.get(value, [])]
        for alias in aliases:
            pattern = rf"{re.escape(alias)}\s*[:=\-]?\s*(10|[1-9])(?:\s*/\s*10)?"
            match = re.search(pattern, lowered)
            if match:
                weights[value] = max(1, min(10, int(match.group(1))))
                break
    return weights


def _render_question(question: dict, answers: list[str]) -> str:
    text = question["question"]
    name = answers[0].strip().split("\n")[0][:80] if answers else "there"
    if name:
        text = text.replace("[name]", name)
    top_domain = "the area that feels most alive or neglected"
    if len(answers) >= 2:
        domains = _extract_selected_domains(answers[1])
        if domains:
            top_domain = domains[0]
    return text.replace("[top domain]", top_domain)


def _questions_for_variant(variant_id: str) -> list[dict]:
    return ONBOARDING_VARIANTS.get(variant_id, ONBOARDING_VARIANTS["A"])["questions"]


class OnboardingRequest(BaseModel):
    conversation: list[dict] = Field(default_factory=list)  # [{role: "user"|"ora", content: "..."}]


class OnboardingResponse(BaseModel):
    message: str
    is_complete: bool
    question_index: int
    total_questions: int = 6
    variant_id: str = "A"
    render_hint: Optional[str] = None


class OnboardingVariantStats(BaseModel):
    variant_id: str
    name: Optional[str] = None
    assigned_users: int = 0
    completed_users: int = 0
    completion_rate: float = 0.0
    average_ioo_engagement_after_7_days: float = 0.0


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


async def _aura_onboarding_message(
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


async def _ensure_onboarding_schema() -> None:
    await execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_completed BOOLEAN DEFAULT FALSE")
    await execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_completed_at TIMESTAMP")
    await execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_variant TEXT")
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
    await execute(
        """
        CREATE TABLE IF NOT EXISTS onboarding_variants (
            id SERIAL PRIMARY KEY,
            variant_id TEXT NOT NULL UNIQUE,
            name TEXT,
            description TEXT,
            questions JSONB NOT NULL,
            active BOOLEAN DEFAULT true,
            weight INTEGER DEFAULT 25,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    for variant_id, config in ONBOARDING_VARIANTS.items():
        await execute(
            """
            INSERT INTO onboarding_variants (variant_id, name, description, questions, active, weight)
            VALUES ($1, $2, $3, $4::jsonb, true, $5)
            ON CONFLICT (variant_id) DO UPDATE SET
                name = EXCLUDED.name,
                description = EXCLUDED.description,
                questions = EXCLUDED.questions,
                active = EXCLUDED.active,
                weight = EXCLUDED.weight
            """,
            variant_id,
            config["name"],
            config["description"],
            json.dumps(config["questions"]),
            int(config.get("weight", 25)),
        )


async def _assign_onboarding_variant(uid: UUID) -> str:
    await _ensure_onboarding_schema()
    row = await fetchrow("SELECT onboarding_variant FROM users WHERE id = $1", uid)
    if row and row["onboarding_variant"] in ONBOARDING_VARIANTS:
        return row["onboarding_variant"]

    rows = await fetch("SELECT variant_id, weight FROM onboarding_variants WHERE active = true")
    weighted = [(r["variant_id"], int(r["weight"] or 0)) for r in rows if r["variant_id"] in ONBOARDING_VARIANTS]
    if not weighted:
        weighted = [(vid, int(cfg.get("weight", 25))) for vid, cfg in ONBOARDING_VARIANTS.items()]
    total = sum(max(weight, 0) for _, weight in weighted) or len(weighted)
    pick = random.uniform(0, total)
    cursor = 0.0
    selected = weighted[-1][0]
    for variant_id, weight in weighted:
        cursor += max(weight, 0) or 1
        if pick <= cursor:
            selected = variant_id
            break

    await execute("UPDATE users SET onboarding_variant = $1, last_active = NOW() WHERE id = $2", selected, uid)
    await execute(
        """
        INSERT INTO discovery_profile (user_id, field_name, field_value, question_id)
        VALUES ($1, 'onboarding_variant', $2, 'onboarding_variant')
        ON CONFLICT (user_id, field_name) DO UPDATE SET
            field_value = EXCLUDED.field_value,
            updated_at = NOW()
        """,
        uid,
        selected,
    )
    return selected


async def _seed_onboarding_ioo_nodes(uid: UUID, variant_id: str, answer_map: dict[str, str], questions: list[dict]) -> None:
    current_items: list[tuple[str, str]] = []
    aspiration_items: list[tuple[str, str]] = []
    for q in questions:
        answer = (answer_map.get(q["field_name"]) or "").strip()
        if not answer:
            continue
        domain = _infer_domain(" ".join([q.get("field_name", ""), q.get("question", ""), answer]))
        if q.get("kind") in {"current", "mixed"}:
            current_items.append((domain, answer))
        if q.get("kind") in {"aspiration", "mixed"}:
            aspiration_items.append((domain, answer))

    async def insert_seed(domain: str, text: str, seed_type: str) -> None:
        title = f"{seed_type.title()}: {text[:72]}"
        description = text[:1000]
        progress_status = "started" if seed_type == "current" else "suggested"
        row = await fetchrow(
            """
            INSERT INTO ioo_nodes (type, title, description, tags, domain, step_type, requirements, is_active)
            VALUES ('activity', $1, $2, $3::text[], $4, 'hybrid', $5::jsonb, true)
            RETURNING id
            """,
            title,
            description,
            ["onboarding", seed_type, variant_id],
            domain,
            json.dumps({"source": "onboarding", "seed_type": seed_type, "variant_id": variant_id}),
        )
        if row:
            await execute(
                """
                INSERT INTO ioo_user_progress (user_id, node_id, status, started_at, surface_type, surface_id)
                VALUES ($1, $2, $3, CASE WHEN $3 = 'started' THEN NOW() ELSE NULL END, 'onboarding', $4)
                """,
                uid,
                row["id"],
                progress_status,
                variant_id,
            )

    try:
        for domain, text in current_items[:4]:
            await insert_seed(domain, text, "current")
        for domain, text in aspiration_items[:3]:
            await insert_seed(domain, text, "aspiration")
        await execute(
            """
            INSERT INTO ioo_user_state (user_id, state_json, last_updated)
            VALUES ($1, $2::jsonb, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                state_json = COALESCE(ioo_user_state.state_json, '{}'::jsonb) || EXCLUDED.state_json,
                last_updated = NOW()
            """,
            uid,
            json.dumps({"onboarding_variant": variant_id, "current_seeds": [x[1] for x in current_items], "aspiration_seeds": [x[1] for x in aspiration_items]}),
        )
    except Exception as e:
        logger.warning(f"Could not seed onboarding IOO nodes: {e}")


async def _store_onboarding_answers(user_id: str, answers: list[str], variant_id: str) -> None:
    uid = UUID(user_id)
    await _ensure_onboarding_schema()
    questions = _questions_for_variant(variant_id)

    answer_map = {"onboarding_variant": variant_id}
    for idx, answer in enumerate(answers[: len(questions)]):
        q = questions[idx]
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

    current_state = {q["field_name"]: answer_map.get(q["field_name"], "") for q in questions if q.get("kind") in {"current", "mixed"}}
    aspirations = {q["field_name"]: answer_map.get(q["field_name"], "") for q in questions if q.get("kind") in {"aspiration", "mixed"}}
    value_weights = _parse_value_weights(answer_map.get("value_weights", ""))
    row = await fetchrow("SELECT profile FROM users WHERE id = $1", uid)
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    profile = {}
    raw_profile = row["profile"]
    if raw_profile:
        profile = json.loads(raw_profile) if isinstance(raw_profile, str) else dict(raw_profile)
    profile["onboarding_variant"] = variant_id
    profile["onboarding_intake"] = answer_map
    profile["onboarding_current_state"] = current_state
    profile["onboarding_aspirations"] = aspirations
    if value_weights:
        profile["value_weights"] = value_weights
    if answer_map.get("name"):
        profile["display_name"] = answer_map["name"]

    await execute(
        """
        UPDATE users
        SET profile = $1,
            display_name = COALESCE(NULLIF($2, ''), display_name),
            onboarding_variant = $3,
            onboarding_completed = TRUE,
            onboarding_completed_at = NOW(),
            last_active = NOW()
        WHERE id = $4
        """,
        json.dumps(profile),
        answer_map.get("name", ""),
        variant_id,
        uid,
    )

    if value_weights:
        try:
            await execute(
                """
                INSERT INTO ioo_user_state (user_id, state_json, last_updated)
                VALUES ($1, $2::jsonb, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    state_json = COALESCE(ioo_user_state.state_json, '{}'::jsonb) || EXCLUDED.state_json,
                    last_updated = NOW()
                """,
                uid,
                json.dumps({"value_weights": value_weights, "value_weights_source": "onboarding_explicit_1_10"}),
            )
        except Exception as e:
            logger.warning(f"Could not store onboarding value weights: {e}")

    await _seed_onboarding_ioo_nodes(uid, variant_id, answer_map, questions)

    try:
        client = _get_openai()
        if client:
            intake_text = "\n".join(
                f"{questions[i]['field_name']}: {answer}"
                for i, answer in enumerate(answers[: len(questions)])
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

    # Re-embed user vector when a capability/context answer comes in
    # so the IOO/vector search immediately reflects the user's current state
    CAPABILITY_FIELDS = {
        "today_capacity", "current_energy_state", "available_resources_today",
        "energy_state", "capacity", "resources",
    }
    if body.profile_field in CAPABILITY_FIELDS or body.question_id.startswith("feed_capability"):
        import asyncio
        from ora.user_model import update_user_embedding_from_context
        context = {body.profile_field: body.answer}
        # Capability intake refines the Now vector; desired_feed_mode/later interests refine Later.
        vector_mode = "later" if body.profile_field in {"desired_feed_mode", "later_interests", "future_interests"} else "now"
        asyncio.ensure_future(update_user_embedding_from_context(user_id, context, vector_mode))

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
    uid = UUID(user_id)
    await _ensure_onboarding_schema()
    row = await fetchrow("SELECT id, onboarding_completed FROM users WHERE id = $1", uid)
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    variant_id = await _assign_onboarding_variant(uid)
    questions = _questions_for_variant(variant_id)
    answered_count = min(len(user_turns), len(questions))

    if bool(row["onboarding_completed"]):
        return OnboardingResponse(
            message="I've got what I need to start building your path. Let's go! ◈",
            is_complete=True,
            question_index=len(questions),
            total_questions=len(questions),
            variant_id=variant_id,
        )

    if answered_count >= len(questions):
        await _store_onboarding_answers(user_id, user_turns, variant_id)
        return OnboardingResponse(
            message="I've got what I need to start building your path. Let's go! ◈",
            is_complete=True,
            question_index=len(questions),
            total_questions=len(questions),
            variant_id=variant_id,
        )

    next_question_obj = questions[answered_count]
    next_question = _render_question(next_question_obj, user_turns)
    message = await _aura_onboarding_message(
        body.conversation,
        next_question=next_question,
        is_complete=False,
    )
    return OnboardingResponse(
        message=message,
        is_complete=False,
        question_index=answered_count,
        total_questions=len(questions),
        variant_id=variant_id,
        render_hint=next_question_obj.get("render_hint"),
    )


@router.get("/onboarding/status")
async def onboarding_status(user_id: str = Depends(get_current_user_id)):
    await _ensure_onboarding_schema()
    row = await fetchrow("SELECT onboarding_completed, onboarding_variant FROM users WHERE id = $1", UUID(user_id))
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    completed = bool(row["onboarding_completed"])
    variant_id = row["onboarding_variant"] or None
    try:
        count = await fetchval(
            """
            SELECT COUNT(*) FROM discovery_profile
            WHERE user_id = $1 AND field_name <> 'onboarding_variant'
            """,
            UUID(user_id),
        )
    except Exception as e:
        logger.warning(f"Could not count discovery_profile rows for onboarding status: {e}")
        count = 0
    return {"completed": completed, "question_index": int(count or 0), "variant_id": variant_id}


@router.get("/onboarding/variants")
async def onboarding_variant_stats(user_id: str = Depends(get_current_user_id)):
    """Return onboarding A/B variant stats for analytics dashboards."""
    await _ensure_onboarding_schema()
    variant_rows = await fetch("SELECT variant_id, name FROM onboarding_variants WHERE active = true ORDER BY variant_id")
    stats = []
    for variant in variant_rows:
        variant_id = variant["variant_id"]
        assigned = int(await fetchval("SELECT COUNT(*) FROM users WHERE onboarding_variant = $1", variant_id) or 0)
        completed = int(await fetchval(
            "SELECT COUNT(*) FROM users WHERE onboarding_variant = $1 AND onboarding_completed = true",
            variant_id,
        ) or 0)
        engagement = float(await fetchval(
            """
            SELECT COALESCE(AVG(progress_count), 0) FROM (
                SELECT u.id, COUNT(p.id)::float AS progress_count
                FROM users u
                LEFT JOIN ioo_user_progress p
                    ON p.user_id = u.id
                   AND p.created_at >= u.onboarding_completed_at
                   AND p.created_at < u.onboarding_completed_at + INTERVAL '7 days'
                WHERE u.onboarding_variant = $1
                  AND u.onboarding_completed = true
                GROUP BY u.id
            ) s
            """,
            variant_id,
        ) or 0.0)
        stats.append({
            "variant_id": variant_id,
            "name": variant["name"],
            "assigned_users": assigned,
            "completed_users": completed,
            "completion_rate": round(completed / assigned, 4) if assigned else 0.0,
            "average_ioo_engagement_after_7_days": round(engagement, 4),
        })
    return {"variants": stats}
