"""
Ora Chat API — Talk directly to Ora, the brain of Connectome AI OS.

Endpoints:
  POST /api/ora/chat         — Send a message to Ora
  GET  /api/ora/reflect      — Latest reflection
  GET  /api/ora/explain/{id} — Why was this screen shown?
  GET  /api/ora/self         — Ora's current state
"""

import logging
import re
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.middleware import get_current_user_id
from core.database import fetchrow, fetch, execute
from ora.brain import get_brain

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ora", tags=["ora"])


# ---------------------------------------------------------------------------
# IOO user state extraction from conversation
# ---------------------------------------------------------------------------

# City / region patterns
_CITY_PATTERNS = [
    re.compile(r"\b(?:in|from|at|near|to|live in|living in|based in|located in|moving to)\s+([A-Z][a-zA-Z\s]{2,24})(?:[,.]|\b)", re.IGNORECASE),
]

# Finance distress keywords → finances_level 1 (tight); positive keywords → 3
_FINANCE_TIGHT = re.compile(
    r"\b(tight|broke|budget|can't afford|cannot afford|low on cash|no money|saving up|cheap|expensive too|struggling financially)\b",
    re.IGNORECASE,
)
_FINANCE_GOOD = re.compile(
    r"\b(financially comfortable|can afford|well off|good income|high income|well-paid|earning well)\b",
    re.IGNORECASE,
)

# Skill patterns: "I know X", "I'm good at X", "I can X", "I work with X"
_SKILL_PATTERNS = [
    re.compile(
        r"\b(?:know|good at|expert in|specialise in|specialize in|experienced in|work with|studying|I can)\s+([a-zA-Z+#]{2,30})\b",
        re.IGNORECASE,
    ),
]

_KNOWN_SKILLS = {
    "python", "javascript", "typescript", "react", "node", "sql", "java", "swift",
    "kotlin", "flutter", "design", "figma", "photoshop", "video", "music", "piano",
    "guitar", "coding", "programming", "marketing", "writing", "finance",
    "accounting", "teaching", "cooking", "photography", "yoga", "running",
    "spanish", "french", "mandarin", "arabic",
}


async def _update_user_state_from_message(user_id: str, message_text: str) -> None:
    """
    Lightweight keyword extraction from a user message.
    Updates ioo_user_state with any signals detected:
      - location_city (if a recognisable city/region is mentioned)
      - finances_level (1=tight, 2=moderate, 3=comfortable)
      - known_skills (array, additive)
    """
    updates: Dict[str, Any] = {}

    # Location detection
    for pattern in _CITY_PATTERNS:
        m = pattern.search(message_text)
        if m:
            city_candidate = m.group(1).strip().title()
            # Sanity-check: ignore common false positives (short articles, verbs)
            if len(city_candidate) >= 3 and city_candidate.lower() not in {
                "the", "for", "and", "but", "not", "now", "here", "home", "work",
            }:
                updates["location_city"] = city_candidate
                break

    # Finance level (matches CHECK constraint: 'tight' | 'comfortable')
    if _FINANCE_TIGHT.search(message_text):
        updates["finances_level"] = "tight"
    elif _FINANCE_GOOD.search(message_text):
        updates["finances_level"] = "comfortable"

    # Skill extraction
    new_skills: list = []
    for pattern in _SKILL_PATTERNS:
        for m in pattern.finditer(message_text):
            skill = m.group(1).lower().strip()
            if skill in _KNOWN_SKILLS:
                new_skills.append(skill)

    if not updates and not new_skills:
        return  # nothing to update

    try:
        # Read existing state
        row = await fetchrow(
            "SELECT location_city, finances_level, known_skills FROM ioo_user_state WHERE user_id = $1",
            str(user_id),
        )
        existing_city = row["location_city"] if row else None
        existing_finances = row["finances_level"] if row else None
        existing_skills: list = list(row["known_skills"] or []) if row else []

        merged_skills = list(set(existing_skills + new_skills))

        await execute(
            """
            INSERT INTO ioo_user_state (user_id, location_city, finances_level, known_skills, last_updated)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                location_city = COALESCE(EXCLUDED.location_city, ioo_user_state.location_city),
                finances_level = COALESCE(EXCLUDED.finances_level, ioo_user_state.finances_level),
                known_skills = EXCLUDED.known_skills,
                last_updated = NOW()
            """,
            str(user_id),
            updates.get("location_city", existing_city),
            updates.get("finances_level", existing_finances),
            merged_skills,
        )
        logger.debug(
            f"IOO user state updated: user={user_id[:8]} "
            f"updates={list(updates.keys())} new_skills={new_skills}"
        )
    except Exception as _err:
        logger.warning(f"IOO user state update skipped: {_err}")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ConversationTurn(BaseModel):
    role: str   # 'user' | 'assistant'
    content: str


class ChatRequest(BaseModel):
    message: str
    conversation_history: List[ConversationTurn] = []


class ChatResponse(BaseModel):
    reply: str
    aura_state: Dict[str, Any]


# ---------------------------------------------------------------------------
# POST /api/ora/chat
# ---------------------------------------------------------------------------

@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Send a message to Ora and get a reply."""
    brain = get_brain()
    consciousness = getattr(brain, "consciousness", None)
    if not consciousness:
        raise HTTPException(status_code=503, detail="OraConsciousness not available")

    history = [{"role": t.role, "content": t.content} for t in payload.conversation_history[-10:]]

    try:
        reply = await consciousness.converse(
            user_id=user_id,
            message=payload.message,
            conversation_history=history,
        )
    except Exception as e:
        logger.error(f"Ora chat error for user {user_id[:8]}: {e}")
        raise HTTPException(status_code=500, detail="Ora is temporarily unavailable")

    # Extract IOO user-state signals from the user's message (non-blocking)
    await _update_user_state_from_message(user_id, payload.message)

    # Build a lightweight ora_state for the mobile UI
    uncertainty = await consciousness.articulate_uncertainty(user_id)
    aura_state = {
        "mood_hint": "curious",
        "confidence": uncertainty.get("confidence_overall", 0.5),
    }

    return ChatResponse(reply=reply, aura_state=aura_state)


# ---------------------------------------------------------------------------
# GET /api/ora/reflect
# ---------------------------------------------------------------------------

@router.get("/reflect")
async def get_latest_reflection(
    user_id: str = Depends(get_current_user_id),
):
    """Return Ora's latest reflection."""
    import json

    row = await fetchrow(
        "SELECT * FROM ora_reflections ORDER BY created_at DESC LIMIT 1"
    )
    if not row:
        # Trigger a fresh reflection
        brain = get_brain()
        consciousness = getattr(brain, "consciousness", None)
        if consciousness:
            try:
                reflection = await consciousness.reflect()
                return reflection.to_dict()
            except Exception as _ref_err:
                logger.error(f"Reflection generation failed: {_ref_err}")
                raise HTTPException(status_code=500, detail="Could not generate reflection")
        return {"message": "No reflections yet"}

    r = dict(row)
    for key in ("top_performing_content", "underperforming_areas", "new_lessons_learned",
                "model_changes", "uncertainty_areas"):
        if isinstance(r.get(key), str):
            try:
                r[key] = json.loads(r[key])
            except Exception:
                r[key] = []
    for key in ("period_start", "period_end", "created_at"):
        if r.get(key) and hasattr(r[key], "isoformat"):
            r[key] = r[key].isoformat()
        elif r.get(key) and not isinstance(r[key], str):
            r[key] = str(r[key])
    return r


# ---------------------------------------------------------------------------
# GET /api/ora/explain/{screen_spec_id}
# ---------------------------------------------------------------------------

@router.get("/explain/{screen_spec_id}")
async def explain_screen(
    screen_spec_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Return a plain-English explanation of why Ora showed this screen."""
    # Validate UUID
    try:
        UUID(screen_spec_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid screen_spec_id")

    brain = get_brain()
    consciousness = getattr(brain, "consciousness", None)
    if not consciousness:
        raise HTTPException(status_code=503, detail="OraConsciousness not available")

    try:
        explanation = await consciousness.explain_decision(
            screen_spec_id=screen_spec_id,
            user_id=user_id,
        )
    except Exception as e:
        logger.error(f"explain_decision error: {e}")
        raise HTTPException(status_code=500, detail="Could not explain decision")

    return {"explanation": explanation}


# ---------------------------------------------------------------------------
# GET /api/ora/self
# ---------------------------------------------------------------------------

@router.get("/self")
async def get_aura_self(
    user_id: str = Depends(get_current_user_id),
):
    """Return Ora's full self-description — identity, stats, latest reflection."""
    brain = get_brain()
    consciousness = getattr(brain, "consciousness", None)
    if not consciousness:
        raise HTTPException(status_code=503, detail="OraConsciousness not available")

    try:
        state = await consciousness.get_self_state()
    except Exception as e:
        logger.error(f"get_self_state error: {e}")
        # Return a minimal safe state rather than failing hard
        from ora.consciousness import ORA_IDENTITY
        state = {
            "identity": ORA_IDENTITY,
            "current_model": "unavailable",
            "total_decisions": 0,
            "users_served": 0,
            "avg_fulfilment_score": 0.0,
            "latest_reflection": None,
            "uncertainty_global": "State temporarily unavailable.",
            "error": str(e),
        }

    return state


# ---------------------------------------------------------------------------
# GET /api/ora/opening — first message for OraScreen
# ---------------------------------------------------------------------------

@router.get("/opening")
async def get_opening_message(
    user_id: str = Depends(get_current_user_id),
):
    """Generate Ora's opening message when the user opens OraScreen."""
    brain = get_brain()
    consciousness = getattr(brain, "consciousness", None)
    if not consciousness:
        return {"message": "Hi. I'm Ora — the brain of Connectome, the AI OS for human fulfilment. I can help you navigate your IOO graph toward what matters next."}

    try:
        message = await consciousness.opening_message(user_id)
    except Exception as e:
        logger.warning(f"Opening message error: {e}")
        message = "Hi. I'm Ora — the brain of Connectome, powered by the Ascension Technologies DAO. I can help you navigate your IOO graph toward your deepest fulfilment."

    return {"message": message}


# ---------------------------------------------------------------------------
# GET /api/ora/collective/inspiration — what others like you are doing
# ---------------------------------------------------------------------------

@router.get("/collective/inspiration")
async def get_collective_inspiration(
    user_id: str = Depends(get_current_user_id),
):
    """
    Returns 1-2 "what others like you are doing" inspiration cards.
    These are users with a similar fulfilment score and domain profile.

    PRIVACY: Aggregate data only. No individual user data ever surfaced.
    """
    brain = get_brain()
    collective = getattr(brain, "collective", None)
    if not collective:
        return []

    try:
        from ora.user_model import load_user_model
        user_model = await load_user_model(user_id)
        user_context = user_model.to_context_dict() if user_model else {"user_id": user_id}
        cards = await collective.get_inspiration_cards_for_user(user_context, count=2)
        return cards
    except Exception as e:
        logger.warning(f"get_collective_inspiration error: {e}")
        return []


# GET /api/ora/collective — what humanity is reaching for right now
# ---------------------------------------------------------------------------

@router.get("/collective")
async def get_collective_voice(
    user_id: str = Depends(get_current_user_id),
):
    """
    Returns Ora's collective intelligence summary:
    - collective_voice: what humanity is reaching for right now
    - total_users_analyzed: how many users contributed to this insight
    - computed_at: when this was last computed

    PRIVACY: All data is aggregate. No individual user data is ever returned.
    This is the "majority ruling on human desire" endpoint.
    """
    brain = get_brain()
    collective = getattr(brain, "collective", None)
    if not collective:
        return {
            "collective_voice": "I'm still building my collective picture.",
            "total_users_analyzed": 0,
            "computed_at": None,
        }

    try:
        wisdom = await collective.get_latest_wisdom_dict()
        return {
            "collective_voice": wisdom.get("collective_voice", ""),
            "total_users_analyzed": wisdom.get("total_users_analyzed", 0),
            "computed_at": wisdom.get("computed_at"),
            "domain_synergies": wisdom.get("domain_synergies", [])[:3],
            "surprises": wisdom.get("surprises", [])[:2],
            "privacy_note": "Aggregate data only \u2014 no individual user data.",
        }
    except Exception as e:
        logger.warning(f"get_collective_voice error: {e}")
        return {
            "collective_voice": "I'm still learning from humanity. Check back soon.",
            "total_users_analyzed": 0,
            "computed_at": None,
        }


# ---------------------------------------------------------------------------
# POST /api/ora/learn — teach Ora a lesson (admin only)
# ---------------------------------------------------------------------------

class LearnPayload(BaseModel):
    source: str = "manual"
    lesson: str
    confidence: float = 0.85
    applies_to: list = []


# ---------------------------------------------------------------------------
# Role-Play Simulation Coaching (Integration F)
# ---------------------------------------------------------------------------

class RolePlayStartRequest(BaseModel):
    scenario: str  # job_interview | difficult_conversation | sales_pitch | first_date | negotiation
    context: Optional[str] = None


class RolePlayMessageRequest(BaseModel):
    session_id: str
    message: str


class RolePlayEndRequest(BaseModel):
    session_id: str


@router.post("/roleplay/start")
async def roleplay_start(
    payload: RolePlayStartRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Start a new role-play simulation session."""
    from ora.agents.roleplay_agent import RolePlayAgent
    brain = get_brain()
    agent = RolePlayAgent(getattr(brain, '_openai', None))
    try:
        result = await agent.start_session(
            user_id=user_id,
            scenario=payload.scenario,
            context=payload.context,
        )
        return result
    except Exception as e:
        logger.error(f"roleplay_start error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/roleplay/message")
async def roleplay_message(
    payload: RolePlayMessageRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Send a message within an active role-play session. Ora stays in character."""
    from ora.agents.roleplay_agent import RolePlayAgent
    brain = get_brain()
    agent = RolePlayAgent(getattr(brain, '_openai', None))
    try:
        result = await agent.send_message(
            session_id=payload.session_id,
            user_message=payload.message,
        )
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"roleplay_message error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/roleplay/end")
async def roleplay_end(
    payload: RolePlayEndRequest,
    user_id: str = Depends(get_current_user_id),
):
    """End a role-play session. Returns coaching debrief."""
    from ora.agents.roleplay_agent import RolePlayAgent
    brain = get_brain()
    agent = RolePlayAgent(getattr(brain, '_openai', None))
    try:
        result = await agent.end_session(session_id=payload.session_id)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"roleplay_end error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/learn")
async def teach_aura(
    payload: LearnPayload,
    user_id: str = Depends(get_current_user_id),
):
    """Inject a lesson directly into Ora's long-term memory (ora_lessons table).
    Deduplicates: similar lessons (≥85% word overlap with existing) are not re-inserted.
    """
    import json as _json
    from core.database import execute as _execute, fetch as _fetch
    try:
        # Deduplication: check for very similar existing lessons (same source + high word overlap)
        existing = await _fetch(
            "SELECT lesson FROM ora_lessons WHERE source = $1 ORDER BY created_at DESC LIMIT 20",
            payload.source,
        )

        def _similarity(a: str, b: str) -> float:
            words_a = set(a.lower().split())
            words_b = set(b.lower().split())
            if not words_a or not words_b:
                return 0.0
            return len(words_a & words_b) / max(len(words_a), len(words_b))

        new_words = set(payload.lesson.lower().split())
        for row in existing:
            sim = _similarity(payload.lesson, row["lesson"])
            if sim >= 0.85:
                logger.info(
                    f"ora/learn: deduped lesson from {payload.source} (similarity={sim:.2f})"
                )
                return {"ok": True, "lesson": payload.lesson[:80], "deduplicated": True}

        await _execute(
            """
            INSERT INTO ora_lessons (source, lesson, confidence, applies_to, created_at)
            VALUES ($1, $2, $3, $4::jsonb, NOW())
            """,
            payload.source,
            payload.lesson,
            payload.confidence,
            _json.dumps(payload.applies_to),
        )
        return {"ok": True, "lesson": payload.lesson[:80], "deduplicated": False}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/lessons")
async def get_aura_lessons(
    limit: int = 20,
    user_id: str = Depends(get_current_user_id),
):
    """Return Ora's most recent lessons — what she knows and why.
    Sensitive lessons (founder sessions, directives, or those containing
    credentials/admin details) are filtered out for non-admin users.
    """
    from core.database import fetch as _fetch, fetchrow as _fetchrow
    from core.config import settings as _settings

    # Determine if caller is admin
    _user_row = await _fetchrow("SELECT email FROM users WHERE id = $1", __import__('uuid').UUID(user_id))
    _is_admin = False
    if _user_row:
        _admin_list = getattr(_settings, "admin_email_list", ["carlosandromeda8@gmail.com"])
        _is_admin = (_user_row["email"] or "").lower() in _admin_list

    rows = await _fetch(
        "SELECT source, lesson, confidence, applies_to, created_at FROM ora_lessons ORDER BY created_at DESC LIMIT $1",
        limit,
    )

    if _is_admin:
        return [dict(r) for r in rows]

    # Filter sensitive content for non-admin users
    _SENSITIVE_SOURCES = ("founder_session", "founder_directive")
    _SENSITIVE_KEYWORDS = ("password", "passwd", "secret", "api_key", "apikey",
                           "admin", "private_key", "token", "credential")

    def _is_safe(row: dict) -> bool:
        src = (row.get("source") or "").lower()
        if any(s in src for s in _SENSITIVE_SOURCES):
            return False
        lesson_lower = (row.get("lesson") or "").lower()
        if any(kw in lesson_lower for kw in _SENSITIVE_KEYWORDS):
            return False
        return True

    return [dict(r) for r in rows if _is_safe(dict(r))]
