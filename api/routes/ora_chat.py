"""
Ora Chat API — Talk directly to Ora.

Endpoints:
  POST /api/ora/chat         — Send a message to Ora
  GET  /api/ora/reflect      — Latest reflection
  GET  /api/ora/explain/{id} — Why was this screen shown?
  GET  /api/ora/self         — Ora's current state
"""

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.middleware import get_current_user_id
from core.database import fetchrow, fetch
from ora.brain import get_brain

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ora", tags=["ora"])


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
    ora_state: Dict[str, Any]


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

    # Build a lightweight ora_state for the mobile UI
    uncertainty = await consciousness.articulate_uncertainty(user_id)
    ora_state = {
        "mood_hint": "curious",
        "confidence": uncertainty.get("confidence_overall", 0.5),
    }

    return ChatResponse(reply=reply, ora_state=ora_state)


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
            reflection = await consciousness.reflect()
            return reflection.to_dict()
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
        if r.get(key):
            r[key] = r[key].isoformat()
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
async def get_ora_self(
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
        raise HTTPException(status_code=500, detail="Could not retrieve Ora state")

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
        return {"message": "Hi. I'm Ora. I'm here if you want to talk."}

    try:
        message = await consciousness.opening_message(user_id)
    except Exception as e:
        logger.warning(f"Opening message error: {e}")
        message = "Hi. I'm Ora. I'm here if you want to talk."

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
async def teach_ora(
    payload: LearnPayload,
    user_id: str = Depends(get_current_user_id),
):
    """Inject a lesson directly into Ora's long-term memory (ora_lessons table)."""
    import json as _json
    from core.database import execute as _execute
    from uuid import UUID as _UUID
    try:
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
        return {"ok": True, "lesson": payload.lesson[:80]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/lessons")
async def get_ora_lessons(
    limit: int = 20,
    user_id: str = Depends(get_current_user_id),
):
    """Return Ora's most recent lessons — what she knows and why."""
    from core.database import fetch as _fetch
    rows = await _fetch(
        "SELECT source, lesson, confidence, applies_to, created_at FROM ora_lessons ORDER BY created_at DESC LIMIT $1",
        limit
    )
    return [dict(r) for r in rows]
