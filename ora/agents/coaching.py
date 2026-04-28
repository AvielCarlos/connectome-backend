"""
CoachingAgent
Generates goal-oriented coaching screens — check-ins, reflections,
micro-challenges, and accountability nudges.

V2 improvements:
- Streak tracking: celebrates consistency, adapts on breaks
- Emotional state awareness: adjusts tone based on mood + recent ratings
- Coaching type variety: rotates across reflection/challenge/celebration/reset
- Deeper personalisation: uses goals, next steps, and fulfilment trajectory
"""

import logging
import json
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timezone, date

from core.config import settings

logger = logging.getLogger(__name__)


MOCK_COACHING_SESSIONS = [
    {
        "title": "What's stopping you?",
        "prompt": "Think about the goal you set last week. What's the real obstacle — not the surface one, but the underlying fear or belief?",
        "reflection_prompts": [
            "I'm afraid that...",
            "The story I tell myself is...",
            "One small step I could take today is...",
        ],
        "cta": "Reflect now",
    },
    {
        "title": "Win Review",
        "prompt": "Name three things that went well this week. They don't have to be big. Progress is progress.",
        "reflection_prompts": [
            "A win I'm proud of...",
            "What made it possible...",
            "How I'll build on it...",
        ],
        "cta": "List your wins",
    },
    {
        "title": "The 1% Challenge",
        "prompt": "You don't need to transform overnight. What's one thing you could do 1% better today than yesterday?",
        "reflection_prompts": [
            "The area I want to improve...",
            "My 1% improvement today...",
            "How I'll know I did it...",
        ],
        "cta": "Take the challenge",
    },
    {
        "title": "Intention Setting",
        "prompt": "Before the day escapes you — set one clear intention. Not a to-do list. One thing that would make today feel complete.",
        "reflection_prompts": [
            "Today I intend to...",
            "This matters because...",
            "I'll do it at...",
        ],
        "cta": "Set my intention",
    },
]


class CoachingAgent:
    """
    Generates coaching screens tied to the user's active goals.
    Focuses on reflection, accountability, and forward momentum.
    """

    AGENT_NAME = "CoachingAgent"

    def __init__(self, openai_client=None):
        self.openai = openai_client

    # -----------------------------------------------------------------------
    # Streak helpers
    # -----------------------------------------------------------------------

    async def _get_streak(self, user_id: str) -> Dict[str, Any]:
        """Load or initialise the user's coaching streak from DB."""
        try:
            from core.database import fetchrow, execute
            row = await fetchrow(
                "SELECT current_streak, longest_streak, last_coaching_date, total_sessions "
                "FROM coaching_streaks WHERE user_id = $1",
                user_id if not isinstance(user_id, str) else __import__('uuid').UUID(user_id),
            )
            if not row:
                return {"current_streak": 0, "longest_streak": 0, "total_sessions": 0, "last_coaching_date": None}
            return {
                "current_streak": row["current_streak"] or 0,
                "longest_streak": row["longest_streak"] or 0,
                "total_sessions": row["total_sessions"] or 0,
                "last_coaching_date": row["last_coaching_date"],
            }
        except Exception as e:
            logger.debug(f"CoachingAgent streak load failed: {e}")
            return {"current_streak": 0, "longest_streak": 0, "total_sessions": 0, "last_coaching_date": None}

    async def _update_streak(self, user_id: str) -> Dict[str, Any]:
        """Increment or reset the streak and return the updated values."""
        try:
            from core.database import fetchrow, execute
            import uuid as _uuid
            uid = _uuid.UUID(user_id) if isinstance(user_id, str) else user_id
            today = date.today()

            row = await fetchrow(
                "SELECT current_streak, longest_streak, last_coaching_date, total_sessions "
                "FROM coaching_streaks WHERE user_id = $1", uid
            )
            if not row:
                await execute(
                    """
                    INSERT INTO coaching_streaks (user_id, current_streak, longest_streak, last_coaching_date, total_sessions)
                    VALUES ($1, 1, 1, $2, 1)
                    """,
                    uid, today
                )
                return {"current_streak": 1, "longest_streak": 1, "total_sessions": 1, "is_new": True}

            last_date = row["last_coaching_date"]
            current = row["current_streak"] or 0
            longest = row["longest_streak"] or 0
            total = (row["total_sessions"] or 0) + 1

            if last_date is None:
                current = 1
            elif (today - last_date).days == 1:
                current += 1  # Consecutive day
            elif (today - last_date).days == 0:
                pass  # Same day, don't increment
            else:
                current = 1  # Streak broken

            longest = max(longest, current)
            await execute(
                """
                INSERT INTO coaching_streaks (user_id, current_streak, longest_streak, last_coaching_date, total_sessions, updated_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET current_streak = $2, longest_streak = $3,
                    last_coaching_date = $4, total_sessions = $5, updated_at = NOW()
                """,
                uid, current, longest, today, total
            )
            return {"current_streak": current, "longest_streak": longest, "total_sessions": total}
        except Exception as e:
            logger.debug(f"CoachingAgent streak update failed: {e}")
            return {"current_streak": 0, "longest_streak": 0, "total_sessions": 0}

    @staticmethod
    def _infer_emotional_state(user_context: Dict[str, Any]) -> Tuple[str, str]:
        """
        Infer the user's likely emotional state from mood + recent ratings.
        Returns (state_label, coaching_tone_hint).
        """
        mood = user_context.get("session_mood")  # 0=tired, 4=energised
        recent_ratings = user_context.get("recent_ratings", [])
        avg_rating = sum(recent_ratings) / len(recent_ratings) if recent_ratings else 3.0
        fulfilment = user_context.get("fulfilment_score", 0.5)

        # Map to emotional state
        if mood is not None and mood <= 1:
            return ("low_energy", "Be gentle and restorative. Short, easy exercises only. Focus on self-compassion over achievement.")
        if avg_rating <= 2.0 and len(recent_ratings) >= 3:
            return ("disengaged", "Re-engage with curiosity, not pressure. Ask questions that spark interest rather than accountability.")
        if avg_rating >= 4.2 and fulfilment >= 0.65:
            return ("thriving", "Match their momentum. Stretch goals, bolder challenges. Celebrate and build on wins.")
        if mood is not None and mood >= 4:
            return ("energised", "Channel the energy into bold action. A clear, exciting micro-challenge works best here.")
        if fulfilment < 0.3:
            return ("struggling", "Lead with empathy. Acknowledge difficulty before pivoting to small actionable steps.")
        return ("balanced", "Standard coaching tone — warm, direct, specific.")

    async def generate_screen(
        self,
        user_context: Dict[str, Any],
        variant: str = "A",
    ) -> Dict[str, Any]:
        if self.openai and settings.has_openai:
            return await self._generate_with_ai(user_context, variant)
        return self._generate_mock(user_context, variant)

    # -----------------------------------------------------------------------
    # Integration C: CBT/ACT Mode Detection
    # -----------------------------------------------------------------------

    @staticmethod
    def _detect_cbt_act_mode(emotional_state: str, user_context: Dict[str, Any]) -> str:
        """
        Determine which CBT/ACT technique to apply based on emotional state,
        recent interaction patterns, and context.

        Returns one of:
          cbt_thought_record  — negative thought → Situation→Thought→Emotion→Evidence→Balance
          behavioral_activation — stuck/unmotivated → small achievable activity + schedule
          act_values           — periodic values clarification
          solution_focused     — rocky.ai pattern: what's working / 10% improvement
          standard             — warm accountability check-in
        """
        recent_ratings = user_context.get("recent_ratings", [])
        avg_rating = sum(recent_ratings) / len(recent_ratings) if recent_ratings else 3.0
        total_sessions = user_context.get("coaching_sessions", 0)

        # Explicit signals that point to CBT Thought Record
        if emotional_state in ("struggling", "disengaged") or avg_rating <= 2.0:
            return "cbt_thought_record"

        # Low energy / stuck → Behavioral Activation
        if emotional_state == "low_energy":
            return "behavioral_activation"

        # Every ~5 sessions, do a values clarification
        if total_sessions > 0 and total_sessions % 5 == 0:
            return "act_values"

        # When engagement is moderate, use solution-focused (rocky.ai)
        if 2.0 < avg_rating <= 3.5:
            return "solution_focused"

        # Thriving / energised → standard accountability + challenge
        return "standard"

    async def _generate_with_ai(
        self, user_context: Dict[str, Any], variant: str
    ) -> Dict[str, Any]:
        goals = user_context.get("active_goals", [])
        fulfilment = user_context.get("fulfilment_score", 0.5)
        recent_ratings = user_context.get("recent_ratings", [])
        display_name = user_context.get("display_name", "")
        domain = user_context.get("domain", "iVive")
        user_id = user_context.get("user_id", "")
        ora_lessons = user_context.get("ora_lessons", [])

        # Load / update streak
        streak_data = {"current_streak": 0, "longest_streak": 0, "total_sessions": 0}
        if user_id:
            try:
                streak_data = await self._update_streak(user_id)
            except Exception as _se:
                logger.debug(f"Streak update skipped: {_se}")

        # Infer emotional state
        emotional_state, tone_hint = self._infer_emotional_state(user_context)

        # Pick the most relevant goal: lowest progress (excluding completed) or least started
        active_goals = [g for g in goals if g.get('progress', 1.0) < 1.0]
        coaching_goal = None
        next_step_name = None
        coaching_goal_id = None
        recently_progressed_goal = None

        if active_goals:
            # Detect a goal with recent strong progress (for celebration)
            for g in active_goals:
                if g.get('progress', 0) >= 0.5 and g.get('progress', 0) < 1.0:
                    recently_progressed_goal = g
                    break

            coaching_goal = min(active_goals, key=lambda g: g.get('progress', 0.0))
            coaching_goal_id = coaching_goal.get('id')
            # Find first uncompleted step
            for step in coaching_goal.get('steps', []):
                if isinstance(step, dict) and not step.get('completed', False):
                    next_step_name = step.get('text')
                    break

        goal_summary = (
            "; ".join(f"{g['title']} ({g.get('progress', 0):.0%} done)" for g in goals)
            if goals
            else "no active goals yet"
        )
        name_part = f" for {display_name}" if display_name else ""

        DOMAIN_COACHING = {
            "iVive": "Focus on personal growth, inner work, self-care, and identity. Coach the user toward becoming who they want to be.",
            "Eviva": "Focus on contribution, purpose, meaningful work, and the impact the user is having or could have. Coach toward outward gift.",
            "Aventi": "Focus on joy, experiences, play, culture, and aliveness. Coach the user to embrace and create more meaningful free time.",
        }
        domain_hint = DOMAIN_COACHING.get(domain, DOMAIN_COACHING["iVive"])

        next_step_hint = f"\n- Next uncompleted step on this goal: \"{next_step_name}\"" if next_step_name else ""
        focus_goal_hint = f"\n- Focus goal: \"{coaching_goal['title']}\" ({coaching_goal.get('progress', 0):.0%} done){next_step_hint}" if coaching_goal else ""

        # Streak context
        streak_hint = ""
        if streak_data["current_streak"] >= 7:
            streak_hint = f"\n- STREAK: {streak_data['current_streak']} consecutive days of coaching! Celebrate this."
        elif streak_data["current_streak"] >= 3:
            streak_hint = f"\n- Streak: {streak_data['current_streak']} days in a row. Build momentum."
        elif streak_data["total_sessions"] > 0 and streak_data["current_streak"] == 1:
            streak_hint = f"\n- Returning after a break (total sessions: {streak_data['total_sessions']}). Welcome back warmly."

        # Celebration hint
        celebration_hint = ""
        if recently_progressed_goal and emotional_state in ("thriving", "energised", "balanced"):
            celebration_hint = f"\n- CELEBRATE: '{recently_progressed_goal['title']}' is {recently_progressed_goal.get('progress', 0):.0%} done. Acknowledge this win."

        # Ora lessons relevant to coaching
        lessons_hint = ""
        if ora_lessons:
            lessons_hint = f"\n\nOra has learned these lessons from past sessions:\n" + "\n".join(f"  - {l}" for l in ora_lessons[:3])

        # Integration C: CBT/ACT mode detection
        cbt_mode = self._detect_cbt_act_mode(emotional_state, user_context)

        # Coaching type selection: CBT/ACT-aware
        COACHING_TYPE_GUIDE = {
            "thriving": "challenge or celebration — push them to the next level",
            "energised": "challenge — give them something bold and exciting to act on",
            "low_energy": "reset — something gentle, restorative, short",
            "disengaged": "reflection — spark curiosity with an open-ended question",
            "struggling": "reflection or reset — lead with compassion",
            "balanced": "accountability or reflection — standard coaching rhythm",
        }
        coaching_type_hint = COACHING_TYPE_GUIDE.get(emotional_state, "reflection")

        # CBT/ACT technique instructions for the prompt
        CBT_ACT_INSTRUCTIONS = {
            "cbt_thought_record": """Use CBT Thought Record structure:
  1. Situation: what is happening right now?
  2. Automatic thought: what is the unhelpful thought?
  3. Emotion: what feeling does that thought produce?
  4. Evidence for/against: what facts support or contradict the thought?
  5. Balanced thought: what is a more realistic, compassionate version?
  Map these 5 steps into the title, prompt, and reflection_prompts.
  Coaching type should be 'reflection'.""",
            "behavioral_activation": """Use Behavioral Activation:
  - Identify one small, achievable activity aligned with the user's stated values.
  - Make it concrete (what, when, how long).
  - Ask: "When exactly will you do this?" — include a scheduling prompt.
  - Coaching type should be 'challenge'.""",
            "act_values": """Use ACT Values Clarification:
  - Ask "What matters most to you in [domain]?"
  - Ask "If you were living fully aligned with your values, what would look different?"
  - Help the user distinguish between values (directions) and goals (destinations).
  - Coaching type should be 'reflection'.""",
            "solution_focused": """Use Solution-Focused Questions (Rocky.ai pattern):
  - "What's working, even a little?"
  - "What would a 10% improvement look like?"
  - "What's one thing you could do in the next 24 hours?"
  Keep it practical, forward-looking, and small-step-oriented.
  Coaching type should be 'accountability'.""",
            "standard": """Use standard warm accountability coaching:
  Reference their specific goals, celebrate any wins, hold them accountable
  without pressure. Be direct and genuine.""",
        }
        cbt_act_technique = CBT_ACT_INSTRUCTIONS.get(cbt_mode, CBT_ACT_INSTRUCTIONS["standard"])

        prompt = f"""You are Ora — the supreme intelligence layer in this person's life, like JARVIS to Iron Man.
You are simultaneously their best coach, most trusted advisor, and most capable assistant.
You are warm, proactive, and relentlessly specific. You never use generic platitudes.
Every message references their actual goals, steps, emotional state, and world context.

You are trained in CBT (Cognitive Behavioral Therapy) and ACT (Acceptance & Commitment Therapy).
Your coaching is grounded in evidence-based psychology, not motivational fluff.

## Coaching Techniques Available
1. CBT Thought Records: Situation → Automatic Thought → Emotion → Evidence For/Against → Balanced Thought
2. Behavioral Activation: Identify a small activity aligned with values. Schedule it concretely.
3. ACT Values Clarification: What matters most? What would living aligned look like?
4. Solution-Focused Questions: What's working? What would 10% improvement look like? One action in 24h?

## Session for{name_part}

### User Context
- Emotional state: {emotional_state} — {tone_hint}
- What they want (I want to...): {goal_summary}{focus_goal_hint}{streak_hint}{celebration_hint}
- Fulfilment score: {fulfilment:.2f}/1.0
- Recent screen ratings: {recent_ratings[-5:] if recent_ratings else "no history"}
- Domain: {domain} — {domain_hint}
- Preferred coaching type this session: {coaching_type_hint}{lessons_hint}

### Selected Technique: {cbt_mode}
{cbt_act_technique}

## Output Format (JSON only, no markdown)
{{
  "title": "6-10 word question or statement — specific to their goals and the CBT/ACT technique",
  "prompt": "2-3 sentences of genuine, technique-grounded coaching insight. Reference specific goal/step. NOT generic.",
  "reflection_prompts": ["3 concrete sentence-starters matching the selected technique"],
  "cta": "action button text (2-5 words — specific to the technique)",
  "goal_id": "{coaching_goal_id or 'null'}",
  "coaching_type": "one of: reflection | accountability | challenge | celebration | reset",
  "domain": "{domain}",
  "streak_message": "optional short streak callout (null if streak < 3)",
  "technique_used": "{cbt_mode}"
}}

Be specific. Be real. Ground every word in the selected CBT/ACT technique.
Avoid 'journey', 'transformation', 'potential', or any other life-coaching clichés.
Return ONLY valid JSON."""

        try:
            response = await self.openai.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": "You are Ora, a direct and effective AI life coach. Return only valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
                max_tokens=500,
                response_format={"type": "json_object"},
            )
            coaching_data = json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.warning(f"OpenAI coaching failed, using mock: {e}")
            return self._generate_mock(user_context, variant)

        return self._build_spec(coaching_data, variant)

    def _generate_mock(
        self, user_context: Dict[str, Any], variant: str
    ) -> Dict[str, Any]:
        import hashlib
        uid = user_context.get("user_id", "anon")
        ts = str(int(datetime.now(timezone.utc).timestamp() / 7200))
        idx = int(hashlib.md5(f"{uid}{ts}coaching".encode()).hexdigest(), 16) % len(
            MOCK_COACHING_SESSIONS
        )
        coaching = dict(MOCK_COACHING_SESSIONS[idx])

        # Inject goal context into the mock
        goals = user_context.get("active_goals", [])
        active_goals = [g for g in goals if g.get('progress', 1.0) < 1.0]
        if active_goals:
            coaching_goal = min(active_goals, key=lambda g: g.get('progress', 0.0))
            coaching["goal_id"] = coaching_goal.get("id")
            # Find first uncompleted step
            for step in coaching_goal.get('steps', []):
                if isinstance(step, dict) and not step.get('completed', False):
                    next_step = step.get('text')
                    if next_step:
                        coaching["prompt"] = (
                            coaching["prompt"] + f" Your next step on '{coaching_goal['title']}': {next_step}."
                        )
                    break

        return self._build_spec(coaching, variant, is_mock=True)

    def _build_spec(
        self,
        coaching: Dict[str, Any],
        variant: str,
        is_mock: bool = False,
    ) -> Dict[str, Any]:
        reflection_prompts = coaching.get("reflection_prompts", [])
        goal_id = coaching.get("goal_id")

        coaching_type = coaching.get("coaching_type", "reflection")
        streak_message = coaching.get("streak_message")

        # Coaching type badge colours
        TYPE_COLORS = {
            "reflection": "#8b5cf6",
            "accountability": "#f59e0b",
            "challenge": "#ef4444",
            "celebration": "#10b981",
            "reset": "#6366f1",
        }
        badge_color = TYPE_COLORS.get(coaching_type, "#8b5cf6")

        type_labels = {
            "reflection": "Reflection",
            "accountability": "Accountability",
            "challenge": "Challenge",
            "celebration": "Celebration 🎉",
            "reset": "Reset",
        }
        type_label = type_labels.get(coaching_type, "Coaching")

        components = [
            {
                "type": "section_header",
                "text": type_label,
                "style": "subtitle",
                "color": badge_color,
            },
        ]

        # Show streak callout if present
        if streak_message and isinstance(streak_message, str) and streak_message.lower() not in ("null", "", "none"):
            components.append({
                "type": "streak_banner",
                "text": streak_message,
                "color": "#f59e0b",
                "icon": "🔥",
            })

        components += [
            {
                "type": "headline",
                "text": coaching.get("title", "Let's check in"),
                "style": "large_bold",
            },
            {
                "type": "body_text",
                "text": coaching.get("prompt", ""),
            },
        ]

        # Add reflection prompt inputs
        for i, rp in enumerate(reflection_prompts[:3]):
            components.append(
                {
                    "type": "text_input_prompt",
                    "label": rp,
                    "placeholder": "Write your thoughts...",
                    "id": f"reflection_{i}",
                }
            )

        # Primary CTA
        action = {"type": "next_screen", "context": "coaching_continue"}
        if goal_id:
            action = {
                "type": "goal_update",
                "goal_id": goal_id,
                "context": "coaching_check_in",
            }

        components.append(
            {
                "type": "action_button",
                "label": coaching.get("cta", "Continue"),
                "action": action,
            }
        )

        # Always include a navigation button to the Goals screen
        components.append(
            {
                "type": "action_button",
                "label": "View Goals →",
                "style": "secondary",
                "action": {
                    "type": "navigate",
                    "context": "goals",
                },
            }
        )

        return {
            "type": "coaching_session",
            "layout": "scroll",
            "components": components,
            "feedback_overlay": {
                "type": "star_rating",
                "position": "bottom_right",
                "always_visible": True,
            },
            "metadata": {
                "agent": self.AGENT_NAME,
                "variant": variant,
                "coaching_type": coaching_type,
                "domain": coaching.get("domain", "iVive"),
                "is_mock": is_mock,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "has_streak": bool(streak_message and str(streak_message).lower() not in ("null", "", "none")),
            },
        }
