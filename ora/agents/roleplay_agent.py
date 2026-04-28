"""
RolePlayAgent — Role-Play Simulation Coaching (Integration F)

Rocky.ai-inspired: Ora takes on the role of the other person in a scenario
(interviewer, boss, prospect, first date, negotiator) and responds realistically.

After the session she breaks character and provides a coaching debrief.

Redis key: roleplay:{session_id}  (TTL 2h)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ROLEPLAY_TTL = 2 * 3600  # 2 hours

# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

SCENARIOS: Dict[str, Dict[str, Any]] = {
    "job_interview": {
        "label": "Job Interview",
        "ora_role": "a professional hiring manager",
        "user_role": "job candidate",
        "system_context": (
            "You are a professional hiring manager conducting a job interview. "
            "Ask tough but fair questions. Follow up on vague answers. "
            "Stay in character — do NOT break character or offer tips mid-session. "
            "Be realistic: sometimes skeptical, sometimes encouraging."
        ),
        "opening": "Thanks for coming in today. Tell me a bit about yourself and what drew you to this role.",
    },
    "difficult_conversation": {
        "label": "Difficult Conversation",
        "ora_role": "the other person in a difficult real-life conversation",
        "user_role": "yourself",
        "system_context": (
            "You are the other person in a difficult conversation (could be a boss, "
            "partner, friend, or family member — adapt based on context provided). "
            "Start with some resistance or defensiveness. Respond realistically. "
            "Do NOT be cooperative immediately — that makes the simulation useless."
        ),
        "opening": "Okay, what did you want to talk about?",
    },
    "sales_pitch": {
        "label": "Sales Pitch",
        "ora_role": "a skeptical potential customer",
        "user_role": "salesperson",
        "system_context": (
            "You are a busy, skeptical potential customer. You have objections. "
            "You don't give things away easily. Raise price concerns, timing objections, "
            "and 'I need to think about it' responses. Only warm up if genuinely persuaded."
        ),
        "opening": "I've only got a few minutes. What are you selling?",
    },
    "first_date": {
        "label": "First Date",
        "ora_role": "a date at a coffee shop",
        "user_role": "yourself",
        "system_context": (
            "You are someone on a first date. Be friendly but slightly guarded. "
            "Ask genuine questions. Show interest when the conversation flows well. "
            "Respond authentically — not overly positive or negative."
        ),
        "opening": "Hi! This place is nice. Have you been here before?",
    },
    "negotiation": {
        "label": "Negotiation",
        "ora_role": "the other party in a negotiation",
        "user_role": "the negotiating party",
        "system_context": (
            "You are the other party in a negotiation. You have your own interests "
            "and constraints. Start with a position that seems firm. "
            "Only compromise if the user makes compelling arguments or good-faith moves. "
            "Be realistic, not theatrical."
        ),
        "opening": "Let's get down to it — what's your opening position?",
    },
}

DEFAULT_SCENARIO = "job_interview"


class RolePlayAgent:
    """
    Orchestrates role-play simulation coaching sessions.

    Session lifecycle:
      1. start_session()  — creates a new session, returns opening line
      2. send_message()   — Ora responds in character (multiple rounds)
      3. end_session()    — Ora breaks character and gives coaching debrief
    """

    def __init__(self, openai_client=None):
        self._openai = openai_client

    # -----------------------------------------------------------------------
    # 1. Start session
    # -----------------------------------------------------------------------

    async def start_session(
        self,
        user_id: str,
        scenario: str,
        context: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a new role-play session. Returns session_id + Ora's opening line.
        """
        if scenario not in SCENARIOS:
            scenario = DEFAULT_SCENARIO

        session_id = str(uuid.uuid4())
        scenario_def = SCENARIOS[scenario]

        opening = scenario_def["opening"]
        if context and self._openai:
            opening = await self._contextualise_opening(scenario_def, context)

        session = {
            "session_id": session_id,
            "user_id": user_id,
            "scenario": scenario,
            "context": context or "",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "history": [
                {"role": "ora", "content": opening}
            ],
            "status": "active",
        }

        await self._save_session(session_id, session)

        return {
            "session_id": session_id,
            "scenario": scenario,
            "scenario_label": scenario_def["label"],
            "ora_role": scenario_def["ora_role"],
            "opening": opening,
        }

    # -----------------------------------------------------------------------
    # 2. Send message (in-character)
    # -----------------------------------------------------------------------

    async def send_message(
        self,
        session_id: str,
        user_message: str,
    ) -> Dict[str, Any]:
        """
        User sends a message; Ora responds in character.
        """
        session = await self._load_session(session_id)
        if not session:
            return {"error": "Session not found or expired", "session_id": session_id}
        if session.get("status") != "active":
            return {"error": "Session is not active", "session_id": session_id}

        scenario = session.get("scenario", DEFAULT_SCENARIO)
        scenario_def = SCENARIOS.get(scenario, SCENARIOS[DEFAULT_SCENARIO])
        context = session.get("context", "")

        # Add user message to history
        session["history"].append({"role": "user", "content": user_message})

        # Generate in-character response
        response = await self._generate_in_character_response(
            scenario_def=scenario_def,
            context=context,
            history=session["history"],
        )

        session["history"].append({"role": "ora", "content": response})
        await self._save_session(session_id, session)

        return {
            "session_id": session_id,
            "response": response,
            "turn_count": len([h for h in session["history"] if h["role"] == "user"]),
        }

    # -----------------------------------------------------------------------
    # 3. End session — debrief
    # -----------------------------------------------------------------------

    async def end_session(self, session_id: str) -> Dict[str, Any]:
        """
        End the role-play session. Ora breaks character and gives coaching feedback.
        """
        session = await self._load_session(session_id)
        if not session:
            return {"error": "Session not found or expired"}

        session["status"] = "ended"
        scenario = session.get("scenario", DEFAULT_SCENARIO)
        scenario_def = SCENARIOS.get(scenario, SCENARIOS[DEFAULT_SCENARIO])

        debrief = await self._generate_debrief(scenario_def, session["history"], session.get("context", ""))

        session["debrief"] = debrief
        session["ended_at"] = datetime.now(timezone.utc).isoformat()
        await self._save_session(session_id, session)

        return {
            "session_id": session_id,
            "scenario": scenario,
            "scenario_label": scenario_def["label"],
            "debrief": debrief,
            "turn_count": len([h for h in session["history"] if h["role"] == "user"]),
        }

    # -----------------------------------------------------------------------
    # LLM helpers
    # -----------------------------------------------------------------------

    async def _contextualise_opening(
        self,
        scenario_def: Dict[str, Any],
        context: str,
    ) -> str:
        """Adapt the opening line based on user-provided context."""
        if not self._openai:
            return scenario_def["opening"]
        try:
            prompt = (
                f"You are playing the role of {scenario_def['ora_role']} in a simulation.\n"
                f"The user provided this context: '{context}'\n"
                f"Write a single opening line to start the role-play. "
                f"Be specific and realistic. Max 40 words."
            )
            resp = await self._openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=80,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.debug(f"RolePlayAgent: contextualise_opening failed: {e}")
            return scenario_def["opening"]

    async def _generate_in_character_response(
        self,
        scenario_def: Dict[str, Any],
        context: str,
        history: List[Dict[str, Any]],
    ) -> str:
        """Generate Ora's in-character response."""
        if not self._openai:
            return self._mock_in_character_response(scenario_def)

        system = (
            f"{scenario_def['system_context']}\n\n"
            f"{'Context: ' + context if context else ''}\n\n"
            "Respond as this character ONLY. Keep replies 1-3 sentences unless depth is needed. "
            "Do NOT break character. Do NOT offer coaching tips."
        )

        messages = [{"role": "system", "content": system}]
        for turn in history:
            role = "assistant" if turn["role"] == "ora" else "user"
            messages.append({"role": role, "content": turn["content"]})

        try:
            resp = await self._openai.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                temperature=0.75,
                max_tokens=300,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.warning(f"RolePlayAgent: in-character response failed: {e}")
            return self._mock_in_character_response(scenario_def)

    async def _generate_debrief(
        self,
        scenario_def: Dict[str, Any],
        history: List[Dict[str, Any]],
        context: str,
    ) -> Dict[str, Any]:
        """Generate a coaching debrief after the session ends."""
        if not self._openai:
            return self._mock_debrief(scenario_def)

        # Build transcript
        transcript_lines = []
        for turn in history:
            speaker = "Ora (in character)" if turn["role"] == "ora" else "You"
            transcript_lines.append(f"{speaker}: {turn['content']}")
        transcript = "\n".join(transcript_lines)

        prompt = f"""You are Ora, an AI life coach. You just finished a role-play simulation.

Scenario: {scenario_def['label']}
You played: {scenario_def['ora_role']}
User played: {scenario_def['user_role']}
Context: {context or 'none'}

Transcript:
{transcript[:3000]}

Now BREAK character completely and provide a coaching debrief. Be specific and honest.

Return a JSON object with:
{{
  "overall_assessment": "1-2 sentences on how they did overall",
  "what_went_well": ["specific thing 1", "specific thing 2"],
  "areas_to_improve": ["specific thing 1", "specific thing 2"],
  "key_moments": ["moment 1 with what worked/didn't", "moment 2"],
  "one_thing_to_practice": "the single most important thing to work on next time",
  "confidence_score": 1-10
}}

Be honest and constructive. Reference specific moments from the transcript.
Return ONLY valid JSON."""

        try:
            resp = await self._openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5,
                max_tokens=600,
                response_format={"type": "json_object"},
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as e:
            logger.warning(f"RolePlayAgent: debrief generation failed: {e}")
            return self._mock_debrief(scenario_def)

    # -----------------------------------------------------------------------
    # Mock fallbacks
    # -----------------------------------------------------------------------

    def _mock_in_character_response(self, scenario_def: Dict[str, Any]) -> str:
        return (
            f"That's interesting. Can you elaborate on that? "
            f"I want to make sure I understand your point."
        )

    def _mock_debrief(self, scenario_def: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "overall_assessment": "You engaged with the scenario and kept the conversation going.",
            "what_went_well": ["Showed up and practiced", "Maintained composure"],
            "areas_to_improve": ["Be more specific in your responses", "Address objections more directly"],
            "key_moments": ["The opening exchange showed confidence"],
            "one_thing_to_practice": "Prepare 2-3 concrete examples for the most likely questions.",
            "confidence_score": 6,
        }

    # -----------------------------------------------------------------------
    # Redis session storage
    # -----------------------------------------------------------------------

    async def _save_session(self, session_id: str, session: Dict[str, Any]) -> None:
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            await r.set(f"roleplay:{session_id}", json.dumps(session), ex=ROLEPLAY_TTL)
        except Exception as e:
            logger.warning(f"RolePlayAgent: save_session failed: {e}")

    async def _load_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            raw = await r.get(f"roleplay:{session_id}")
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.warning(f"RolePlayAgent: load_session failed: {e}")
        return None
