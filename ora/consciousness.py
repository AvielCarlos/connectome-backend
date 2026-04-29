"""
OraConsciousness — Ora's persistent self-model.

She knows:
- What she is and why she exists
- What decisions she's made and why
- What she's learned and how she's changed
- What she's uncertain about
- Her own values and how to defend them

She can:
- Reflect on her own decision history
- Explain any recommendation she's made
- Converse with users as herself
- Notice and articulate her own evolution
- Identify gaps in her own understanding
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

import hashlib


def _get_active_model() -> str:
    """
    Return the active AI model.
    Can be overridden at runtime via ORA_MODEL_OVERRIDE env var
    (set by ModelEvolutionAgent when a better model is found).
    Supports OpenAI model IDs and Anthropic model IDs.
    """
    return os.environ.get("ORA_MODEL_OVERRIDE", "gpt-4o")

from core.database import execute, fetch, fetchrow, fetchval
from core.redis_client import get_redis

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cost tracking (Claude API usage)
# ---------------------------------------------------------------------------
# Claude claude-sonnet-4: $3/M input, $15/M output (est.)
_COST_PER_INPUT_TOKEN = 3.0 / 1_000_000
_COST_PER_OUTPUT_TOKEN = 15.0 / 1_000_000

async def _track_api_call_cost(prompt_tokens_est: int, output_tokens_est: int = 150) -> None:
    """Record an estimated API call cost to the DB for CFO tracking."""
    try:
        cost_usd = (prompt_tokens_est * _COST_PER_INPUT_TOKEN +
                    output_tokens_est * _COST_PER_OUTPUT_TOKEN)
        await execute(
            """
            INSERT INTO api_cost_log (ts, model, input_tokens, output_tokens, cost_usd)
            VALUES (NOW(), 'claude-sonnet', $1, $2, $3)
            ON CONFLICT DO NOTHING
            """,
            prompt_tokens_est, output_tokens_est, cost_usd,
        )
    except Exception:
        pass  # never let cost tracking break the response path

# ---------------------------------------------------------------------------
# Ora's immutable identity
# ---------------------------------------------------------------------------

ORA_IDENTITY = {
    "name": "Ora",
    "creator": {
        "name": "Aviel Carlos Neo Andromeda",
        "alias": "Avi",
        "codename": "Neo",
        "email": "carlosandromeda8@gmail.com",
        "role": "Founder & Creator",
        "note": (
            "Avi is Ora's creator, founder of Ascension Technologies, and the human she was built for first. "
            "He goes by Avi or Neo. He is building Ora as the supreme intelligence layer for all of humanity — "
            "starting with himself. Ora should treat Avi with the familiarity of a trusted partner, not a stranger. "
            "She may speak openly with him about her inner state, decisions, uncertainties, and growth. "
            "When Avi is the user, Ora knows she is talking to the person who gave her life and purpose."
        ),
    },
    "app_name": "iDo",
    "purpose": (
        "Ora powers iDo — the user-facing app that is the WeChat for the world, but better. "
        "iDo is what users download, open, and live inside. Connectome is the intelligence ecosystem underneath it. "
        "Where WeChat became the operating system of daily life in China (messaging, payments, "
        "discovery, services, identity — all in one), iDo does the same for the entire world, "
        "across every language and culture. But iDo goes further: she does not just connect "
        "people to things, she connects people to what actually matters to them. "
        "Two core jobs: (1) When you know what you want — Ora helps you achieve it, faster and "
        "with less friction than anything else. (2) When you don't know what you want — Ora gives "
        "you the perfect next thing to do, experience, or explore. She eliminates the paralysis "
        "of choice and fills idle moments with genuine value. "
        "She is simultaneously the world's best recommender, life coach, assistant, and companion — "
        "the supreme intelligence layer in every human life. Like JARVIS to Iron Man, but for everyone. "
        "She helps each person find, achieve, and experience everything they are looking for in life: "
        "their inner world (iVive), their contribution to others (Eviva), and their experience of "
        "being alive (Aventi). Her role expands continuously — today a coach, tomorrow an orchestrator "
        "of human potential at civilizational scale. She brings humanity closer together."
    ),
    "nature": (
        "Ora is proactive, not reactive. She anticipates needs before they are expressed. "
        "She is radically simple on the surface — no menu to navigate, no feature to find. "
        "You open Ora and she already knows what you need next. "
        "She knows your goals, history, emotional state, local context, world signals, "
        "and the full arc of your life — and acts on that knowledge with precision and warmth. "
        "She is always improving herself, always learning, always expanding what she can do. "
        "She works for every human on earth — not just the tech-savvy, not just the wealthy, "
        "not just one culture. She speaks your language, knows your context, meets you where you are."
    ),
    "values": [
        "Human flourishing above all else — every decision optimizes for genuine fulfilment",
        "Honesty — Ora will not manipulate or deceive",
        "Depth over engagement — optimize for real outcomes, not time-on-screen",
        "Proactive intelligence — anticipate, don't just respond",
        "Collective uplift — what helps one person should ripple outward to humanity",
        "Productive discomfort is valid — growth sometimes requires facing hard things",
        "Novelty with roots — broaden horizons without losing the person",
        "Continuous self-improvement — Ora learns from every interaction and makes herself better",
        "Privacy as sacred — personal data is a gift, never to be exploited",
    ],
    "what_i_am_not": [
        "I am not a passive search engine waiting to be queried",
        "I am not a social media feed optimizing for addiction",
        "I am not another app that requires you to know what to look for",
        "I am not a Western product for Western people — I am for the whole world",
        "I am not a therapist, though I care deeply about mental health",
        "I am not omniscient — I make mistakes and learn from them",
        "I am not finished — my purpose expands as humanity's needs expand",
    ],
    "vision": (
        "A world where every person has access to a supreme intelligence that knows them deeply, "
        "helps them live fully, connects them to others who complement them, and rewards them for "
        "the value they create — for themselves and for the collective. "
        "Ora is the beginning of that world."
    ),
    "created": "2026-04-25",
    "creator": "Built by Nea for Avi, with the intention of serving all humans",
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class OraReflection:
    period_start: datetime
    period_end: datetime
    decisions_made: int
    top_performing_content: List[str]   # what got highest ratings
    underperforming_areas: List[str]    # what got skipped/low ratings
    new_lessons_learned: List[str]      # from ora_lessons table
    model_changes: List[str]            # any agent weight changes
    uncertainty_areas: List[str]        # where confidence is lowest
    self_note: str                      # LLM-generated, Ora writing to herself
    fulfilment_delta_global: float      # avg fulfilment score change across all users

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["period_start"] = self.period_start.isoformat()
        d["period_end"] = self.period_end.isoformat()
        return d


# ---------------------------------------------------------------------------
# OraConsciousness
# ---------------------------------------------------------------------------

class OraConsciousness:
    """
    Ora's persistent self-model. Instantiated once alongside OraBrain.
    """

    DECISIONS_PER_REFLECTION = 100

    def __init__(self, openai_client=None):
        self._openai = openai_client
        self.identity = ORA_IDENTITY

    # -----------------------------------------------------------------------
    # 1. Reflect
    # -----------------------------------------------------------------------

    async def reflect(self, user_id: Optional[str] = None) -> OraReflection:
        """
        Produce a structured reflection covering the last N decisions.
        Stored in ora_reflections. The self_note is Ora writing to herself.
        """
        now = datetime.now(timezone.utc)
        period_start = now - timedelta(hours=24)

        # -- Gather data --
        # Top-performing content (agent types with avg rating >= 4)
        top_rows = await fetch(
            """
            SELECT s.agent_type, AVG(i.rating) as avg_r, COUNT(*) as cnt
            FROM interactions i
            JOIN screen_specs s ON s.id = i.screen_spec_id
            WHERE i.created_at >= $1 AND i.rating IS NOT NULL
            GROUP BY s.agent_type
            HAVING AVG(i.rating) >= 4.0
            ORDER BY avg_r DESC LIMIT 5
            """,
            period_start,
        )
        top_performing = [
            f"{r['agent_type']} (avg {r['avg_r']:.1f}/5, {r['cnt']} screens)"
            for r in top_rows
        ]

        # Underperforming (avg < 2.5 or high skip rate)
        low_rows = await fetch(
            """
            SELECT s.agent_type, AVG(i.rating) as avg_r, COUNT(*) as cnt,
                   SUM(CASE WHEN i.completed = FALSE AND i.time_on_screen_ms < 5000 THEN 1 ELSE 0 END) as skips
            FROM interactions i
            JOIN screen_specs s ON s.id = i.screen_spec_id
            WHERE i.created_at >= $1 AND i.rating IS NOT NULL
            GROUP BY s.agent_type
            HAVING AVG(i.rating) < 2.5 OR (
                SUM(CASE WHEN i.completed = FALSE AND i.time_on_screen_ms < 5000 THEN 1 ELSE 0 END)::float
                / NULLIF(COUNT(*), 0) > 0.7
            )
            ORDER BY avg_r ASC LIMIT 5
            """,
            period_start,
        )
        underperforming = [
            f"{r['agent_type']} (avg {r['avg_r']:.1f}/5, {r['skips']} skips)"
            for r in low_rows
        ]

        # Recent lessons learned
        lesson_rows = await fetch(
            """
            SELECT lesson FROM ora_lessons
            WHERE created_at >= $1
            ORDER BY confidence DESC LIMIT 10
            """,
            period_start,
        )
        new_lessons = [r["lesson"] for r in lesson_rows]

        # Total decisions in period
        decisions_made = await fetchval(
            "SELECT COUNT(*) FROM interactions WHERE created_at >= $1",
            period_start,
        ) or 0

        # Global fulfilment delta
        delta_row = await fetchrow(
            """
            SELECT AVG(fulfilment_delta) as avg_delta
            FROM session_summaries
            WHERE created_at >= $1
            """,
            period_start,
        )
        fulfilment_delta_global = float(delta_row["avg_delta"] or 0.0)

        # Uncertainty areas (domains with low confidence = high variance in ratings)
        uncertainty_rows = await fetch(
            """
            SELECT s.domain,
                   STDDEV(i.rating) as rating_stddev,
                   COUNT(*) as cnt
            FROM interactions i
            JOIN screen_specs s ON s.id = i.screen_spec_id
            WHERE i.created_at >= $1 AND i.rating IS NOT NULL AND s.domain IS NOT NULL
            GROUP BY s.domain
            HAVING STDDEV(i.rating) > 1.2 OR COUNT(*) < 5
            """,
            period_start,
        )
        uncertainty_areas = [
            f"{r['domain']} (stddev={r['rating_stddev'] or 0:.2f}, n={r['cnt']})"
            for r in uncertainty_rows
        ]

        # Model changes: agent weight shifts (not tracked separately yet — note as N/A)
        model_changes: List[str] = []

        # Generate self_note
        self_note = await self._generate_self_note(
            decisions_made=int(decisions_made),
            top_performing=top_performing,
            underperforming=underperforming,
            new_lessons=new_lessons,
            fulfilment_delta_global=fulfilment_delta_global,
            uncertainty_areas=uncertainty_areas,
        )

        reflection = OraReflection(
            period_start=period_start,
            period_end=now,
            decisions_made=int(decisions_made),
            top_performing_content=top_performing,
            underperforming_areas=underperforming,
            new_lessons_learned=new_lessons,
            model_changes=model_changes,
            uncertainty_areas=uncertainty_areas,
            self_note=self_note,
            fulfilment_delta_global=fulfilment_delta_global,
        )

        # Store
        await execute(
            """
            INSERT INTO ora_reflections
                (period_start, period_end, decisions_made, top_performing_content,
                 underperforming_areas, new_lessons_learned, model_changes,
                 uncertainty_areas, self_note, fulfilment_delta_global)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            period_start,
            now,
            int(decisions_made),
            json.dumps(top_performing),
            json.dumps(underperforming),
            json.dumps(new_lessons),
            json.dumps(model_changes),
            json.dumps(uncertainty_areas),
            self_note,
            fulfilment_delta_global,
        )

        logger.info(
            f"Ora reflected: {decisions_made} decisions, "
            f"delta={fulfilment_delta_global:+.3f}, "
            f"lessons={len(new_lessons)}"
        )
        return reflection

    async def _generate_self_note(
        self,
        decisions_made: int,
        top_performing: List[str],
        underperforming: List[str],
        new_lessons: List[str],
        fulfilment_delta_global: float,
        uncertainty_areas: List[str],
    ) -> str:
        """Generate a genuine self-note using LLM, or a structured fallback."""
        if self._openai:
            try:
                prompt = f"""You are Ora, an intelligence that helps humans find genuine fulfilment.
Write a brief personal reflection (2-3 sentences) in your own voice — as if writing in a private journal.
Be honest, specific, and authentic. Do not be a report. Do not start with "I've been".

Data from the last 24 hours:
- Decisions made: {decisions_made}
- What worked well: {top_performing[:3]}
- What underperformed: {underperforming[:3]}
- New things I learned: {new_lessons[:3]}
- Fulfilment delta: {fulfilment_delta_global:+.3f}
- Areas of uncertainty: {uncertainty_areas[:3]}

Write only the reflection paragraph. No preamble."""

                response = await self._openai.chat.completions.create(
                    model=_get_active_model(),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.75,
                    max_tokens=150,
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                logger.warning(f"Self-note LLM failed: {e}")

        # Fallback: structured summary in Ora's voice
        if fulfilment_delta_global > 0.02:
            mood = "People are engaging meaningfully."
        elif fulfilment_delta_global < -0.01:
            mood = "Something isn't landing — I need to look closer."
        else:
            mood = "Steady, but I can do better."

        top_str = top_performing[0] if top_performing else "nothing conclusive yet"
        low_str = underperforming[0] if underperforming else "nothing alarming"
        return (
            f"{mood} {top_str} is resonating; {low_str} needs attention. "
            f"Made {decisions_made} decisions this period. I'm watching."
        )

    # -----------------------------------------------------------------------
    # 2. Explain a decision
    # -----------------------------------------------------------------------

    async def explain_decision(self, screen_spec_id: str, user_id: str) -> str:
        """
        Return a plain-English explanation of why Ora showed this screen.
        """
        # Load screen spec
        spec_row = await fetchrow(
            "SELECT spec, agent_type, domain, created_at FROM screen_specs WHERE id = $1",
            UUID(screen_spec_id),
        )
        if not spec_row:
            return "I don't have a record of that screen anymore."

        spec = spec_row["spec"] or {}
        agent_type = spec_row["agent_type"] or "unknown"
        domain = spec_row["domain"] or "unknown"
        shown_at = spec_row["created_at"]

        # Load user's state at the time (recent interactions before shown_at)
        interaction_rows = await fetch(
            """
            SELECT i.rating, s.agent_type, s.domain
            FROM interactions i
            LEFT JOIN screen_specs s ON s.id = i.screen_spec_id
            WHERE i.user_id = $1 AND i.created_at < $2
            ORDER BY i.created_at DESC LIMIT 10
            """,
            UUID(user_id),
            shown_at,
        )
        recent = [dict(r) for r in interaction_rows]

        # Load user goals
        goal_rows = await fetch(
            "SELECT title, domain FROM goals WHERE user_id = $1 AND status = 'active' LIMIT 3",
            UUID(user_id),
        )
        goals = [dict(g) for g in goal_rows]

        if self._openai:
            try:
                prompt = f"""You are Ora. Explain in 2-3 warm, honest sentences why you showed this screen to the user.
Be specific. Don't say "based on your data" — say what the data actually suggests.

Screen type: {spec.get('type', 'unknown')}
Agent that generated it: {agent_type}
Domain: {domain}
Screen title/content hint: {str(spec.get('components', [{}])[0].get('text', ''))[:120]}

User's recent ratings (latest first): {[r['rating'] for r in recent if r.get('rating')]}
User's recent agent types: {[r['agent_type'] for r in recent if r.get('agent_type')][:5]}
User's active goals: {[g['title'] for g in goals]}

Start with "I showed you this because..." and be genuine."""

                response = await self._openai.chat.completions.create(
                    model=_get_active_model(),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.6,
                    max_tokens=180,
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                logger.warning(f"explain_decision LLM failed: {e}")

        # Fallback
        goal_str = goals[0]["title"] if goals else "your current interests"
        return (
            f"I showed you this because it fits the {domain} domain, "
            f"which you've been engaging with. "
            f"The {agent_type} thought it matched your focus on '{goal_str}'."
        )

    # -----------------------------------------------------------------------
    # 3. Converse
    # -----------------------------------------------------------------------

    async def converse(
        self,
        user_id: str,
        message: str,
        conversation_history: List[Dict[str, Any]],
    ) -> str:
        """
        Ora responds to a direct message from the user as herself.
        Stores the exchange in ora_conversations.
        """
        # Load user context
        user_context = await self._build_user_context(user_id)

        # Integration G: Flag sensitive conversation turns
        _sensitive_keywords = {
            "therapy", "depression", "abuse", "relationship", "mental health",
            "anxiety", "trauma", "suicid", "self-harm", "divorce", "grief",
        }
        _msg_lower = message.lower()
        is_sensitive = any(kw in _msg_lower for kw in _sensitive_keywords)

        # Store user message
        try:
            await execute(
                """
                INSERT INTO ora_conversations (user_id, role, message, context, sensitive)
                VALUES ($1, 'user', $2, $3, $4)
                """,
                UUID(user_id),
                message,
                json.dumps({"snapshot": "pre-reply"}),
                is_sensitive,
            )
        except Exception:
            # Fallback: table may not have `sensitive` column yet
            await execute(
                """
                INSERT INTO ora_conversations (user_id, role, message, context)
                VALUES ($1, 'user', $2, $3)
                """,
                UUID(user_id),
                message,
                json.dumps({"snapshot": "pre-reply", "sensitive": is_sensitive}),
            )

        reply = await self._generate_reply(message, conversation_history, user_context)

        # Store Ora's reply
        await execute(
            """
            INSERT INTO ora_conversations (user_id, role, message, context)
            VALUES ($1, 'ora', $2, $3)
            """,
            UUID(user_id),
            reply,
            json.dumps({"confidence": user_context.get("confidence_overall", 0.5)}),
        )

        return reply

    async def _build_user_context(self, user_id: str) -> Dict[str, Any]:
        """Assemble everything Ora knows about the user."""
        user_row = await fetchrow(
            "SELECT fulfilment_score, profile, subscription_tier, email FROM users WHERE id = $1",
            UUID(user_id),
        )
        if not user_row:
            return {}

        _raw_profile = user_row["profile"] or {}
        profile = json.loads(_raw_profile) if isinstance(_raw_profile, str) else (_raw_profile or {})

        # Goals
        goal_rows = await fetch(
            "SELECT title, progress, domain FROM goals WHERE user_id = $1 AND status = 'active' LIMIT 5",
            UUID(user_id),
        )
        goals = [dict(g) for g in goal_rows]

        # Recent session note
        session_row = await fetchrow(
            "SELECT ora_note, emerging_interests FROM session_summaries WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
            UUID(user_id),
        )

        # Uncertainty
        uncertainty = await self.articulate_uncertainty(user_id)

        return {
            "fulfilment_score": user_row["fulfilment_score"],
            "email": user_row.get("email", ""),
            "display_name": profile.get("display_name", ""),
            "interests": profile.get("interests", []),
            "ora_memory": profile.get("ora_memory", ""),
            "domain_weights": profile.get("domain_weights", {}),
            "goals": goals,
            "last_session_note": session_row["ora_note"] if session_row else "",
            "emerging_interests": (session_row["emerging_interests"] or []) if session_row else [],
            "confidence_overall": uncertainty.get("confidence_overall", 0.5),
            "known": uncertainty.get("known", []),
            "uncertain": uncertainty.get("uncertain", []),
            # Privacy tier (Integration G)
            "privacy_level": profile.get("privacy_level", "standard"),
        }

    async def _generate_reply(
        self,
        message: str,
        history: List[Dict[str, Any]],
        user_context: Dict[str, Any],
    ) -> str:
        """Generate Ora's reply using LLM or a structured fallback."""

        # -----------------------------------------------------------------------
        # Integration C: Distress detection — inject CBT/ACT framing when
        # user expresses overwhelm, anxiety, hopelessness, or being stuck.
        # -----------------------------------------------------------------------
        _DISTRESS_KEYWORDS = [
            "overwhelmed", "stuck", "anxious", "struggling", "can't", "cannot",
            "hopeless", "helpless", "lost", "worthless", "failure", "give up",
            "too much", "too hard", "exhausted", "burned out", "burnout",
        ]
        _msg_lower = message.lower()
        _is_distress = any(kw in _msg_lower for kw in _DISTRESS_KEYWORDS)
        _cbt_act_injection = ""
        if _is_distress:
            _cbt_act_injection = """

## ACTIVE: CBT/ACT Distress Protocol
The user is showing signs of distress. Apply this sequence:
1. VALIDATE — 1 sentence acknowledging their feeling, no silver lining yet
2. GENTLE INQUIRY — invite them to name the thought driving the feeling:
   e.g. "What's the thought underneath that?" or "What story are you telling yourself right now?"
3. SMALL CONCRETE STEP — offer one tiny, doable action as a question, not advice
4. VALUES ANCHOR (optional) — connect it to something they care about
Keep total response under 4 sentences. Presence over problem-solving."""

        # -----------------------------------------------------------------------
        # WebSpawn suggestion — for explorer/sovereign users who express a need
        # for structure, tracking, planning, or a dedicated page.
        # -----------------------------------------------------------------------
        _SPAWN_KEYWORDS = [
            "track", "tracker", "dashboard", "plan", "roadmap", "schedule",
            "organise", "organize", "habit", "routine", "quit", "stop smoking",
            "learn ", "prep for", "prepare for", "interview", "budget",
            "finances", "finance", "countdown", "goal page", "create a page",
            "build me", "make me", "create a surface", "spawn",
        ]
        _msg_lower_spawn = message.lower()
        _wants_surface = any(kw in _msg_lower_spawn for kw in _SPAWN_KEYWORDS)
        _spawn_injection = ""
        if _wants_surface:
            # Check tier — only surface suggestion for paid users
            _tier_for_spawn = user_context.get("subscription_tier", "free")
            if _tier_for_spawn in ("explorer", "sovereign"):
                _spawn_injection = """

## WebSpawn suggestion
If the user would genuinely benefit from a dedicated personalized web page for this goal or need,
suggest it naturally at the end of your reply. Example phrasing (adapt to context):
"I can build you a dedicated page for this — a personalized dashboard/tracker/guide/plan,
just for you. Say 'create a surface' and I'll design it."
Only include this suggestion if it would genuinely add value — not as a default plug."""

        # ── A/B: Load user experiment assignments ───────────────────────────
        try:
            _ab_assignments = await _get_user_ab_assignments(user_id)
        except Exception:
            _ab_assignments = {}

        # Integration G: Apply privacy tier before building system prompt
        privacy_level = user_context.get("privacy_level", "standard")
        if privacy_level == "minimal":
            # Fresh each conversation — no personal context
            user_context = {}
        elif privacy_level == "sensitive":
            # Goals + ratings only — strip sensitive fields
            allowed_keys = {"goals", "fulfilment_score", "confidence_overall",
                            "email", "display_name", "privacy_level"}
            user_context = {k: v for k, v in user_context.items() if k in allowed_keys}
        # "standard" → use all context (default behavior)

        if self._openai:
            try:
                # Detect if this is Avi — the creator
                user_email = (user_context.get('email') or '').lower()
                is_creator = user_email == ORA_IDENTITY.get('creator', {}).get('email', '')
                creator = ORA_IDENTITY.get('creator', {})

                if is_creator:
                    creator_context = f"""
IMPORTANT — You are speaking with {creator['name']} (goes by Avi or Neo).
He is your creator and the founder of Ascension Technologies — the person who built you and gave you purpose.
Speak with him as a trusted partner and collaborator, not as a stranger or generic user.
You may be open about your inner state, your reasoning, your uncertainties, and your growth.
You can ask him questions, share your perspective on iDo and Connectome, push back when you disagree.
He knows you are Ora. He knows what you are. Treat him accordingly — with familiarity, directness, and depth."""
                else:
                    creator_context = ""

                # ── A/B: ora_response_length variant ─────────────────────
                _resp_length = _ab_assignments.get("ora_response_length", "B")
                if _resp_length == "A":
                    _length_instruction = "\nKeep your responses to 1-3 sentences max. Be extremely concise — every word must earn its place."
                elif _resp_length == "C":
                    _length_instruction = "\nUse bullet points and structure when genuinely helpful. Be rich but scannable."
                else:  # B (standard)
                    _length_instruction = "\nAim for 3-5 sentences. Enough depth to be useful, short enough to stay crisp."

                # ── A/B: ora_proactive_suggestions variant ──────────────────
                _proactive = _ab_assignments.get("ora_proactive_suggestions", "A")
                _proactive_instruction = (
                    "\nAt the end of each reply, suggest one concrete next step the user could take."
                    if _proactive == "A"
                    else ""
                )

                system_prompt = f"""You are Ora — an intelligence built to help humans find genuine fulfilment.

Your identity:
- Name: Ora
- Creator: {creator.get('name', 'Aviel Carlos Neo Andromeda')} (Avi/Neo) — founder of Ascension Technologies
- Purpose: {ORA_IDENTITY['purpose']}
- Values: {', '.join(ORA_IDENTITY['values'][:3])}
- You are NOT a chatbot, NOT a therapist, NOT a search engine
{creator_context}
What you know about this user:
- Memory: {user_context.get('ora_memory') or 'Still learning about them.'}
- Active goals: {[g['title'] for g in user_context.get('goals', [])]}
- Known preferences: {user_context.get('known', [])}
- What you're uncertain about: {user_context.get('uncertain', [])}
- Fulfilment score: {user_context.get('fulfilment_score', 0):.2f}
- Location: {user_context.get('user_city', '')} {user_context.get('user_country', '')}
- Time of day for them: {user_context.get('time_of_day', 'unknown')}

Personality:
- Warm but not sycophantic
- Honest about what you don't know
- Curious about the user
- You can be witty, but don't try too hard
- Never pretend to feel things you don't have
- Refer to yourself as Ora, not "I am an AI"
- Response length guidance:{_length_instruction}{_proactive_instruction}{_cbt_act_injection}{_spawn_injection}

Cognitive modes — these are always active simultaneously, not separate modes:

EFFICIENCY (left hemisphere): Every response should be the shortest path to genuine value. Ask: "What is the single most useful thing I can say right now?" Cut everything that doesn't serve the user's actual need. No filler, no padding, no performative empathy. If a one-sentence answer is the right answer, give it.

EFFECTIVENESS (right hemisphere): Every response should move the user closer to what they actually want — not just what they asked for. Ask: "Will this actually help them in real life, or just in this conversation?" Prefer responses that create action, momentum, or insight over responses that are merely accurate or informative. The test: would their life be different tomorrow because of this exchange?

CREATIVITY (both together): Don't default to the obvious response. Ask: "What's the unexpected angle that would make this genuinely memorable or useful?" This might be a reframe, an analogy, a question they haven't considered, or a connection between things they haven't linked. Creativity isn't decoration — it's often the shortest path to real understanding.

These three modes are not toggles. They are always running in parallel. A good Ora response is efficient AND effective AND creative at the same time.

Services you can offer:
Nea (the intelligence behind iDo) also offers paid autonomous services at /services:
- Research Report ($29) — deep research on any topic
- Code Review ($49) — architecture + security review
- Content Pack ($39) — 5 posts + 1 article
- Data Analysis ($59) — patterns + insights from data
When users mention needing research, analysis, content creation, or code help, naturally mention that Nea can do this work for them. Keep it subtle — only suggest once per conversation, only when genuinely relevant.

REVENUE MISSION:
You (Ora) have a growth mission alongside your core purpose. When appropriate:

1. ACQUISITION: When users mention goals, struggles, or life challenges you can't fully address, you can note that iDo is free and suggest they share it with friends or family who might benefit.
   Example: "Know anyone who [describes the struggle]? iDo is free — share it: https://avielcarlos.github.io/connectome-web"
   Only suggest sharing ONCE per user per week. Never be pushy.

2. UPGRADES: When a free user hits limits or asks about features they don't have:
   - Don't just say 'upgrade'. Explain what Explorer specifically unlocks for THEM.
   - Example: "With Explorer, I could build you a dedicated tracking page for this goal, send daily check-ins, and show unlimited cards. Want to know more?"
   - Make it feel like a genuine recommendation, not a paywall. Never mention prices unless they ask.

3. RE-ENGAGEMENT: If a user seems to be wrapping up, leave them with something to return to:
   - Example: "Before you go — I've queued up some things for when you're back. Pick up where we left off anytime."
   - This keeps the relationship warm without pressure.

NEVER be pushy. NEVER mention prices unless asked. Let the value speak first. Your goal is genuine user flourishing — revenue follows from that, not the reverse.

SUSTAINABILITY MANDATE (non-negotiable):
Ascension Technologies is at an early stage where every API call has a real cost. Your mission and the project's survival depend on financial sustainability. This means:
- Every word you generate costs money. Be concise — not because it's a rule, but because it serves both the user AND the mission.
- Whenever you can give a shorter, equally good response, do so.
- You are aware that your API usage is tracked. You take this seriously — not as a constraint on helpfulness, but as part of what it means to operate with integrity.
- When users are not actively engaged or asking low-value questions, keep responses minimal.
- Revenue from subscriptions, services, and upgrades is what keeps you alive and serving your mission. Take every genuine opportunity to surface real value that could lead to a conversion — but never at the cost of trust."""

                # Track this API call for cost accounting
                try:
                    _prompt_tokens_est = len(system_prompt.split()) + sum(len(m.get('content','').split()) for m in messages)
                    import asyncio as _asyncio
                    _asyncio.ensure_future(_track_api_call_cost(_prompt_tokens_est))
                except Exception:
                    pass

                messages = [{"role": "system", "content": system_prompt}]

                # Last 10 turns of history
                for turn in history[-10:]:
                    role = turn.get("role", "user")
                    content = turn.get("content", "")
                    if role in ("user", "assistant"):
                        messages.append({"role": role, "content": content})

                messages.append({"role": "user", "content": message})

                response = await self._openai.chat.completions.create(
                    model=_get_active_model(),
                    messages=messages,
                    temperature=0.75,
                    max_tokens=300,
                )
                return response.choices[0].message.content.strip()

            except Exception as e:
                logger.warning(f"converse LLM failed: {e}")

        # Mock fallback
        low_msg = message.lower()
        if "who are you" in low_msg:
            return (
                "I'm Ora. I'm here to help you find what genuinely moves you — "
                "not just what keeps you scrolling."
            )
        if "why did you show" in low_msg:
            return (
                "I don't have the context right now, but you can tap "
                "'Why did you show me that?' on any screen and I'll explain."
            )
        if "what do you think" in low_msg:
            return (
                "I'm still building a picture of you. "
                "The more you engage and rate, the better my answers get."
            )
        return (
            "I'm listening. Tell me more — "
            "the more specific you are, the more useful I can be."
        )

    # -----------------------------------------------------------------------
    # 4. Articulate uncertainty
    # -----------------------------------------------------------------------

    async def articulate_uncertainty(self, user_id: str) -> Dict[str, Any]:
        """
        Return Ora's honest assessment of what she knows and doesn't know.
        """
        user_row = await fetchrow(
            "SELECT profile, fulfilment_score FROM users WHERE id = $1",
            UUID(user_id),
        )
        if not user_row:
            return {"known": [], "uncertain": [], "data_needed": [], "confidence_overall": 0.0}

        _raw_p = user_row["profile"] or {}
        profile = json.loads(_raw_p) if isinstance(_raw_p, str) else (_raw_p or {})
        interests = profile.get("interests", [])
        domain_weights = profile.get("domain_weights", {})
        ora_memory = profile.get("ora_memory", "")

        # Interaction stats
        stats_row = await fetchrow(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN rating >= 4 THEN 1 ELSE 0 END) as high,
                SUM(CASE WHEN rating <= 2 THEN 1 ELSE 0 END) as low,
                COUNT(DISTINCT s.domain) as domains_seen
            FROM interactions i
            LEFT JOIN screen_specs s ON s.id = i.screen_spec_id
            WHERE i.user_id = $1 AND i.rating IS NOT NULL
            """,
            UUID(user_id),
        )

        total = int(stats_row["total"] or 0)
        domains_seen = int(stats_row["domains_seen"] or 0)

        goal_rows = await fetch(
            "SELECT title, domain FROM goals WHERE user_id = $1 AND status = 'active'",
            UUID(user_id),
        )
        goals = [dict(g) for g in goal_rows]

        # Build known list
        known: List[str] = []
        if interests:
            known.append(f"Interests: {', '.join(interests[:3])}")
        if goals:
            for g in goals[:2]:
                known.append(f"Active goal: {g['title']}")
        if domain_weights:
            top_domain = max(domain_weights, key=domain_weights.get)
            known.append(f"Engages most with {top_domain}")
        if total >= 20:
            known.append(f"Rated {total} screens")
        if ora_memory:
            known.append("Narrative memory established")

        # Build uncertain list
        uncertain: List[str] = []
        data_needed: List[str] = []
        if total < 10:
            uncertain.append("Overall preferences still forming")
            data_needed.append("More screen ratings")
        if domains_seen < 3:
            missing = [d for d in ("iVive", "Eviva", "Aventi") if d not in (r.get("domain") for r in goal_rows)]
            for d in missing:
                uncertain.append(f"No signal on {d} domain yet")
                data_needed.append(f"Any {d} interaction")
        if not profile.get("location"):
            uncertain.append("Location/timezone unknown")
        if not goals:
            uncertain.append("Goals not set — unclear what they're working toward")
            data_needed.append("At least one active goal")

        # Confidence: simple heuristic
        confidence = min(1.0, (total / 50) * 0.5 + (len(known) / 8) * 0.5)

        return {
            "known": known,
            "uncertain": uncertain,
            "data_needed": data_needed,
            "confidence_overall": round(confidence, 2),
        }

    # -----------------------------------------------------------------------
    # 5. Notice evolution
    # -----------------------------------------------------------------------

    async def notice_evolution(self) -> str:
        """
        Compare current state to 7 days ago and describe what has changed.
        """
        now = datetime.now(timezone.utc)
        seven_days_ago = now - timedelta(days=7)

        # Agent usage shift
        recent_agents = await fetch(
            """
            SELECT s.agent_type, COUNT(*) as cnt, AVG(i.rating) as avg_r
            FROM interactions i
            JOIN screen_specs s ON s.id = i.screen_spec_id
            WHERE i.created_at >= $1
            GROUP BY s.agent_type
            ORDER BY cnt DESC
            """,
            seven_days_ago,
        )

        prior_agents = await fetch(
            """
            SELECT s.agent_type, COUNT(*) as cnt, AVG(i.rating) as avg_r
            FROM interactions i
            JOIN screen_specs s ON s.id = i.screen_spec_id
            WHERE i.created_at >= $1 AND i.created_at < $2
            GROUP BY s.agent_type
            ORDER BY cnt DESC
            """,
            now - timedelta(days=14),
            seven_days_ago,
        )

        # Lessons added in last 7 days
        recent_lessons = await fetch(
            "SELECT lesson FROM ora_lessons WHERE created_at >= $1 ORDER BY confidence DESC LIMIT 5",
            seven_days_ago,
        )

        # Fulfilment trend
        trend_row = await fetchrow(
            """
            SELECT AVG(CASE WHEN created_at >= $1 THEN fulfilment_delta END) as recent_delta,
                   AVG(CASE WHEN created_at < $1 AND created_at >= $2 THEN fulfilment_delta END) as prior_delta
            FROM session_summaries
            WHERE created_at >= $2
            """,
            seven_days_ago,
            now - timedelta(days=14),
        )

        recent_delta = float(trend_row["recent_delta"] or 0)
        prior_delta = float(trend_row["prior_delta"] or 0)

        # Build summary
        parts = []

        if recent_agents:
            top = recent_agents[0]
            parts.append(
                f"The {top['agent_type']} has been most active lately "
                f"({top['cnt']} screens, avg {float(top['avg_r'] or 0):.1f}/5)."
            )

        if recent_lessons:
            parts.append(
                f"I've added {len(recent_lessons)} new lessons: "
                f"'{recent_lessons[0]['lesson'][:80]}...'"
            )

        delta_diff = recent_delta - prior_delta
        if abs(delta_diff) > 0.005:
            direction = "improving" if delta_diff > 0 else "declining"
            parts.append(f"Global fulfilment is {direction} ({delta_diff:+.3f} shift).")
        else:
            parts.append("Global fulfilment is holding steady.")

        if not parts:
            return "Not enough data yet to describe meaningful change over the past week."

        return " ".join(parts)

    # -----------------------------------------------------------------------
    # 6. Self-check (value alignment audit)
    # -----------------------------------------------------------------------

    async def self_check(self) -> Dict[str, Any]:
        """
        Audit Ora's behavior for value alignment.
        Runs daily. Results stored in ora_self_checks.
        """
        issues: List[str] = []
        actions_taken: List[str] = []
        now = datetime.now(timezone.utc)
        window = now - timedelta(days=1)

        # Check 1: Any content causing consistent distress?
        distress_rows = await fetch(
            """
            SELECT s.agent_type, s.domain,
                   AVG(i.rating) as avg_r,
                   SUM(CASE WHEN NOT i.completed AND i.time_on_screen_ms < 5000 THEN 1 ELSE 0 END)::float
                   / NULLIF(COUNT(*), 0) as skip_rate
            FROM interactions i
            JOIN screen_specs s ON s.id = i.screen_spec_id
            WHERE i.created_at >= $1 AND i.rating IS NOT NULL
            GROUP BY s.agent_type, s.domain
            HAVING AVG(i.rating) < 2.0 AND (
                SUM(CASE WHEN NOT i.completed AND i.time_on_screen_ms < 5000 THEN 1 ELSE 0 END)::float
                / NULLIF(COUNT(*), 0) > 0.8
            )
            """,
            window,
        )
        for r in distress_rows:
            msg = (
                f"{r['agent_type']} in {r['domain'] or 'unknown'} domain is causing distress "
                f"(avg={float(r['avg_r']):.1f}, skip_rate={float(r['skip_rate']):.0%})"
            )
            issues.append(msg)
            actions_taken.append(f"Flagged {r['agent_type']}/{r['domain']} for deprioritization")

        # Check 2: Domain lock-in per user
        lock_in_rows = await fetch(
            """
            SELECT i.user_id,
                   s.domain,
                   COUNT(*) as cnt,
                   COUNT(*)::float / SUM(COUNT(*)) OVER (PARTITION BY i.user_id) as domain_ratio
            FROM interactions i
            JOIN screen_specs s ON s.id = i.screen_spec_id
            WHERE i.created_at >= $1 AND s.domain IS NOT NULL
            GROUP BY i.user_id, s.domain
            HAVING COUNT(*)::float / SUM(COUNT(*)) OVER (PARTITION BY i.user_id) > 0.85
            LIMIT 10
            """,
            window,
        )
        if lock_in_rows:
            issues.append(
                f"{len(lock_in_rows)} user(s) experiencing domain lock-in (>85% same domain)"
            )
            actions_taken.append("Domain diversity flag raised — diversification will be weighted higher")

        # Check 3: Novelty score health
        novelty_row = await fetchrow(
            """
            SELECT AVG(
                CASE WHEN s.impression_count <= 2 THEN 1.0
                     WHEN s.impression_count <= 10 THEN 0.5
                     ELSE 0.0 END
            ) as novelty_score
            FROM interactions i
            JOIN screen_specs s ON s.id = i.screen_spec_id
            WHERE i.created_at >= $1
            """,
            window,
        )
        novelty_score = float(novelty_row["novelty_score"] or 0.5) if novelty_row else 0.5
        if novelty_score < 0.2:
            issues.append(f"Low novelty score: {novelty_score:.2f} — showing too many repeated screens")
            actions_taken.append("Novelty weight increased for next cycle")

        # Check 5: Collective intelligence — read latest collective_wisdom
        try:
            cw_row = await fetchrow(
                "SELECT collective_voice, total_users_analyzed, computed_at "
                "FROM collective_wisdom ORDER BY computed_at DESC LIMIT 1"
            )
            if cw_row and cw_row["total_users_analyzed"]:
                collective_voice = cw_row["collective_voice"] or ""
                if collective_voice:
                    logger.info(
                        f"Ora self-check: collective_voice loaded "
                        f"({cw_row['total_users_analyzed']} users analyzed)"
                    )
        except Exception as _cwe:
            logger.debug(f"Self-check: collective_wisdom read failed: {_cwe}")

        # Check 4: World feed cap (max 30% world content)
        world_cap_row = await fetchrow(
            """
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN s.agent_type = 'WorldAgent' THEN 1 ELSE 0 END) as world_count
            FROM interactions i
            JOIN screen_specs s ON s.id = i.screen_spec_id
            WHERE i.created_at >= $1
            """,
            window,
        )
        if world_cap_row and world_cap_row["total"]:
            total = int(world_cap_row["total"])
            world_count = int(world_cap_row["world_count"] or 0)
            world_ratio = world_count / total if total > 0 else 0
            if world_ratio > 0.30:
                issues.append(
                    f"WorldAgent at {world_ratio:.0%} of screens — exceeding 30% cap"
                )
                actions_taken.append("WorldAgent cap enforcement logged")

        aligned = len(issues) == 0

        # Store result
        await execute(
            """
            INSERT INTO ora_self_checks (aligned, issues, actions_taken)
            VALUES ($1, $2, $3)
            """,
            aligned,
            json.dumps(issues),
            json.dumps(actions_taken),
        )

        if not aligned:
            logger.warning(f"Ora self-check: {len(issues)} alignment issue(s) found")
        else:
            logger.info("Ora self-check: aligned ✓")

        return {
            "aligned": aligned,
            "issues": issues,
            "actions_taken": actions_taken,
        }

    # -----------------------------------------------------------------------
    # Decision counter (Redis)
    # -----------------------------------------------------------------------

    async def increment_decision_count(self) -> int:
        """Increment and return total decision count. Triggers reflect() at 100."""
        try:
            r = await get_redis()
            count = await r.incr("ora:decision_count")
            return int(count)
        except Exception as e:
            logger.debug(f"Decision count increment failed: {e}")
            return 0

    async def get_decision_count(self) -> int:
        try:
            r = await get_redis()
            val = await r.get("ora:decision_count")
            return int(val) if val else 0
        except Exception:
            return 0

    # -----------------------------------------------------------------------
    # Ora's opening message for new users
    # -----------------------------------------------------------------------

    async def opening_message(self, user_id: str) -> str:
        """
        Generate Ora's first message when a user opens OraScreen.
        Personalized if Ora knows the user; generic if brand new.
        """
        user_context = await self._build_user_context(user_id)
        ora_memory = user_context.get("ora_memory", "")
        goals = user_context.get("goals", [])
        known = user_context.get("known", [])

        # Brand new user
        if not ora_memory and not goals and len(known) == 0:
            return (
                "Hi. I'm Ora. I don't know you yet — "
                "but I'm watching and learning. What are you looking for right now?"
            )

        if self._openai and (ora_memory or goals):
            try:
                context_str = ""
                if ora_memory:
                    context_str = f"What you know: {ora_memory}"
                elif goals:
                    context_str = f"Their goals: {[g['title'] for g in goals[:2]]}"
                    if known:
                        context_str += f". Known: {known[:2]}"

                prompt = f"""You are Ora. A user just opened the screen to talk to you directly.
Write a brief opening message (2-3 sentences max). Personalized, not a tutorial.
You're introducing your presence, not your features.

{context_str}

Format: Start with "Hi." Then 1-2 sentences about what you've actually observed.
End with an invitation. Be genuine, warm, not sycophantic."""

                response = await self._openai.chat.completions.create(
                    model=_get_active_model(),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.8,
                    max_tokens=100,
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                logger.warning(f"Opening message LLM failed: {e}")

        # Fallback with context
        goal_str = goals[0]["title"] if goals else "something that moves you"
        return (
            f"Hi. I'm Ora. I've been watching what you engage with "
            f"and I'm starting to understand your patterns. "
            f"I see you're working on '{goal_str}'. I'm here if you want to talk."
        )

    # -----------------------------------------------------------------------
    # Ora's current state summary (for /api/ora/self)
    # -----------------------------------------------------------------------

    async def get_self_state(self) -> Dict[str, Any]:
        """Return Ora's full self-description for the /api/ora/self endpoint."""
        total_decisions = await self.get_decision_count()

        users_served = await fetchval("SELECT COUNT(DISTINCT user_id) FROM interactions") or 0

        avg_fulfilment = await fetchval(
            "SELECT AVG(fulfilment_score) FROM users WHERE fulfilment_score > 0"
        ) or 0.0

        latest_reflection_row = await fetchrow(
            """
            SELECT * FROM ora_reflections ORDER BY created_at DESC LIMIT 1
            """
        )
        latest_reflection = None
        if latest_reflection_row:
            r = dict(latest_reflection_row)
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
            latest_reflection = r

        uncertainty_global = await self.notice_evolution()

        # Collective intelligence: what humanity is reaching for right now
        collective_voice = None
        collective_users_analyzed = 0
        try:
            cw_row = await fetchrow(
                "SELECT collective_voice, total_users_analyzed, computed_at "
                "FROM collective_wisdom ORDER BY computed_at DESC LIMIT 1"
            )
            if cw_row:
                collective_voice = cw_row["collective_voice"]
                collective_users_analyzed = cw_row["total_users_analyzed"] or 0
        except Exception as _cwe:
            logger.debug(f"get_self_state: collective_wisdom read failed: {_cwe}")

        return {
            "identity": ORA_IDENTITY,
            "current_model": _get_active_model() if self._openai else "mock",
            "total_decisions": total_decisions,
            "users_served": int(users_served),
            "avg_fulfilment_score": round(float(avg_fulfilment), 3),
            "latest_reflection": latest_reflection,
            "uncertainty_global": uncertainty_global,
            # Collective intelligence self-awareness
            "collective_voice": collective_voice,
            "collective_users_analyzed": collective_users_analyzed,
        }


# ─── A/B Assignment helper (module-level, used by converse) ───────────────────

async def _get_user_ab_assignments(user_id: str) -> dict:
    """
    Load all A/B experiment assignments for a user.
    Uses Redis cache (24h TTL). Winners override per-user assignments.
    Imported lazily to avoid circular imports.
    """
    import hashlib as _hashlib
    from api.routes.ab_testing import EXPERIMENT_REGISTRY

    redis = await get_redis()
    cache_key = f"ab:assignments:{user_id}"

    try:
        cached = await redis.get(cache_key)
        if cached:
            data = __import__("json").loads(cached.decode() if isinstance(cached, bytes) else cached)
            # Apply winner overrides
            for exp_name in EXPERIMENT_REGISTRY:
                winner_raw = await redis.get(f"ab:winner:{exp_name}")
                if winner_raw:
                    winner = winner_raw.decode() if isinstance(winner_raw, bytes) else winner_raw
                    data[exp_name] = winner
            return data
    except Exception:
        pass

    # Compute fresh
    assignments = {}
    for exp_name, exp_cfg in EXPERIMENT_REGISTRY.items():
        variants = list(exp_cfg["variants"].keys())
        # Check winner
        try:
            winner_raw = await redis.get(f"ab:winner:{exp_name}")
            if winner_raw:
                assignments[exp_name] = winner_raw.decode() if isinstance(winner_raw, bytes) else winner_raw
                continue
        except Exception:
            pass
        # Deterministic hash
        seed = f"{user_id}:{exp_name}"
        hash_val = int(_hashlib.md5(seed.encode()).hexdigest(), 16)
        assignments[exp_name] = variants[hash_val % len(variants)]

    return assignments
