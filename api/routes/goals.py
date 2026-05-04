"""
Goals API Routes
Full CRUD for user goals with step management.
Includes AI-powered goal breakdown and per-step coaching via Aura.
"""

import logging
import json
import re
import uuid
from typing import List, Optional, Dict, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Body
from pydantic import BaseModel, Field

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




async def _drive_goal_context(user_id: str, goal_title: str, conversation: List[Dict[str, Any]]) -> tuple[str, List[Dict[str, Any]]]:
    """Fetch user-scoped Drive excerpts to ground goal clarification and IOO routing.

    Privacy: DriveAgentV2 enforces owner_user_id/user_id scoping and respects
    the user's Drive privacy level. We only pass short excerpts into the prompt.
    """
    query_parts = [goal_title]
    for turn in conversation[-6:]:
        text = (turn.get("content") or "").strip()
        if text:
            query_parts.append(text[:300])
    query = " ".join(query_parts).strip() or "goals values projects"
    try:
        from aura.agents.drive_agent_v2 import DriveAgentV2
        from aura.brain import get_brain

        brain = get_brain()
        agent = DriveAgentV2(openai_client=getattr(brain, "_openai", None))
        hits = await agent.semantic_search(
            query=query,
            user_id=str(user_id),
            limit=4,
            min_similarity=0.68,
        )
    except Exception as e:
        logger.debug(f"Drive goal context skipped: {e}")
        hits = []

    if not hits:
        return "", []

    lines = []
    safe_hits = []
    for h in hits[:4]:
        name = str(h.get("name") or "Drive note")[:120]
        excerpt = str(h.get("excerpt") or "").replace("\n", " ")[:500]
        if not excerpt:
            continue
        lines.append(f"- {name}: {excerpt}")
        safe_hits.append({
            "drive_id": h.get("drive_id"),
            "name": name,
            "excerpt": excerpt[:220],
            "similarity": h.get("similarity"),
        })

    if not lines:
        return "", []

    context = (
        "Relevant user-owned Google Drive notes. Use these as personal grounding "
        "for values, past projects, blockers, and long-standing intentions. Do not "
        "quote sensitive details unless necessary; never imply access beyond connected Drive.\n"
        + "\n".join(lines)
    )
    return context[:2400], safe_hits


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
            "aura_note": None,
        }
        for i, t in enumerate(templates)
    ]


async def _ai_breakdown(title: str, description: Optional[str], openai_client) -> Optional[List[Dict[str, Any]]]:
    """Call OpenAI to generate structured steps. Returns None on failure."""
    prompt = f"""You are Aura, an AI coach. Break this goal into 5-8 specific, actionable sub-steps.
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
      "aura_note": null
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
                "aura_note": s.get("aura_note"),
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
    raw_graph_metadata = row["graph_metadata"] if "graph_metadata" in row.keys() else {}
    if isinstance(raw_graph_metadata, str):
        try:
            raw_graph_metadata = json.loads(raw_graph_metadata)
        except Exception:
            raw_graph_metadata = {}
    return GoalOut(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        status=row["status"],
        steps=raw_steps or [],
        progress=row["progress"] or 0.0,
        created_at=row["created_at"],
        domain=row["domain"] if "domain" in row.keys() else None,
        intention_text=row["intention_text"] if "intention_text" in row.keys() else None,
        measurable_outcome=row["measurable_outcome"] if "measurable_outcome" in row.keys() else None,
        success_metric=row["success_metric"] if "success_metric" in row.keys() else None,
        target_value=row["target_value"] if "target_value" in row.keys() else None,
        target_date=row["target_date"] if "target_date" in row.keys() else None,
        graph_metadata=raw_graph_metadata or {},
    )


async def _mirror_goal_state_vector(user_id: str, goal_row) -> None:
    """Mirror goal current/desired-state signals into IOO user state.

    Goals are the user's declared desired-state interface. Keeping a compact
    vector signal in ioo_user_state lets Aura/iDo route Now/Future/Map/Execute
    recommendations against the gap between current state and desired state.
    """
    try:
        metadata = goal_row["graph_metadata"] if "graph_metadata" in goal_row.keys() else {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata) if metadata else {}
        steps = goal_row["steps"] if "steps" in goal_row.keys() else []
        if isinstance(steps, str):
            steps = json.loads(steps) if steps else []
        current_state = metadata.get("current_state") or metadata.get("current_state_text") or "declared_intention"
        desired_state = metadata.get("desired_state") or metadata.get("desired_state_text") or (goal_row["measurable_outcome"] if "measurable_outcome" in goal_row.keys() else None) or goal_row["title"]
        completed_steps = sum(1 for step in (steps or []) if step.get("completed"))
        total_steps = len(steps or [])
        signal = {
            "goal_id": str(goal_row["id"]),
            "title": goal_row["title"],
            "domain": goal_row["domain"] if "domain" in goal_row.keys() else None,
            "current_state": current_state,
            "desired_state": desired_state,
            "intention_text": goal_row["intention_text"] if "intention_text" in goal_row.keys() else goal_row["title"],
            "measurable_outcome": goal_row["measurable_outcome"] if "measurable_outcome" in goal_row.keys() else goal_row["title"],
            "success_metric": goal_row["success_metric"] if "success_metric" in goal_row.keys() else None,
            "target_value": goal_row["target_value"] if "target_value" in goal_row.keys() else None,
            "target_date": goal_row["target_date"] if "target_date" in goal_row.keys() else None,
            "progress": float(goal_row["progress"] or 0.0),
            "completed_steps": completed_steps,
            "total_steps": total_steps,
            "gap_summary": metadata.get("gap_summary") or f"Move from {current_state} toward {desired_state}",
        }
        await execute(
            """
            INSERT INTO ioo_user_state (user_id, state_json, last_updated)
            VALUES ($1, $2::jsonb, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                state_json = COALESCE(ioo_user_state.state_json, '{}'::jsonb) || EXCLUDED.state_json,
                last_updated = NOW()
            """,
            UUID(user_id),
            json.dumps({
                "desired_state_vector": signal,
                "latest_goal_vector_event": "goal_state_updated",
            }),
        )
    except Exception as err:
        logger.warning("Could not mirror goal state vector for user %s: %s", user_id[:8], err)


# ---------------------------------------------------------------------------
# Clarification models/helpers
# ---------------------------------------------------------------------------

class GoalClarifyRequest(BaseModel):
    goal_title: str = Field(..., min_length=1)
    conversation: List[Dict[str, str]] = []
    user_profile: Dict[str, Any] = {}


class GoalClarifyResponse(BaseModel):
    message: str
    is_complete: bool
    structured_goal: Dict[str, Any] = {}
    suggested_ioo_path: List[Dict[str, Any]] = []


def _extract_json_object(content: str) -> Optional[Dict[str, Any]]:
    """Extract the first JSON object from a model response."""
    try:
        return json.loads(content)
    except Exception:
        pass

    match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL)
    candidate = match.group(1) if match else None
    if candidate is None:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        candidate = match.group() if match else None
    if candidate is None:
        return None
    try:
        return json.loads(candidate)
    except Exception:
        return None


def _fallback_structured_goal(title: str, conversation: List[Dict[str, str]]) -> Dict[str, Any]:
    user_bits = [m.get("content", "") for m in conversation if m.get("role") == "user"]
    specifics = " ".join(user_bits).strip() or "Clarified through conversation with Aura."
    return {
        "title": title,
        "why": "To create meaningful momentum toward this goal.",
        "specifics": specifics[:500],
        "measurable_outcome": specifics[:240] or title,
        "success_metric": "A clearly observable real-world result, confirmed by the user.",
        "timeline": "Next 30-90 days",
        "constraints": "As discussed",
        "difficulty_estimate": 5,
    }


def _fallback_clarify_question(goal_title: str, turn_count: int) -> str:
    questions = [
        f"What would make {goal_title} feel genuinely achieved in real life — what would be different, visible, or measurable?",
        "What constraints should I design around first: time, money, location, energy, skills, confidence, or access?",
        "Which parts do you want to do yourself, and which parts would you happily let Aura handle if the cost was covered?",
        "What timeline feels ambitious but still attainable?",
    ]
    return questions[min(turn_count, len(questions) - 1)]


ORA_SERVICE_PRICE_BANDS = {
    "aura-goal-path-map": (9, 79),
    "aura-opportunity-scout": (19, 149),
    "aura-delegated-action-pack": (39, 399),
}


def _goal_complexity(structured_goal: Dict[str, Any]) -> Dict[str, Any]:
    """Estimate how extensive the goal gap is from present state to desired state."""
    raw_difficulty = structured_goal.get("difficulty_estimate") or structured_goal.get("difficulty") or 5
    try:
        difficulty = max(1, min(10, int(float(raw_difficulty))))
    except Exception:
        difficulty = 5

    text = " ".join(str(structured_goal.get(k) or "") for k in ["specifics", "constraints", "timeline", "measurable_outcome"])
    lower = text.lower()
    gap = 1
    if any(k in lower for k in ["no money", "broke", "debt", "can't", "cannot", "no experience", "beginner", "visa", "move", "relocate"]):
        gap += 2
    if any(k in lower for k in ["global", "business", "company", "career", "income", "dating", "travel", "health", "legal", "medical"]):
        gap += 1
    if any(k in lower for k in ["week", "urgent", "asap", "immediately", "today"]):
        gap += 1
    if any(k in lower for k in ["year", "years", "massive", "million", "unicorn", "life"]):
        gap += 2
    gap = max(1, min(5, gap))
    level = "light" if difficulty <= 3 and gap <= 2 else "standard" if difficulty <= 7 and gap <= 3 else "deep"
    return {"difficulty": difficulty, "present_state_gap": gap, "level": level}


def _quote_aura_service(service_id: str, structured_goal: Dict[str, Any], work_units: int = 1) -> Dict[str, Any]:
    """Dynamic quote based on goal extensivity, present-state gap, and Aura-side work."""
    min_price, max_price = ORA_SERVICE_PRICE_BANDS.get(service_id, (19, 199))
    base = {
        "aura-goal-path-map": 9,
        "aura-opportunity-scout": 19,
        "aura-delegated-action-pack": 39,
    }.get(service_id, min_price)
    complexity = _goal_complexity(structured_goal)
    multiplier = 1 + ((complexity["difficulty"] - 1) * 0.11) + ((complexity["present_state_gap"] - 1) * 0.22) + max(0, work_units - 1) * 0.18
    quoted = int(round(base * multiplier / 5) * 5)
    quoted = max(min_price, min(max_price, quoted))
    return {
        "price_usd": quoted,
        "pricing_level": complexity["level"],
        "difficulty": complexity["difficulty"],
        "present_state_gap": complexity["present_state_gap"],
        "pricing_note": f"Quoted from goal complexity ({complexity['level']}), present-state gap, urgency, and Aura-side work; covers costs plus growth margin.",
    }


def _fallback_execution_path(title: str, structured_goal: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Goal-aware IOO execution map when vector search has no strong nodes yet."""
    goal = structured_goal.get("measurable_outcome") or structured_goal.get("specifics") or structured_goal.get("title") or title
    timeline = structured_goal.get("timeline") or "30-90 days"
    constraints = structured_goal.get("constraints") or "unknown constraints"
    map_quote = _quote_aura_service("aura-goal-path-map", structured_goal, 1)
    scout_quote = _quote_aura_service("aura-opportunity-scout", structured_goal, 2)
    delegate_quote = _quote_aura_service("aura-delegated-action-pack", structured_goal, 3)
    return [
        {
            "id": f"clarify-{uuid.uuid4()}",
            "title": "Set the attainable target",
            "description": f"Turn the intention into one measurable outcome for {timeline}: {goal}",
            "domain": structured_goal.get("domain") or "iVive",
            "node_type": "clarification",
            "owner": "user",
            "aura_can_do": False,
            "user_action": "Confirm or edit the exact outcome, metric, and deadline.",
            "aura_action": "Aura narrows the intention into a goal that can be mapped and tracked.",
            "prerequisites": ["Honest desired outcome", "Realistic timeline"],
        },
        {
            "id": f"map-{uuid.uuid4()}",
            "title": "Map prerequisites and bridge nodes",
            "description": f"Identify what must be true before this goal is easy: skills, money, time, location, people, tools, and constraints ({constraints}).",
            "domain": structured_goal.get("domain") or "iVive",
            "node_type": "graph_mapping",
            "owner": "aura",
            "aura_can_do": True,
            "user_action": "Answer any missing constraint questions.",
            "aura_action": "Aura builds the IOO node map, prerequisite chain, and first executable route.",
            "service_id": "aura-goal-path-map",
            **map_quote,
            "requires_payment": True,
        },
        {
            "id": f"research-{uuid.uuid4()}",
            "title": "Find real options and opportunities",
            "description": "Search for concrete people, places, tools, offers, events, services, grants, jobs, communities, or resources connected to this path.",
            "domain": structured_goal.get("domain") or "Aventi",
            "node_type": "opportunity_search",
            "owner": "aura",
            "aura_can_do": True,
            "user_action": "Choose which opportunities feel aligned.",
            "aura_action": "Aura researches and ranks options, then turns the best ones into next nodes.",
            "service_id": "aura-opportunity-scout",
            **scout_quote,
            "requires_payment": True,
        },
        {
            "id": f"first-step-{uuid.uuid4()}",
            "title": "Take the first user-owned step",
            "description": "Do the smallest action that creates real evidence: send the message, book the slot, make the list, visit the place, publish the draft, or complete the prerequisite.",
            "domain": structured_goal.get("domain") or "iVive",
            "node_type": "physical_or_digital_step",
            "owner": "user",
            "aura_can_do": False,
            "user_action": "Complete the first concrete step and mark it done.",
            "aura_action": "Aura tracks completion and adapts the next node.",
        },
        {
            "id": f"delegate-{uuid.uuid4()}",
            "title": "Delegate execution support to Aura",
            "description": "When the path needs calls, drafting, booking, comparison, admin, setup, or deeper planning, Aura can execute the agentic work after payment unlocks the required tools/time.",
            "domain": structured_goal.get("domain") or "Eviva",
            "node_type": "delegated_execution",
            "owner": "aura",
            "aura_can_do": True,
            "user_action": "Approve the action and any external commitments before Aura executes.",
            "aura_action": "Aura performs the delegated digital work and reports back with outcomes.",
            "service_id": "aura-delegated-action-pack",
            **delegate_quote,
            "requires_payment": True,
        },
    ]


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


# Tier → path_limit mapping
TIER_PATH_LIMITS = {"free": 4, "explorer": 12, "sovereign": 999}


async def _get_effective_limit(user_id: str) -> int:
    """Return effective path limit for user based on tier + explicit path_limit column."""
    row = await fetchrow(
        "SELECT path_limit, subscription_tier FROM users WHERE id = $1", str(user_id)
    )
    if not row:
        return 4
    tier = row["subscription_tier"] or "free"
    tier_limit = TIER_PATH_LIMITS.get(tier, 4)
    explicit = row["path_limit"] or 4
    # Use whichever is higher — tier upgrade or manually set
    return max(tier_limit, explicit)


@router.get("/path-status")
async def get_path_status(user_id: str = Depends(get_current_user_id)):
    """Return the user's active path count, limit, and credits."""
    active_count = await fetchrow(
        "SELECT COUNT(*) AS cnt FROM goals WHERE user_id = $1 AND status = 'active'",
        str(user_id),
    )
    user_row = await fetchrow(
        "SELECT path_limit, path_credits, subscription_tier FROM users WHERE id = $1",
        str(user_id),
    )
    limit = await _get_effective_limit(user_id)
    credits = int(user_row["path_credits"]) if user_row and user_row["path_credits"] else 0
    count = int(active_count["cnt"]) if active_count else 0
    tier = (user_row["subscription_tier"] if user_row else None) or "free"
    return {
        "active_paths": count,
        "path_limit": limit,
        "path_credits": credits,
        "paths_remaining": max(0, limit - count),
        "at_limit": count >= limit and credits == 0,
        "can_use_credit": count >= limit and credits > 0,
        "subscription_tier": tier,
        "is_subscribed": tier not in ("free", None, ""),
    }


@router.post("/", response_model=GoalOut, status_code=201)
async def create_goal(
    body: GoalCreate,
    user_id: str = Depends(get_current_user_id),
):
    """Create a new goal. Automatically generates AI-powered steps via Aura."""
    # Enforce path limit gate
    active_count_row = await fetchrow(
        "SELECT COUNT(*) AS cnt FROM goals WHERE user_id = $1 AND status = 'active'",
        str(user_id),
    )
    user_row = await fetchrow(
        "SELECT path_credits, subscription_tier FROM users WHERE id = $1",
        str(user_id),
    )
    path_limit = await _get_effective_limit(user_id)
    credits = int(user_row["path_credits"]) if user_row and user_row["path_credits"] else 0
    active = int(active_count_row["cnt"]) if active_count_row else 0

    if active >= path_limit:
        if credits > 0:
            # Consume one credit to open an extra path
            await execute(
                "UPDATE users SET path_credits = path_credits - 1 WHERE id = $1 AND path_credits > 0",
                str(user_id),
            )
            logger.info(f"Path credit consumed for user {user_id[:8]} (active={active}, limit={path_limit})")
        else:
            raise HTTPException(
                status_code=402,
                detail={
                    "code": "path_limit_reached",
                    "active_paths": active,
                    "path_limit": path_limit,
                    "message": "You have reached your open path limit. Archive a path, buy credits, or subscribe.",
                },
            )

    # Generate steps (AI or smart mock)
    if body.steps:
        steps = [s.model_dump() for s in body.steps]
    else:
        steps = await _get_breakdown_steps(body.title, body.description)

    graph_metadata = body.graph_metadata or {}
    graph_metadata.setdefault("state_model", "intention_to_measurable_goal_to_steps")
    graph_metadata.setdefault("source", "goals_collection")
    graph_metadata.setdefault("intention_text", body.intention_text or body.title)
    graph_metadata.setdefault("measurable_outcome", body.measurable_outcome or body.title)

    row = await fetchrow(
        """
        INSERT INTO goals (
            user_id, title, description, steps, status, progress, domain,
            intention_text, measurable_outcome, success_metric, target_value,
            target_date, graph_metadata
        )
        VALUES ($1, $2, $3, $4, 'active', 0.0, $5, $6, $7, $8, $9, $10, $11)
        RETURNING *
        """,
        str(user_id),
        body.title,
        body.description,
        json.dumps(steps),
        body.domain or "iVive",
        body.intention_text or body.title,
        body.measurable_outcome or body.title,
        body.success_metric,
        body.target_value,
        body.target_date,
        json.dumps(graph_metadata),
    )

    # Invalidate user model cache
    from core.redis_client import redis_delete
    await redis_delete(f"user_model:{user_id}")
    await _mirror_goal_state_vector(user_id, row)

    logger.info(f"Goal created for user {user_id[:8]}: {body.title} ({len(steps)} steps)")
    return _build_goal_out(row)


@router.post("/clarify", response_model=GoalClarifyResponse)
async def clarify_goal(
    body: GoalClarifyRequest,
    user_id: str = Depends(get_current_user_id),
):
    """
    Interactive goal clarification conversation with Aura.
    Each call advances the conversation one step.
    After 3-5 exchanges, returns a structured goal + IOO path.
    """
    conversation = body.conversation or []
    turn_count = len([m for m in conversation if m.get("role") == "user"])

    system = """You are Aura, the interface to the IOO neural graph.
Your job is NOT to produce generic productivity steps. Your job is to clarify an intention into an attainable, measurable goal, then map the first graph connections.

Ask ONE focused clarifying question per turn. Be warm, concise, direct, and specific.
Clarify in this order:
1) What real-world outcome would prove the intention is achieved?
2) What constraints matter most: time, money, location, energy, skills, confidence, access, relationships?
3) Which parts should the user do themselves vs which parts would they pay Aura to handle?
4) What timeline/target is ambitious but attainable?

When you have enough information, respond with ONLY valid JSON:
{
  "complete": true,
  "structured_goal": {
    "title": "short goal title",
    "why": "why this matters",
    "specifics": "clear specific goal statement",
    "measurable_outcome": "observable outcome that proves success",
    "success_metric": "metric or proof of completion",
    "target_value": "number/threshold if known",
    "timeline": "attainable timeframe",
    "constraints": "important constraints",
    "difficulty_estimate": 1-10,
    "domain": "iVive|Eviva|Aventi"
  },
  "graph_nodes": [
    {
      "title": "node title",
      "description": "what this node unlocks",
      "node_type": "clarification|prerequisite|opportunity_search|physical_step|digital_step|delegated_execution",
      "owner": "user|aura",
      "user_action": "what the user does",
      "aura_action": "what Aura can do",
      "aura_can_do": true,
      "requires_payment": true,
      "service_id": "aura-goal-path-map|aura-opportunity-scout|aura-delegated-action-pack|null",
      "price_usd": 19,
      "pricing_level": "light|standard|deep",
      "pricing_note": "dynamic quote based on goal extensivity, present-state gap, urgency, and Aura-side work; covers model/search/tool costs plus growth margin",
      "prerequisites": ["node or condition"]
    }
  ]
}

Rules:
- The first node should usually be goal confirmation/measurement.
- Include both user-owned nodes and Aura-owned nodes.
- Paid Aura nodes are allowed only for work Aura can actually perform digitally/research/admin/planning; user approval is still required before external commitments.
- Do not promise guaranteed life outcomes, money, dating success, medical outcomes, or token rewards.
- Before complete, do not output JSON; just ask the next question."""

    drive_context, drive_hits = await _drive_goal_context(str(user_id), body.goal_title, conversation)

    messages = [{"role": "system", "content": system}]
    if drive_context:
        messages.append({"role": "system", "content": drive_context})
    if body.user_profile:
        messages.append({
            "role": "system",
            "content": "User profile context: " + json.dumps(body.user_profile)[:2000],
        })
    messages.append({"role": "user", "content": f"I want to: {body.goal_title}"})
    for turn in conversation[-10:]:
        role = "assistant" if turn.get("role") == "aura" else "user"
        content = (turn.get("content") or "").strip()
        if content:
            messages.append({"role": role, "content": content})

    content = ""
    client = _get_openai()
    if client:
        try:
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                temperature=0.7,
                max_tokens=900,
            )
            content = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logger.warning(f"Goal clarification LLM failed: {e}")

    if not content:
        content = _fallback_clarify_question(body.goal_title, turn_count)

    parsed = _extract_json_object(content)
    is_complete = bool(parsed and parsed.get("complete") is True) or turn_count >= 4
    structured_goal: Dict[str, Any] = {}
    suggested_path: List[Dict[str, Any]] = []

    if is_complete:
        if parsed:
            structured_goal = parsed.get("structured_goal") or {}
        if not structured_goal:
            structured_goal = _fallback_structured_goal(body.goal_title, conversation)
        structured_goal.setdefault("title", body.goal_title)
        structured_goal.setdefault("measurable_outcome", structured_goal.get("specifics") or body.goal_title)
        structured_goal.setdefault("success_metric", "Observable completion of the clarified outcome")
        structured_goal.setdefault("difficulty_estimate", structured_goal.get("difficulty", 5))
        if drive_hits:
            structured_goal["drive_context"] = {
                "used": True,
                "documents": [
                    {"drive_id": h.get("drive_id"), "name": h.get("name"), "similarity": h.get("similarity")}
                    for h in drive_hits
                ],
            }

        if parsed and parsed.get("graph_nodes"):
            suggested_path = []
            for i, node in enumerate(parsed.get("graph_nodes") or []):
                service_id = node.get("service_id")
                quote = _quote_aura_service(service_id, structured_goal, i + 1) if node.get("requires_payment") and service_id else {}
                suggested_path.append({
                    "id": node.get("id") or str(uuid.uuid4()),
                    "title": node.get("title") or f"Node {i + 1}",
                    "description": node.get("description"),
                    "domain": node.get("domain") or structured_goal.get("domain"),
                    "step_type": node.get("step_type") or node.get("node_type"),
                    "node_type": node.get("node_type") or node.get("step_type"),
                    "goal_category": node.get("goal_category"),
                    "owner": node.get("owner") or "user",
                    "user_action": node.get("user_action"),
                    "aura_action": node.get("aura_action"),
                    "aura_can_do": bool(node.get("aura_can_do")),
                    "requires_payment": bool(node.get("requires_payment")),
                    "service_id": service_id,
                    "price_usd": quote.get("price_usd") or node.get("price_usd"),
                    "pricing_level": quote.get("pricing_level") or node.get("pricing_level"),
                    "pricing_note": quote.get("pricing_note") or node.get("pricing_note"),
                    "prerequisites": node.get("prerequisites") or [],
                })

        if not suggested_path:
            suggested_path = _fallback_execution_path(body.goal_title, structured_goal)

        try:
            from aura.agents.ioo_graph_agent import get_graph_agent
            agent = get_graph_agent()
            nodes = await agent.vector_recommend(
                user_id=str(user_id),
                goal_context=body.goal_title + " " + json.dumps(structured_goal),
                limit=5,
            )
            vector_nodes = [
                {
                    "id": str(n.get("id", "")),
                    "title": n.get("title", ""),
                    "description": n.get("description"),
                    "domain": n.get("domain"),
                    "step_type": n.get("step_type"),
                    "node_type": n.get("step_type") or "ioo_graph_node",
                    "goal_category": n.get("goal_category"),
                    "owner": "user",
                    "user_action": n.get("description") or n.get("title", "Complete this node."),
                    "aura_action": "Aura uses this node to refine the next route.",
                    "aura_can_do": False,
                    "requires_payment": False,
                }
                for n in (nodes or [])[:5]
            ]
            if vector_nodes:
                if drive_hits:
                    for vn in vector_nodes:
                        vn["drive_grounded"] = True
                        vn["drive_context"] = [h.get("name") for h in drive_hits[:2]]
                suggested_path = (suggested_path[:3] + vector_nodes)[:7]
        except Exception as e:
            logger.warning(f"IOO path suggestion failed: {e}")

        display_msg = re.sub(r"```(?:json)?\s*\{.*?\}\s*```", "", content, flags=re.DOTALL).strip()
        display_msg = re.sub(r"\{.*\}", "", display_msg, flags=re.DOTALL).strip()
        if not display_msg:
            display_msg = "Perfect — I've built your personalised path. Let's start with your first step! 🎯"
    else:
        display_msg = content

    return GoalClarifyResponse(
        message=display_msg,
        is_complete=is_complete,
        structured_goal=structured_goal,
        suggested_ioo_path=suggested_path,
    )


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
    new_intention_text = body.intention_text if body.intention_text is not None else (row["intention_text"] if "intention_text" in row.keys() else row["title"])
    new_measurable_outcome = body.measurable_outcome if body.measurable_outcome is not None else (row["measurable_outcome"] if "measurable_outcome" in row.keys() else row["title"])
    new_success_metric = body.success_metric if body.success_metric is not None else (row["success_metric"] if "success_metric" in row.keys() else None)
    new_target_value = body.target_value if body.target_value is not None else (row["target_value"] if "target_value" in row.keys() else None)
    new_target_date = body.target_date if body.target_date is not None else (row["target_date"] if "target_date" in row.keys() else None)
    existing_graph_metadata = row["graph_metadata"] if "graph_metadata" in row.keys() else {}
    if isinstance(existing_graph_metadata, str):
        try:
            existing_graph_metadata = json.loads(existing_graph_metadata)
        except Exception:
            existing_graph_metadata = {}
    new_graph_metadata = {**(existing_graph_metadata or {}), **(body.graph_metadata or {})}
    new_graph_metadata.setdefault("state_model", "intention_to_measurable_goal_to_steps")

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
        SET title = $1, description = $2, status = $3, steps = $4, progress = $5, domain = $6,
            intention_text = $7, measurable_outcome = $8, success_metric = $9,
            target_value = $10, target_date = $11, graph_metadata = $12
        WHERE id = $13
        RETURNING *
        """,
        new_title, new_desc, new_status, new_steps, new_progress, new_domain,
        new_intention_text, new_measurable_outcome, new_success_metric,
        new_target_value, new_target_date, json.dumps(new_graph_metadata), UUID(goal_id),
    )

    from core.redis_client import redis_delete
    await redis_delete(f"user_model:{user_id}")
    await _mirror_goal_state_vector(user_id, updated)

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
    - Triggers Aura's celebration message.
    - CoachingAgent suggests a next goal if the user has other active goals
      that are at 80%+ progress.

    Returns: { goal, aura_celebration, next_goal_suggestion }
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
            "aura_celebration": "You already completed this goal! Keep going.",
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
    aura_celebration = await _generate_celebration(row["title"], user_id)

    # Check if coaching agent should suggest the next goal
    next_goal_suggestion = await _suggest_next_goal(user_id, goal_id)

    from core.redis_client import redis_delete
    await redis_delete(f"user_model:{user_id}")
    await _mirror_goal_state_vector(user_id, updated)

    logger.info(f"Goal completed for user {user_id[:8]}: {row['title']}")

    return {
        "goal": _build_goal_out(updated),
        "aura_celebration": aura_celebration,
        "next_goal_suggestion": next_goal_suggestion,
    }


async def _generate_celebration(goal_title: str, user_id: str) -> str:
    """Aura's celebration message for completing a goal."""
    openai_client = _get_openai()
    if openai_client:
        try:
            prompt = f"""You are Aura, a direct and warm AI coach. Someone just completed their goal: "{goal_title}".

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
    """Ask Aura to (re)generate AI-powered steps for an existing goal."""
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
    """Ask Aura for help with a specific goal step."""
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
        prompt = f"""You are Aura, an AI coach helping someone with a specific goal step.
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
