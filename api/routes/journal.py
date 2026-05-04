"""
Journal API Routes

Endpoints:
  GET  /api/journal/prompt    — get today's Aura-generated journal prompt
  POST /api/journal/entry     — submit a journal entry, get Aura's reflection
  GET  /api/journal/entries   — list past entries (newest first, paginated)

Storage: uses aura_conversations table with role='journal_prompt' / role='journal_entry'
to avoid schema changes.
"""

import json
import logging
import uuid
from datetime import datetime, timezone, date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.middleware import get_current_user_id
from core.database import fetchrow, fetch, execute
from aura.brain import get_brain
from aura.user_model import load_user_model

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/journal", tags=["journal"])

# ─── Prompt generation ────────────────────────────────────────────────────────

FALLBACK_PROMPTS = [
    "What's one thing that happened recently that you haven't fully processed yet?",
    "If your future self could give you one piece of advice right now, what would it be?",
    "What's something you've been avoiding, and why?",
    "Describe a moment this week when you felt most like yourself.",
    "What would you do differently if you knew you couldn't fail?",
    "What emotion have you been carrying the longest? Where did it start?",
    "What are you most grateful for today, and why does it matter?",
    "What's one habit you'd like to build, and what's the first tiny step?",
    "Who has positively influenced your life recently, and have you told them?",
    "What story are you telling yourself that might not be true?",
]

import random


async def _generate_prompt(user_context: Dict[str, Any], openai_client=None) -> str:
    """Generate a personalised journal prompt using Aura."""
    if not openai_client:
        return random.choice(FALLBACK_PROMPTS)

    try:
        goals = user_context.get("goals_text", "")
        domain = user_context.get("preferred_domain", "")
        mood_index = user_context.get("session_mood")
        mood_note = ""
        if mood_index is not None:
            moods = ["exhausted", "neutral", "okay", "good", "energised"]
            mood_note = f"The user is currently feeling {moods[int(mood_index)]}."

        system = (
            "You are Aura, a wise and caring personal intelligence. "
            "Generate a single, thoughtful journal prompt for the user. "
            "It should be introspective, specific (not generic), and 1-2 sentences max. "
            "No preamble — just the question."
        )
        user_msg = (
            f"User goals: {goals or 'not set'}. "
            f"Focus domain: {domain or 'general'}. "
            f"{mood_note} "
            "Write one powerful journal question for today."
        )

        resp = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=80,
            temperature=0.9,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"Prompt generation failed: {e}")
        return random.choice(FALLBACK_PROMPTS)


async def _generate_reflection(
    prompt: str, response: str, openai_client=None
) -> str:
    """Generate Aura's reflection on the user's journal entry."""
    if not openai_client:
        return "Thank you for sharing that. Sitting with what you've written is itself a form of growth. ✦"

    try:
        system = (
            "You are Aura, a wise and empathetic guide. "
            "The user has just written a journal entry in response to a prompt. "
            "Write a short, warm, insightful reflection (2-3 sentences). "
            "Don't just repeat what they said — add depth, a gentle insight, or an affirmation. "
            "Be personal, not generic."
        )
        user_msg = f"Prompt: {prompt}\n\nUser's response: {response}"

        resp = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=120,
            temperature=0.8,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"Reflection generation failed: {e}")
        return "What you've shared shows real self-awareness. Keep honouring that voice. ✦"


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/prompt")
async def get_journal_prompt(
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """
    Get today's journal prompt. Returns existing one if already generated today.
    """
    today = date.today().isoformat()

    # Check if we already generated a prompt today
    row = await fetchrow(
        """
        SELECT id, message, context FROM aura_conversations
        WHERE user_id = $1
          AND role = 'journal_prompt'
          AND context::jsonb->>'date' = $2
        ORDER BY created_at DESC
        LIMIT 1
        """,
        uuid.UUID(user_id), today
    )

    if row:
        return {"id": str(row["id"]), "prompt": row["message"]}

    # Generate a new prompt
    brain = get_brain()
    user_model = await load_user_model(user_id)
    user_context = user_model.to_context_dict() if user_model else {}

    prompt_text = await _generate_prompt(user_context, brain._openai)
    prompt_id = uuid.uuid4()

    await execute(
        """
        INSERT INTO aura_conversations (id, user_id, role, message, context)
        VALUES ($1, $2, 'journal_prompt', $3, $4)
        """,
        prompt_id,
        uuid.UUID(user_id),
        prompt_text,
        json.dumps({"date": today}),
    )

    return {"id": str(prompt_id), "prompt": prompt_text}


class JournalEntryRequest(BaseModel):
    prompt_id: str
    response: str


@router.post("/entry")
async def submit_journal_entry(
    body: JournalEntryRequest,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """
    Submit a journal entry. Aura writes a reflection and stores everything.
    """
    # Fetch the prompt
    prompt_row = await fetchrow(
        "SELECT message FROM aura_conversations WHERE id = $1 AND user_id = $2",
        uuid.UUID(body.prompt_id), uuid.UUID(user_id)
    )
    prompt_text = prompt_row["message"] if prompt_row else "Today's reflection"

    brain = get_brain()
    reflection = await _generate_reflection(prompt_text, body.response, brain._openai)

    entry_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    # Store the user entry
    await execute(
        """
        INSERT INTO aura_conversations (id, user_id, role, message, context)
        VALUES ($1, $2, 'journal_entry', $3, $4)
        """,
        entry_id,
        uuid.UUID(user_id),
        body.response,
        json.dumps({
            "prompt_id": body.prompt_id,
            "prompt_text": prompt_text,
            "aura_reflection": reflection,
            "created_at": now.isoformat(),
        }),
    )

    return {"aura_reflection": reflection, "entry_id": str(entry_id)}


@router.get("/entries")
async def get_journal_entries(
    user_id: str = Depends(get_current_user_id),
    limit: int = 20,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """
    List past journal entries, newest first.
    """
    rows = await fetch(
        """
        SELECT id, message, context, created_at
        FROM aura_conversations
        WHERE user_id = $1 AND role = 'journal_entry'
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
        """,
        uuid.UUID(user_id), limit, offset
    )

    results = []
    for row in rows:
        meta = {}
        try:
            raw_context = row["context"]
            if isinstance(raw_context, str):
                meta = json.loads(raw_context)
            elif isinstance(raw_context, dict):
                meta = raw_context
        except Exception:
            pass

        results.append({
            "id": str(row["id"]),
            "prompt": meta.get("prompt_text", ""),
            "response": row["message"],
            "aura_reflection": meta.get("aura_reflection", ""),
            "created_at": row["created_at"].isoformat() if row["created_at"] else "",
        })

    return results
