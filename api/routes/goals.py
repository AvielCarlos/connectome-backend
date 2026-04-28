"""
Goals API Routes
Full CRUD for user goals with step management.
Includes AI-powered goal breakdown and per-step coaching via Ora.
"""

import logging
import json
import re
import uuid
from typing import List, Optional, Dict, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Body

from core.models import GoalCreate, GoalUpdate, GoalOut
from core.database import fetchrow, fetch, execute
from api.middleware import get_current_user_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/goals", tags=["goals"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_openai():
    """Lazy OpenAI client — returns None if no key configured."""
    from core.config import settings
    if not settings.has_openai:
        return None
    try:
        from openai import AsyncOpenAI
        return AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    except Exception as e:
        logger.warning(f"Could not init OpenAI: {e}")
        return None


def _generate_smart_mock_steps(title: str, description: Optional[str] = None) -> List[Dict[str, Any]]:
    """Generate goal-aware placeholder steps based on title keywords when OpenAI is unavailable."""
    lower = (title + " " + (description or "")).lower()

    if any(k in lower for k in ["fitness", "workout", "gym", "exercise", "run", "weight", "health", "diet"]):
        templates = [
            ("Schedule dedicated workout sessions",
             "Block specific days and times in your calendar. Consistency beats intensity — start with 3 days/week.",
             [{"label": "MyFitnessPal", "url": "https://www.myfitnesspal.com"}]),
            ("Set measurable fitness targets",
             "Define exact metrics: distance, weight lifted, reps, or body measurements. Vague goals stay dreams.",
             [{"label": "SMART Goals Guide", "url": "https://www.mindtools.com/pages/article/smart-goals.htm"}]),
            ("Track nutrition and hydration daily",
             "Log what you eat for 2 weeks — awareness alone changes behaviour. Aim for 2L water/day.",
             [{"label": "Cronometer", "url": "https://cronometer.com"}]),
            ("Find an accountability partner",
             "Tell someone your goal and schedule weekly check-ins. Social commitment is the strongest motivator.",
             []),
            ("Review and adjust weekly",
             "Every Sunday, review the week. What worked? What didn't? Adjust one variable.",
             []),
        ]
    elif any(k in lower for k in ["learn", "study", "course", "skill", "book", "read", "language", "code", "program", "develop"]):
        templates = [
            ("Define exactly what you want to learn",
             "Write a specific outcome: not 'learn Python' but 'build a working web scraper in Python'. Specificity is power.",
             [{"label": "Learning How to Learn (Coursera)", "url": "https://www.coursera.org/learn/learning-how-to-learn"}]),
            ("Gather your learning resources",
             "Find 2-3 quality sources (book, course, mentor). More than that is procrastination disguised as research.",
             [{"label": "Goodreads", "url": "https://www.goodreads.com"}]),
            ("Build a daily study schedule",
             "Block 30-90 minutes at the same time every day. The habit triggers the brain to enter learning mode.",
             [{"label": "Anki (spaced repetition)", "url": "https://apps.ankiweb.net"}]),
            ("Apply knowledge to a real project",
             "You learn 5x faster by doing. Within week 1, build or write something real — even if it's rough.",
             []),
            ("Test yourself and track retention",
             "Quiz yourself weekly without notes. Active recall is the most evidence-backed learning technique.",
             []),
        ]
    elif any(k in lower for k in ["start", "business", "launch", "product", "startup", "entrepreneur", "company", "brand"]):
        templates = [
            ("Validate the idea with real people",
             "Talk to 5 potential customers before writing a line of code or spending a dollar. Listen more than you pitch.",
             [{"label": "The Mom Test (book)", "url": "https://www.momtestbook.com"}]),
            ("Define your Minimum Viable Product",
             "The smallest thing that delivers value and generates feedback. Cut everything that doesn't prove the core hypothesis.",
             [{"label": "Lean Startup Principles", "url": "https://theleanstartup.com/principles"}]),
            ("Set up a landing page and waitlist",
             "A simple page with one CTA captures demand before you build. Use Carrd, Framer, or Webflow.",
             [{"label": "Carrd", "url": "https://carrd.co"}]),
            ("Define your first revenue milestone",
             "What does success look like in 90 days? Even $1 of real revenue teaches more than months of planning.",
             []),
            ("Ship version 1 and collect feedback",
             "Done is better than perfect. Release early, talk to every user, and iterate weekly.",
             [{"label": "Product Hunt", "url": "https://www.producthunt.com"}]),
        ]
    elif any(k in lower for k in ["save", "invest", "money", "financial", "debt", "budget", "retire", "wealth"]):
        templates = [
            ("Audit your current financial picture",
             "List every income source, fixed expense, variable expense, and debt. You can't fix what you haven't faced.",
             [{"label": "Mint", "url": "https://mint.intuit.com"}]),
            ("Create a zero-based monthly budget",
             "Give every dollar a job. Income minus expenses should equal zero (with savings as an expense).",
             [{"label": "YNAB (You Need A Budget)", "url": "https://www.youneedabudget.com"}]),
            ("Automate savings before you spend",
             "Set up automatic transfer on payday. Pay yourself first — willpower is finite, automation is not.",
             []),
            ("Eliminate or reduce one major expense",
             "Find your biggest discretionary spend and cut it by 50% for 30 days. Notice how little you miss it.",
             []),
            ("Review financial metrics monthly",
             "Net worth, savings rate, debt paydown. Track these numbers like a business — because your life is one.",
             [{"label": "Personal Capital", "url": "https://www.personalcapital.com"}]),
        ]
    elif any(k in lower for k in ["write", "blog", "content", "creative", "art", "music", "draw", "paint", "novel", "podcast"]):
        templates = [
            ("Define your creative vision in writing",
             "Write one paragraph: what you're making, who it's for, and why it matters. Post it somewhere visible.",
             []),
            ("Set a non-negotiable daily creation habit",
             "Choose a time and minimum output (500 words, 30 min practice, 1 sketch). Show up even when uninspired.",
             [{"label": "The War of Art (book)", "url": "https://www.goodreads.com/book/show/1319.The_War_of_Art"}]),
            ("Complete a rough first draft or prototype",
             "Quantity breeds quality in creative work. Finish before you polish — the inner critic can't edit a blank page.",
             []),
            ("Share with 3 people and gather real feedback",
             "Choose people who will be honest, not kind. Ask specific questions: What's confusing? What's missing?",
             []),
            ("Publish or perform — make it real",
             "Creative work that stays in a drawer doesn't count. Ship it, perform it, post it. The world needs to react.",
             []),
        ]
    else:
        # Generic — extract meaningful words from title
        words = [w for w in re.findall(r'\b[a-zA-Z]{4,}\b', title) if w.lower() not in
                 {"want", "need", "will", "make", "have", "that", "this", "with", "from", "into", "more", "some"}]
        keyword = words[0].lower() if words else "goal"
        templates = [
            (f"Write down exactly what '{title}' means to you",
             "Clarity is everything. In one paragraph, define success so specifically that someone else could measure it.",
             []),
            ("Research what others have done to achieve this",
             "Find 3 people who've done it. Read their stories. Extract the pattern — then adapt it to your situation.",
             [{"label": "Reddit communities", "url": f"https://www.reddit.com/search/?q={keyword.replace(' ', '+')}"}]),
            ("Create your 30-day action plan",
             "Break the goal into weekly milestones. Each week should end with something tangible you can point to.",
             []),
            ("Take your first concrete action today",
             "Not tomorrow. The gap between intention and action is where goals die. Do something in the next 2 hours.",
             []),
            ("Set a weekly review ritual",
             "Every week, answer: What moved forward? What blocked me? What's the one priority for next week?",
             []),
        ]

    return [
        {
            "id": str(uuid.uuid4()),
            "text": t[0],
            "detail": t[1],
            "resources": t[2] if len(t) > 2 else [],
            "completed": False,
            "order": i,
            "ora_note": None,
        }
        for i, t in enumerate(templates)
    ]


async def _ai_breakdown(title: str, description: Optional[str], openai_client) -> Optional[List[Dict[str, Any]]]:
    """Call OpenAI to generate structured steps. Returns None on failure."""
    prompt = f"""You are Ora, an AI coach. Break this goal into 5-8 specific, actionable sub-steps.
Goal: "{title}"
Description: "{description or 'No description provided'}"

Return JSON: {{
  "steps": [
    {{
      "id": "uuid-string",
      "text": "step text",
      "detail": "1-2 sentences why this step matters and how to do it",
      "resources": [{{"label": "resource name", "url": "https://..."}}],
      "completed": false,
      "order": 0,
      "ora_note": null
    }}
  ]
}}

Rules:
- Steps must be concrete and actionable (not "think about X", but "write down X")
- Each step should be completable in 1-3 hours
- Resources should be real URLs (articles, tools, apps) that help with that specific step
- Order matters — earlier steps unlock later ones
- Return ONLY valid JSON, no markdown"""

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=1200,
            response_format={"type": "json_object"},
        )
        data = json.loads(response.choices[0].message.content)
        raw_steps = data.get("steps", [])
        # Ensure required fields
        steps = []
        for i, s in enumerate(raw_steps):
            steps.append({
                "id": s.get("id") or str(uuid.uuid4()),
                "text": s.get("text", f"Step {i+1}"),
                "detail": s.get("detail"),
                "resources": s.get("resources") or [],
                "completed": False,
                "order": s.get("order", i),
                "ora_note": s.get("ora_note"),
            })
        return steps
    except Exception as e:
        logger.warning(f"OpenAI breakdown failed: {e}")
        return None


async def _get_breakdown_steps(title: str, description: Optional[str]) -> List[Dict[str, Any]]:
    """Get AI steps or fall back to smart mock steps."""
    openai_client = _get_openai()
    if openai_client:
        steps = await _ai_breakdown(title, description, openai_client)
        if steps:
            return steps
    return _generate_smart_mock_steps(title, description)


def _build_goal_out(row) -> GoalOut:
    """Build a GoalOut from a DB row, handling JSON steps."""
    raw_steps = row["steps"]
    if isinstance(raw_steps, str):
        raw_steps = json.loads(raw_steps)
    return GoalOut(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        status=row["status"],
        steps=raw_steps or [],
        progress=row["progress"] or 0.0,
        created_at=row["created_at"],
        domain=row["domain"] if "domain" in row.keys() else None,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/", response_model=List[GoalOut])
async def list_goals(
    status_filter: str = "active",
    user_id: str = Depends(get_current_user_id),
):
    """List user's goals, optionally filtered by status."""
    if status_filter == "all":
        rows = await fetch(
            "SELECT * FROM goals WHERE user_id = $1 ORDER BY created_at DESC",
            str(user_id),
        )
    else:
        rows = await fetch(
            "SELECT * FROM goals WHERE user_id = $1 AND status = $2 ORDER BY created_at DESC",
            str(user_id),
            status_filter,
        )
    return [_build_goal_out(r) for r in rows]


@router.post("/", response_model=GoalOut, status_code=201)
async def create_goal(
    body: GoalCreate,
    user_id: str = Depends(get_current_user_id),
):
    """Create a new goal. Automatically generates AI-powered steps via Ora."""
    # Generate steps (AI or smart mock)
    if body.steps:
        steps = [s.model_dump() for s in body.steps]
    else:
        steps = await _get_breakdown_steps(body.title, body.description)

    row = await fetchrow(
        """
        INSERT INTO goals (user_id, title, description, steps, status, progress, domain)
        VALUES ($1, $2, $3, $4, 'active', 0.0, $5)
        RETURNING *
        """,
        str(user_id),
        body.title,
        body.description,
        json.dumps(steps),
        body.domain or "iVive",
    )

    # Invalidate user model cache
    from core.redis_client import redis_delete
    await redis_delete(f"user_model:{user_id}")

    logger.info(f"Goal created for user {user_id[:8]}: {body.title} ({len(steps)} steps)")
    return _build_goal_out(row)


@router.get("/{goal_id}", response_model=GoalOut)
async def get_goal(
    goal_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Get a specific goal."""
    row = await fetchrow(
        "SELECT * FROM goals WHERE id = $1 AND user_id = $2",
        UUID(goal_id),
        str(user_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Goal not found")
    return _build_goal_out(row)


@router.patch("/{goal_id}", response_model=GoalOut)
async def update_goal(
    goal_id: str,
    body: GoalUpdate,
    user_id: str = Depends(get_current_user_id),
):
    """Update a goal — title, status, steps, or progress."""
    row = await fetchrow(
        "SELECT * FROM goals WHERE id = $1 AND user_id = $2",
        UUID(goal_id),
        str(user_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Goal not found")

    new_title = body.title if body.title is not None else row["title"]
    new_desc = body.description if body.description is not None else row["description"]
    new_status = body.status if body.status is not None else row["status"]
    new_domain = body.domain if body.domain is not None else (row["domain"] if "domain" in row.keys() else "iVive")

    if body.steps is not None:
        new_steps = json.dumps([s.model_dump() for s in body.steps])
        completed = sum(1 for s in body.steps if s.completed)
        total = len(body.steps)
        new_progress = (completed / total) if total > 0 else 0.0
    else:
        raw = row["steps"]
        new_steps = json.dumps(raw if isinstance(raw, list) else (json.loads(raw) if raw else []))
        new_progress = body.progress if body.progress is not None else (row["progress"] or 0.0)

    updated = await fetchrow(
        """
        UPDATE goals
        SET title = $1, description = $2, status = $3, steps = $4, progress = $5, domain = $6
        WHERE id = $7
        RETURNING *
        """,
        new_title, new_desc, new_status, new_steps, new_progress, new_domain, UUID(goal_id),
    )

    from core.redis_client import redis_delete
    await redis_delete(f"user_model:{user_id}")

    return _build_goal_out(updated)


@router.delete("/{goal_id}", status_code=204)
async def delete_goal(
    goal_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Delete a goal."""
    row = await fetchrow(
        "SELECT id FROM goals WHERE id = $1 AND user_id = $2",
        UUID(goal_id),
        str(user_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Goal not found")
    await execute("DELETE FROM goals WHERE id = $1", UUID(goal_id))
    from core.redis_client import redis_delete
    await redis_delete(f"user_model:{user_id}")


@router.post("/{goal_id}/complete")
async def complete_goal(
    goal_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """
    Mark a goal as completed.
    - Sets status to 'completed', progress to 1.0, records completed_at.
    - Triggers Ora's celebration message.
    - CoachingAgent suggests a next goal if the user has other active goals
      that are at 80%+ progress.

    Returns: { goal, ora_celebration, next_goal_suggestion }
    """
    row = await fetchrow(
        "SELECT * FROM goals WHERE id = $1 AND user_id = $2",
        UUID(goal_id), str(user_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Goal not found")

    if row["status"] == "completed":
        return {
            "goal": _build_goal_out(row),
            "ora_celebration": "You already completed this goal! Keep going.",
            "next_goal_suggestion": None,
        }

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    updated = await fetchrow(
        """
        UPDATE goals
        SET status = 'completed', progress = 1.0, completed_at = $2, celebration_sent = TRUE
        WHERE id = $1
        RETURNING *
        """,
        UUID(goal_id), now,
    )

    # Generate celebration message
    ora_celebration = await _generate_celebration(row["title"], user_id)

    # Check if coaching agent should suggest the next goal
    next_goal_suggestion = await _suggest_next_goal(user_id, goal_id)

    from core.redis_client import redis_delete
    await redis_delete(f"user_model:{user_id}")

    logger.info(f"Goal completed for user {user_id[:8]}: {row['title']}")

    return {
        "goal": _build_goal_out(updated),
        "ora_celebration": ora_celebration,
        "next_goal_suggestion": next_goal_suggestion,
    }


async def _generate_celebration(goal_title: str, user_id: str) -> str:
    """Ora's celebration message for completing a goal."""
    openai_client = _get_openai()
    if openai_client:
        try:
            prompt = f"""You are Ora, a direct and warm AI coach. Someone just completed their goal: "{goal_title}".

Write a short celebration message (2-3 sentences). Be specific to the goal title. Be genuine, warm, and forward-looking.
Avoid generic phrases. Start with what they achieved, then nudge toward momentum.
Return only the message text, no quotes."""
            response = await openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.8,
                max_tokens=150,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.warning(f"Celebration LLM failed: {e}")

    # Mock celebration
    return (
        f"You completed '{goal_title}' — that's real. "
        f"Most people set goals and never finish them. You did. "
        f"What's next?"
    )


async def _suggest_next_goal(user_id: str, completed_goal_id: str) -> Optional[str]:
    """
    If the user has another goal at 80%+ progress, prompt them to focus on it.
    If they have no active goals, suggest creating a new one.
    """
    try:
        active_rows = await fetch(
            """
            SELECT id, title, progress FROM goals
            WHERE user_id = $1 AND status = 'active' AND id != $2
            ORDER BY progress DESC
            LIMIT 5
            """,
            str(user_id), UUID(completed_goal_id),
        )

        if not active_rows:
            return "You've cleared your active goals. What's the next big thing you want to build?"

        # Check for goal at 80%+
        for row in active_rows:
            if (row["progress"] or 0.0) >= 0.8:
                return (
                    f"You're {round((row['progress'] or 0.0) * 100)}% done with '{row['title']}' — "
                    f"you're so close. Keep the momentum going."
                )

        # Suggest the highest-progress active goal
        top = active_rows[0]
        pct = round((top["progress"] or 0.0) * 100)
        return (
            f"Your next goal to tackle: '{top['title']}' ({pct}% done). "
            f"You're already started — finish what you began."
        )
    except Exception as e:
        logger.warning(f"_suggest_next_goal failed: {e}")
        return None


@router.post("/{goal_id}/breakdown", response_model=GoalOut)
async def breakdown_goal(
    goal_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Ask Ora to (re)generate AI-powered steps for an existing goal."""
    row = await fetchrow(
        "SELECT * FROM goals WHERE id = $1 AND user_id = $2",
        UUID(goal_id),
        str(user_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Goal not found")

    steps = await _get_breakdown_steps(row["title"], row["description"])

    updated = await fetchrow(
        "UPDATE goals SET steps = $1, progress = 0.0 WHERE id = $2 RETURNING *",
        json.dumps(steps),
        UUID(goal_id),
    )

    from core.redis_client import redis_delete
    await redis_delete(f"user_model:{user_id}")

    logger.info(f"Goal breakdown for user {user_id[:8]}: {row['title']} ({len(steps)} steps)")
    return _build_goal_out(updated)


@router.post("/{goal_id}/steps/{step_index}/ask")
async def ask_about_step(
    goal_id: str,
    step_index: int,
    user_id: str = Depends(get_current_user_id),
    body: Dict[str, Any] = Body(default={}),
):
    """Ask Ora for help with a specific goal step."""
    row = await fetchrow(
        "SELECT * FROM goals WHERE id = $1 AND user_id = $2",
        UUID(goal_id),
        str(user_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Goal not found")

    raw_steps = row["steps"]
    if isinstance(raw_steps, str):
        raw_steps = json.loads(raw_steps)
    steps = raw_steps or []

    if step_index < 0 or step_index >= len(steps):
        raise HTTPException(status_code=404, detail="Step not found")

    step = steps[step_index]
    question = (body.get("question") or "").strip() or "Give me practical advice on how to complete this step"

    openai_client = _get_openai()
    if openai_client:
        prompt = f"""You are Ora, an AI coach helping someone with a specific goal step.
Goal: "{row['title']}"
Step: "{step.get('text', '')}"
Step detail: "{step.get('detail', 'No detail provided')}"
User question: "{question}"

Respond in 2-4 sentences with specific, actionable advice. If relevant, mention 1 specific resource (name + URL). Be warm but direct.
Return JSON: {{"reply": "...", "resource": {{"label": "...", "url": "..."}} or null}}"""

        try:
            response = await openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=300,
                response_format={"type": "json_object"},
            )
            data = json.loads(response.choices[0].message.content)
            return {
                "reply": data.get("reply", ""),
                "resource": data.get("resource"),
            }
        except Exception as e:
            logger.warning(f"OpenAI step-ask failed: {e}")

    # Fallback mock response
    step_text = step.get("text", "this step")
    step_detail = step.get("detail", "")
    resources = step.get("resources", [])
    resource = resources[0] if resources else None
    reply = (
        f"For '{step_text}': {step_detail} "
        f"The key is to start small and concrete — pick the one action you can do right now, even if it takes just 10 minutes. "
        f"Momentum is everything."
    ).strip()

    return {"reply": reply, "resource": resource}
