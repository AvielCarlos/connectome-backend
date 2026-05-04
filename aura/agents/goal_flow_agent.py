"""
GoalFlowAgent — Aura's Dynamic Goal Achievement Engine

The key insight: goal flows should NOT be static checklists.
They are a living, adaptive stream of the right next action,
surfaced at the right moment — felt as discovery, not task management.

Philosophy:
- Never show a to-do list unless the user asks for one
- Surface the ONE most valuable next action as a card in the feed
- Adapt based on what the user actually does, not what they plan to do
- For experiential goals: find real opportunities
- For skill goals: sequence micro-actions that build momentum
- For life goals: coaching + reflection cards
"""

import logging
import json
import uuid
import random
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from core.config import settings
from core.database import fetch, fetchrow, execute, fetchval

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Goal flow templates — fallback when AI is unavailable
# ---------------------------------------------------------------------------

FALLBACK_CARDS = {
    "experience": {
        "local_opportunity": {
            "title": "Ready to make it real?",
            "body": "Here's the step most people skip: find an actual place near you. {goal} is more possible than you think.",
            "cta": "Find options near me",
        },
        "preparation": {
            "title": "What to expect on your first try",
            "body": "The unknown is usually what holds people back. Here's what {goal} actually looks like step by step.",
            "cta": "Read the guide",
        },
        "social_proof": {
            "title": "Others who did this said...",
            "body": "\"The hardest part was just deciding to show up. After that it takes on its own momentum.\"",
            "cta": "Read their stories",
        },
        "booking_cta": {
            "title": "You've been thinking about this long enough",
            "body": "Booking {goal} takes about 3 minutes. The thinking has already taken longer.",
            "cta": "Book now",
        },
        "re_engagement": {
            "title": "Still thinking about {goal}?",
            "body": "You set this goal because something in you wanted it. That thing is still there. What would it mean to actually do this?",
            "cta": "Let's pick this up",
        },
    },
    "skill": {
        "quick_win": {
            "title": "10 minutes. That's all.",
            "body": "You don't need a plan. You need a first win. Try {goal} for just 10 minutes right now — momentum beats motivation.",
            "cta": "Start the 10-minute version",
        },
        "daily_practice": {
            "title": "The one habit that makes {goal} stick",
            "body": "10 minutes every day beats 2 hours on weekends. Here's the daily practice that gets results.",
            "cta": "Set up my practice",
        },
        "progress_tracker": {
            "title": "You've been at this. Here's what's changed.",
            "body": "Progress on {goal} is real even when it doesn't feel like it. Let's look at the evidence.",
            "cta": "See my progress",
        },
        "community": {
            "title": "Who else is doing {goal}?",
            "body": "Learning alongside others accelerates everything. Here's where people like you are sharing progress.",
            "cta": "Find my people",
        },
        "re_engagement": {
            "title": "Remember why you wanted {goal}?",
            "body": "Skills have seasons. If you stepped away, that's ok. The part of you that wants this hasn't gone anywhere.",
            "cta": "Pick it back up",
        },
    },
    "life": {
        "reflection_prompt": {
            "title": "One question worth sitting with",
            "body": "About {goal}: what would feel different in your day-to-day life if this was already true?",
            "cta": "Write your answer",
        },
        "perspective_shift": {
            "title": "A different way to see {goal}",
            "body": "Most people chase the outcome and miss the process. What if {goal} was already happening in small ways?",
            "cta": "Explore this",
        },
        "small_experiment": {
            "title": "A 7-day experiment for {goal}",
            "body": "Don't commit to a life change. Commit to 7 days of one small thing. Here's what the research says actually works.",
            "cta": "Try the experiment",
        },
        "insight": {
            "title": "What Aura has noticed about {goal}",
            "body": "People who successfully pursue {goal} have one thing in common — and it's probably not what you expect.",
            "cta": "See the insight",
        },
        "re_engagement": {
            "title": "{goal} — still calling?",
            "body": "Life goals don't expire. They wait. What would one small act in this direction look like today?",
            "cta": "Take one step",
        },
    },
    "project": {
        "clarity_prompt": {
            "title": "Before the first step: clarity",
            "body": "The #1 killer of projects is fuzzy vision. Finish this sentence: '{goal} is done when...'",
            "cta": "Define my finish line",
        },
        "action_card": {
            "title": "The smallest possible next step",
            "body": "On {goal}: forget the full plan. What's the one thing you could do in the next hour that moves this forward?",
            "cta": "Do the next thing",
        },
        "obstacle_card": {
            "title": "What's actually in the way?",
            "body": "On {goal}, most obstacles feel external but are internal. Let's name the real one.",
            "cta": "Identify the block",
        },
        "resource": {
            "title": "What others use for {goal}",
            "body": "You don't need to figure this out alone. Here's what works for people who've done this before.",
            "cta": "See the toolkit",
        },
        "re_engagement": {
            "title": "{goal} — where did you leave off?",
            "body": "Projects stall. That's not failure. Let's find the thread and pick it up.",
            "cta": "Restart from here",
        },
    },
}

# Flow stage progression order per archetype
FLOW_STAGES = {
    "experience": ["find_opportunity", "prepare", "experience", "reflect", "amplify"],
    "skill": ["first_win", "habit_anchor", "consistency", "milestone", "mastery"],
    "life": ["explore", "clarify", "small_action", "reflection", "evolution"],
    "project": ["clarity", "first_step", "momentum", "obstacle_remove", "completion"],
}

# Card type per flow stage per archetype
STAGE_CARD_TYPES = {
    "experience": {
        "find_opportunity": "local_opportunity",
        "prepare": "preparation",
        "experience": "social_proof",
        "reflect": "social_proof",
        "amplify": "booking_cta",
    },
    "skill": {
        "first_win": "quick_win",
        "habit_anchor": "daily_practice",
        "consistency": "progress_tracker",
        "milestone": "community",
        "mastery": "community",
    },
    "life": {
        "explore": "reflection_prompt",
        "clarify": "perspective_shift",
        "small_action": "small_experiment",
        "reflection": "insight",
        "evolution": "reflection_prompt",
    },
    "project": {
        "clarity": "clarity_prompt",
        "first_step": "action_card",
        "momentum": "action_card",
        "obstacle_remove": "obstacle_card",
        "completion": "resource",
    },
}


class GoalFlowAgent:
    """
    Aura's goal achievement engine.

    Never shows a to-do list. Surfaces ONE perfect next action
    at the right moment, felt as discovery.
    """

    GOAL_ARCHETYPES = {
        "experience": {
            "keywords": [
                "skydiving", "jump", "surf", "concert", "travel", "trip", "visit",
                "try", "taste", "experience", "festival", "event", "race", "marathon",
                "climb", "hike", "dive", "bungee", "camping", "safari",
            ],
            "flow_pattern": "find_opportunity → prepare → experience → reflect → amplify",
            "key_question": "What's the one thing stopping you from doing this today?",
            "card_types": ["local_opportunity", "preparation", "social_proof", "booking_cta"],
        },
        "skill": {
            "keywords": [
                "learn", "study", "practice", "get fit", "fitness", "workout",
                "code", "program", "guitar", "piano", "music", "language", "cook",
                "speak", "write", "draw", "paint", "dance", "martial arts", "yoga",
            ],
            "flow_pattern": "first_win → habit_anchor → consistency → milestone → mastery",
            "key_question": "What does success look like in 30 days?",
            "card_types": ["quick_win", "daily_practice", "progress_tracker", "community"],
        },
        "life": {
            "keywords": [
                "happier", "happy", "purpose", "meaning", "relationships", "friends",
                "better person", "confidence", "anxiety", "stress", "peace", "mindful",
                "grateful", "love", "connect", "belong", "fulfil",
            ],
            "flow_pattern": "explore → clarify → small_action → reflection → evolution",
            "key_question": "What would feel different if this was already true?",
            "card_types": ["reflection_prompt", "perspective_shift", "small_experiment", "insight"],
        },
        "project": {
            "keywords": [
                "business", "startup", "book", "write", "build", "create", "launch",
                "app", "product", "company", "move", "relocate", "career", "job",
                "side project", "blog", "podcast", "channel", "website",
            ],
            "flow_pattern": "clarity → first_step → momentum → obstacle_remove → completion",
            "key_question": "What's the smallest version of this you could start today?",
            "card_types": ["clarity_prompt", "action_card", "obstacle_card", "resource"],
        },
    }

    def __init__(self, openai_client=None):
        self.openai = openai_client

    # -----------------------------------------------------------------------
    # Goal classification
    # -----------------------------------------------------------------------

    async def classify_goal(self, goal_text: str) -> str:
        """Classify goal text into one of the four archetypes."""
        goal_lower = goal_text.lower()

        # Keyword scoring
        scores = {archetype: 0 for archetype in self.GOAL_ARCHETYPES}
        for archetype, data in self.GOAL_ARCHETYPES.items():
            for kw in data["keywords"]:
                if kw in goal_lower:
                    scores[archetype] += 1

        best = max(scores, key=scores.get)
        if scores[best] > 0:
            return best

        # AI classification fallback
        if self.openai:
            try:
                resp = await self.openai.chat.completions.create(
                    model="gpt-4o-mini",
                    max_tokens=10,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Classify the goal into exactly one of: "
                                "experience, skill, life, project. "
                                "Reply with only the single word."
                            ),
                        },
                        {"role": "user", "content": goal_text},
                    ],
                )
                result = resp.choices[0].message.content.strip().lower()
                if result in self.GOAL_ARCHETYPES:
                    return result
            except Exception as e:
                logger.warning(f"GoalFlowAgent.classify_goal AI failed: {e}")

        # Default
        return "experience"

    # -----------------------------------------------------------------------
    # Flow card generation
    # -----------------------------------------------------------------------

    async def generate_next_flow_card(
        self,
        user_id: str,
        goal_id: str,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Generate the SINGLE best next card for a user's goal.

        Rules:
        - < 1h since goal set → first quick-win card
        - 3+ days without engagement → re-engagement card
        - 3+ highly-rated goal cards → advance to next flow stage
        - 2+ skipped goal cards → pivot the approach
        - NEVER show same card type twice in a row for same goal
        """
        goal = await fetchrow(
            "SELECT * FROM goals WHERE id = $1::uuid", goal_id
        )
        if not goal:
            return None

        goal_dict = dict(goal)
        goal_text = goal_dict.get("title") or goal_dict.get("description") or "your goal"
        archetype = goal_dict.get("archetype") or await self.classify_goal(goal_text)
        flow_stage = goal_dict.get("flow_stage") or FLOW_STAGES[archetype][0]
        last_card_type = goal_dict.get("last_card_type")
        last_engaged_at = goal_dict.get("last_engaged_at")
        goal_set_at = goal_dict.get("created_at")

        now = datetime.now(timezone.utc)

        # Persist archetype if missing
        if not goal_dict.get("archetype"):
            try:
                await execute(
                    "UPDATE goals SET archetype = $1 WHERE id = $2::uuid",
                    archetype, goal_id
                )
            except Exception:
                pass

        # --- Timing rules ---
        time_since_set = (now - goal_set_at.replace(tzinfo=timezone.utc) if goal_set_at else timedelta(hours=2))
        days_since_engage = None
        if last_engaged_at:
            ts = last_engaged_at.replace(tzinfo=timezone.utc) if hasattr(last_engaged_at, 'tzinfo') and last_engaged_at.tzinfo is None else last_engaged_at
            days_since_engage = (now - ts).total_seconds() / 86400

        # Re-engagement if dormant 3+ days
        if days_since_engage is not None and days_since_engage >= 3:
            return await self._build_card(
                goal_text, archetype, "re_engagement",
                goal_id, flow_stage, user_context
            )

        # First hour → first card
        if time_since_set < timedelta(hours=1):
            first_stage = FLOW_STAGES[archetype][0]
            card_type = STAGE_CARD_TYPES[archetype][first_stage]
            return await self._build_card(
                goal_text, archetype, card_type,
                goal_id, first_stage, user_context
            )

        # Check recent goal interactions for this user+goal
        recent_interactions = await fetch(
            """
            SELECT i.rating, ss.spec->>'goal_card_type' as card_type
            FROM interactions i
            JOIN screen_specs ss ON i.screen_spec_id = ss.id
            WHERE i.user_id = $1::uuid
              AND ss.spec->>'goal_id' = $2
            ORDER BY i.created_at DESC
            LIMIT 5
            """,
            user_id, goal_id,
        )

        high_ratings = sum(1 for r in recent_interactions if r["rating"] and r["rating"] >= 4)
        skipped = sum(1 for r in recent_interactions if r["rating"] and r["rating"] <= 2)

        # Advance stage if on a roll
        if high_ratings >= 3:
            stages = FLOW_STAGES[archetype]
            current_idx = stages.index(flow_stage) if flow_stage in stages else 0
            next_idx = min(current_idx + 1, len(stages) - 1)
            flow_stage = stages[next_idx]
            try:
                await execute(
                    "UPDATE goals SET flow_stage = $1 WHERE id = $2::uuid",
                    flow_stage, goal_id
                )
            except Exception:
                pass

        # Pivot approach if skipping
        if skipped >= 2:
            card_types = self.GOAL_ARCHETYPES[archetype]["card_types"]
            # Choose a card type that hasn't been shown recently
            shown = {r["card_type"] for r in recent_interactions if r["card_type"]}
            available = [ct for ct in card_types if ct not in shown]
            card_type = available[0] if available else card_types[-1]
        else:
            card_type = STAGE_CARD_TYPES[archetype].get(flow_stage, "re_engagement")

        # Don't repeat last card type
        if card_type == last_card_type:
            card_types = self.GOAL_ARCHETYPES[archetype]["card_types"]
            idx = card_types.index(card_type) if card_type in card_types else 0
            card_type = card_types[(idx + 1) % len(card_types)]

        return await self._build_card(
            goal_text, archetype, card_type,
            goal_id, flow_stage, user_context
        )

    async def _build_card(
        self,
        goal_text: str,
        archetype: str,
        card_type: str,
        goal_id: str,
        flow_stage: str,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build a screen_spec dict for a goal flow card."""

        # Try AI generation first
        if self.openai:
            try:
                return await self._ai_generate_card(
                    goal_text, archetype, card_type, goal_id, flow_stage, user_context
                )
            except Exception as e:
                logger.warning(f"GoalFlowAgent AI card generation failed: {e}")

        # Fallback template
        templates = FALLBACK_CARDS.get(archetype, FALLBACK_CARDS["experience"])
        template = templates.get(card_type, templates.get("re_engagement", {}))

        title = template.get("title", "Your next step").replace("{goal}", goal_text)
        body = template.get("body", "Keep going.").replace("{goal}", goal_text)
        cta = template.get("cta", "Continue")

        # For experience archetype, try to find local opportunities
        location = (user_context or {}).get("city", "")
        local_info = ""
        if archetype == "experience" and card_type == "local_opportunity" and location:
            opps = await self.find_local_opportunities(goal_text, location)
            if opps:
                opp = opps[0]
                local_info = f"\n\n📍 {opp.get('title', '')} — {opp.get('distance', 'Nearby')}"
                if opp.get("url"):
                    local_info += f" [Details]({opp['url']})"

        screen_id = str(uuid.uuid4())
        return {
            "screen_id": screen_id,
            "type": "goal_flow_card",
            "layout": "scroll",
            "goal_id": goal_id,
            "goal_card_type": card_type,
            "flow_stage": flow_stage,
            "archetype": archetype,
            "components": [
                {
                    "type": "hero_text",
                    "text": title,
                    "style": "bold",
                },
                {
                    "type": "body_text",
                    "text": body + local_info,
                },
                {
                    "type": "cta_button",
                    "label": cta,
                    "action": "goal_cta",
                    "goal_id": goal_id,
                },
            ],
            "feedback_overlay": {
                "type": "star_rating",
                "position": "bottom_right",
                "always_visible": True,
            },
            "metadata": {
                "agent": "GoalFlowAgent",
                "archetype": archetype,
                "card_type": card_type,
                "flow_stage": flow_stage,
                "goal_id": goal_id,
                "is_goal_card": True,
            },
        }

    async def _ai_generate_card(
        self,
        goal_text: str,
        archetype: str,
        card_type: str,
        goal_id: str,
        flow_stage: str,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Generate a card using OpenAI, with Aura's voice."""
        archetype_data = self.GOAL_ARCHETYPES[archetype]
        location = (user_context or {}).get("city", "")

        system_prompt = (
            "You are Aura — a life intelligence engine that helps people achieve their goals. "
            "You are generating a single card for a user's goal feed. "
            "Your voice is warm, direct, and non-preachy. You never show a to-do list. "
            "You surface the ONE most valuable next action or insight. "
            "Respond with a JSON object only, no markdown. "
            f"Archetype: {archetype}. Card type: {card_type}. Flow stage: {flow_stage}. "
            f"Goal flow philosophy: {archetype_data['flow_pattern']}"
        )

        user_prompt = (
            f"User goal: \"{goal_text}\"\n"
            f"Location: {location or 'unknown'}\n"
            f"Card type needed: {card_type}\n"
            f"Generate a JSON card with fields: title, body (2-3 sentences), cta (button label). "
            f"Make it feel personal, not generic. No fluff."
        )

        resp = await self.openai.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=300,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

        data = json.loads(resp.choices[0].message.content)
        screen_id = str(uuid.uuid4())

        # For experience archetype, try to inject local opportunity
        local_info = ""
        if archetype == "experience" and card_type == "local_opportunity" and location:
            opps = await self.find_local_opportunities(goal_text, location)
            if opps:
                opp = opps[0]
                local_info = f"\n\n📍 {opp.get('title', '')} — {opp.get('distance', 'Nearby')}"
                if opp.get("url"):
                    local_info += f" — [Book here]({opp['url']})"

        return {
            "screen_id": screen_id,
            "type": "goal_flow_card",
            "layout": "scroll",
            "goal_id": goal_id,
            "goal_card_type": card_type,
            "flow_stage": flow_stage,
            "archetype": archetype,
            "components": [
                {
                    "type": "hero_text",
                    "text": data.get("title", "Your next step"),
                    "style": "bold",
                },
                {
                    "type": "body_text",
                    "text": data.get("body", "") + local_info,
                },
                {
                    "type": "cta_button",
                    "label": data.get("cta", "Continue"),
                    "action": "goal_cta",
                    "goal_id": goal_id,
                },
            ],
            "feedback_overlay": {
                "type": "star_rating",
                "position": "bottom_right",
                "always_visible": True,
            },
            "metadata": {
                "agent": "GoalFlowAgent",
                "archetype": archetype,
                "card_type": card_type,
                "flow_stage": flow_stage,
                "goal_id": goal_id,
                "is_goal_card": True,
            },
        }

    # -----------------------------------------------------------------------
    # Feed injection
    # -----------------------------------------------------------------------

    async def inject_into_feed(
        self,
        user_id: str,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Called by the main feed generation.
        Returns a goal card to inject into the feed (1 per every 5 cards).
        Picks: most recently set active goal (or least progressed).
        """
        try:
            active_goals = await fetch(
                """
                SELECT id, title, description, progress, archetype,
                       flow_stage, last_card_type, last_engaged_at, created_at
                FROM goals
                WHERE user_id = $1::uuid AND status = 'active'
                ORDER BY created_at DESC
                LIMIT 5
                """,
                user_id,
            )
        except Exception as e:
            logger.warning(f"GoalFlowAgent.inject_into_feed: DB query failed: {e}")
            return None

        if not active_goals:
            return None

        # Pick most recent goal (first in list), or least progressed
        goal = dict(active_goals[0])
        goal_id = str(goal["id"])

        try:
            card = await self.generate_next_flow_card(user_id, goal_id, user_context)
            if card:
                # Track that we showed a goal card
                await execute(
                    "UPDATE goals SET last_card_type = $1, last_engaged_at = NOW() WHERE id = $2::uuid",
                    card.get("goal_card_type"), goal_id,
                )
            return card
        except Exception as e:
            logger.warning(f"GoalFlowAgent.inject_into_feed: card generation failed: {e}")
            return None

    # -----------------------------------------------------------------------
    # Completion detection
    # -----------------------------------------------------------------------

    async def detect_completion(self, user_id: str, goal_id: str) -> bool:
        """
        Heuristic: goal is complete if:
        - User explicitly says so in Aura chat (aura_conversations check)
        - User rated 10+ goal-related cards at ≥4
        - 30 days have passed with consistent engagement
        """
        try:
            # Check high-rated goal cards
            high_rated = await fetchval(
                """
                SELECT COUNT(*)
                FROM interactions i
                JOIN screen_specs ss ON i.screen_spec_id = ss.id
                WHERE i.user_id = $1::uuid
                  AND ss.spec->>'goal_id' = $2
                  AND i.rating >= 4
                """,
                user_id, goal_id,
            )
            if high_rated and high_rated >= 10:
                return True

            # Check explicit completion mention in conversations
            mentions = await fetchval(
                """
                SELECT COUNT(*) FROM aura_conversations
                WHERE user_id = $1::uuid
                  AND role = 'user'
                  AND (
                    message ILIKE '%completed%'
                    OR message ILIKE '%finished%'
                    OR message ILIKE '%did it%'
                    OR message ILIKE '%done%'
                  )
                  AND created_at > NOW() - INTERVAL '7 days'
                """,
                user_id,
            )
            if mentions and mentions >= 1:
                return True

            # Check 30-day consistent engagement
            goal_row = await fetchrow(
                "SELECT created_at, last_engaged_at FROM goals WHERE id = $1::uuid", goal_id
            )
            if goal_row:
                now = datetime.now(timezone.utc)
                created = goal_row["created_at"]
                last = goal_row["last_engaged_at"]
                if created:
                    age_days = (now - created.replace(tzinfo=timezone.utc)).days
                    if age_days >= 30 and last:
                        days_since = (now - last.replace(tzinfo=timezone.utc)).days
                        if days_since <= 7:  # still active at 30-day mark
                            return True

        except Exception as e:
            logger.warning(f"GoalFlowAgent.detect_completion: {e}")

        return False

    # -----------------------------------------------------------------------
    # Local opportunity search
    # -----------------------------------------------------------------------

    async def find_local_opportunities(
        self, goal_text: str, user_location: str
    ) -> List[Dict[str, Any]]:
        """
        For experiential goals: search for real local opportunities.
        Returns top 3 with title, url, price, distance.
        """
        from datetime import date

        current_month_year = date.today().strftime("%B %Y")
        query = f"{goal_text} near {user_location} {current_month_year}"

        try:
            from web_search import search  # type: ignore
            results = await search(query, count=3)
            opportunities = []
            for r in results[:3]:
                opportunities.append({
                    "title": r.get("title", goal_text),
                    "url": r.get("url", ""),
                    "price": r.get("price", ""),
                    "distance": "Nearby",
                    "snippet": r.get("snippet", ""),
                })
            return opportunities
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"GoalFlowAgent.find_local_opportunities web_search: {e}")

        # Fallback: construct a Google search URL
        search_url = f"https://www.google.com/search?q={goal_text.replace(' ', '+')}+near+{user_location.replace(' ', '+')}"
        return [
            {
                "title": f"{goal_text.title()} near {user_location}",
                "url": search_url,
                "price": "",
                "distance": "Nearby",
                "snippet": f"Find {goal_text} options near {user_location}",
            }
        ]


# Module-level singleton
_goal_flow_agent: Optional[GoalFlowAgent] = None


def get_goal_flow_agent(openai_client=None) -> GoalFlowAgent:
    global _goal_flow_agent
    if _goal_flow_agent is None:
        _goal_flow_agent = GoalFlowAgent(openai_client)
    return _goal_flow_agent
