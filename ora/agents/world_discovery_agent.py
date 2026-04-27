"""
WorldDiscoveryAgent — Ora's World-Aware Life Suggestion Engine

Generates "life suggestion" cards driven by real-world signals:
  • Right now     — weather + time of day
  • On this day   — Wikipedia historical moment
  • Try something — random micro-experience from a rich domain pool
  • World pulse   — trending topic (HackerNews)
  • Seasonal      — season + moon phase

Card generation:
  1. If OPENAI_API_KEY available → use GPT-4o for fresh, poetic voice
  2. Fallback → rich template pool with 8-10 variants per signal combination

All cards are tagged with is_world_aware=True and the originating signal.
"""

import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.config import settings
from ora.agents.world_signal_agent import get_world_signal_agent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Template pools
# ---------------------------------------------------------------------------

WORLD_CARD_TEMPLATES: Dict[str, List[str]] = {
    # ---- Weather + Time combos ----
    "rainy_morning": [
        "Rain taps on the window like it's asking to join you. Make a slow breakfast, something that takes time and smells good.",
        "Rainy mornings are underrated. Nowhere to rush. Let yourself ease into the day like the light does — gradually.",
        "There's something clarifying about rain in the morning. It decides your pace for you. What do you actually want to do today?",
        "The world is quieter in the rain. Use it. Read something you've been putting off, or just sit and think without a screen.",
        "Rainy morning energy: tea, window, the gentle permission to go slow. Take it.",
    ],
    "rainy_afternoon": [
        "The afternoon rain is asking you to pause. Call someone you haven't talked to in a while — just to say hi.",
        "Rainy afternoons are for projects that live in the background: the playlist you never finished, the idea you keep shelving.",
        "When it rains in the afternoon, the city goes indoors and something quieter takes over. Go with it.",
        "Rain in the afternoon means the best version of today is happening indoors. What would make this a good afternoon?",
        "There's a kind of permission in afternoon rain. You're allowed to be unproductive, reflective, or just still.",
    ],
    "rainy_evening": [
        "The rain has a rhythm tonight. Put on something you haven't listened to in years and let yourself feel whatever comes up.",
        "Rainy evenings are for letters you never send. Write one to someone you've been thinking about.",
        "Make something warm — soup, tea, a blanket fort. Your only job tonight is comfort.",
        "Rain in the evening is the world's way of saying: stay in, go deep, be present with wherever you are.",
        "There's a kind of beauty in a rainy evening that asks nothing of you. What would you do if you had no obligations right now?",
        "Tonight calls for candlelight and something that absorbs you — a book, a show, a conversation you've been saving.",
    ],
    "rainy_night": [
        "Rain at night is a lullaby. Let it be the soundtrack to something quiet before you sleep.",
        "The best sleep happens when rain is falling. But first — what do you want to carry into tomorrow?",
        "Rainy nights are for honesty. Journal, or just lie there and let your mind surface what it's been carrying.",
    ],
    "sunny_morning": [
        "The morning light is asking you to move. Go somewhere on foot you've never been.",
        "Eat breakfast outside, even if just for 10 minutes. Let the day start with sun on your face.",
        "Bright mornings deserve bold intentions. What's the one thing you'd do today if you trusted yourself?",
        "The sun came out and so should you. A walk before 10am changes the chemistry of the day.",
        "This morning has the kind of light that makes ordinary things beautiful. Take your coffee somewhere with a view.",
        "Sunny mornings are earned. Don't spend this one inside staring at a screen. Even 20 minutes outside will shift something.",
    ],
    "sunny_afternoon": [
        "It's the kind of afternoon that's wasted indoors. Park, rooftop, bench by water — anywhere outside will do.",
        "Sunny afternoons move differently. What would you regret not doing with this light?",
        "This is peak go-outside weather. You'll thank yourself for taking 30 minutes away from the screen.",
        "The afternoon sun is doing that golden-hour thing already. Go find something to look at.",
    ],
    "sunny_evening": [
        "Golden hour. Go outside now — you've got maybe 45 minutes of magic light.",
        "Sunny evenings are for wandering without destination. Pick a direction and walk.",
        "The best sunsets are often missed because we're looking at screens. Not tonight.",
        "Evenings like this are rare. Take someone with you, or just yourself — both are good.",
    ],
    "cloudy_morning": [
        "Overcast mornings have a soft, diffused energy. Good for thinking, writing, or work that needs focus.",
        "Grey mornings are underrated. Lower your expectations for the day's aesthetic and raise them for its depth.",
        "When the sky is overcast, the invitation is inward. What's been sitting in the back of your mind?",
    ],
    "cloudy_afternoon": [
        "Grey afternoon energy is perfect for the project you've been avoiding. No distractions today.",
        "This kind of afternoon wants something from you. Not movement, just presence. What needs your attention?",
    ],
    "cloudy_evening": [
        "Overcast evenings have a cinematic quality. It's a good night for a film that makes you feel something.",
        "No stars tonight, but the city light reflecting off the clouds has its own kind of beauty. Go out and look up.",
        "This evening is for candlelight and honesty — with yourself or someone you trust.",
    ],
    "cold_morning": [
        "Cold mornings reward those who get up anyway. Steam from coffee, breath in the air, the world before it wakes.",
        "Cold outside, warm coffee — a combination that needs no improvement. Take it slowly.",
        "The cold is bracing in the best way. A short walk in it will make you feel more awake than anything else.",
    ],
    "cold_evening": [
        "Cold nights call for something long-simmering — a stew, a conversation, a project.",
        "Cook something from scratch tonight — no recipe, just intuition and what's in the fridge.",
        "Cold evenings are made for layered blankets and the kind of reading you do when time isn't a factor.",
    ],
    "stormy": [
        "Thunderstorms are nature's do-not-disturb sign. Stay in, go deep on something meaningful.",
        "The storm makes the decision for you: nowhere to be, nothing to do but be here. What does 'here' feel like?",
        "There's something clarifying about a proper storm. Big weather, big thoughts. What are you actually thinking about?",
    ],
    "snowy": [
        "Snow changes the whole city. If you're safe to go out, it's a photographer's dream. If not, it's a perfect day to cook something new.",
        "The snow is doing what snow does — making everything quieter and softer. Let it slow you down too.",
        "Snowy days are rare gifts. Do something you can only do on days like this.",
    ],
    "foggy": [
        "Fog erases the familiar and reveals something stranger. Go outside and let the mystery be a feature.",
        "There's something meditative about fog. It narrows the world to just what's in front of you.",
        "Foggy days feel like living inside a painting that hasn't been finished yet.",
    ],

    # ---- Moon phase ----
    "new_moon": [
        "New moons are for beginnings. Name one thing you want to call in this month.",
        "Write down 3 things you're releasing and 3 things you're inviting. Burn or save — up to you.",
        "The new moon is invisible tonight, but it's there — like every beginning before it's visible.",
        "New moon energy: plant a seed, literal or metaphorical. What's trying to grow in you right now?",
        "Tonight starts a new cycle. What would you do differently if you had a fresh start right now?",
    ],
    "waxing_crescent": [
        "The moon is growing — good time for momentum. What small step could you take toward something that matters?",
        "Waxing crescent: the world is in building mode. What are you building right now, even quietly?",
        "The moon is just a sliver tonight, but it's moving. So are you, even when it doesn't feel like it.",
    ],
    "first_quarter": [
        "Half moon, half-built. What decision have you been putting off that it's time to make?",
        "The first quarter asks for commitment. You've started something — what would it look like to really go for it?",
    ],
    "waxing_gibbous": [
        "Almost full — a time of refinement. What in your life is almost there, and what would it take to finish?",
        "The moon is nearly full. There's an energy of gathering, completing, almost-there. Use it.",
    ],
    "full_moon": [
        "Full moon. Everything feels a little more vivid, a little more electric. What do you want to feel tonight?",
        "Full moons are for celebrating what's been built. What's grown in your life recently that deserves recognition?",
        "The moon is fully lit tonight. Turn off your phone and go find somewhere to look at it.",
        "Full moon energy is loud and clear. What is it you've been quiet about that wants to be said?",
        "Tonight's full moon is asking: what's at its peak right now? In your work, relationships, yourself?",
    ],
    "waning_gibbous": [
        "The moon starts releasing now. What can you let go of that's been taking up too much space?",
        "Waning moon: good time to finish things, not start them. What needs closing?",
    ],
    "last_quarter": [
        "Last quarter moon — the cycle is completing. What lesson from this month are you taking forward?",
        "Letting go isn't the same as giving up. What are you ready to release without regret?",
    ],
    "waning_crescent": [
        "The moon is almost dark — time to rest, reflect, and prepare for what comes next.",
        "Waning crescent: rest is productive. What does your body and mind actually need right now?",
        "The moon is going quiet. It's okay if you go quiet too.",
    ],

    # ---- Season ----
    "spring": [
        "Spring is the season of beginnings disguised as ordinary days. What are you quietly starting?",
        "Everything outside is emerging from something dormant. What's been dormant in you that wants to move?",
        "Spring light has a quality that makes the past feel far away. What do you want the next season to feel like?",
    ],
    "summer": [
        "Summer asks you to be present. Not productive, not planning — just here, in whatever warmth this day offers.",
        "Long days are a luxury. What would you do today if you had two extra hours of light?",
        "Summer is the season of doing things you'll tell stories about. What story are you in right now?",
    ],
    "autumn": [
        "Autumn is the most honest season — everything is beautiful and letting go at the same time.",
        "The trees are showing us how to release with beauty. What are you holding that's ready to fall?",
        "Autumn light has a golden weight to it. Make something in it — a walk, a meal, a conversation.",
    ],
    "winter": [
        "Winter is for depth. Less doing, more being. What do you know about yourself that you learned in a quiet moment?",
        "The world is going inward. You can too. Rest is not nothing — it's preparation.",
        "Cold air and early dark have a way of making warmth feel sacred. What warms you right now?",
    ],

    # ---- On This Day ----
    "on_this_day": [
        "On this day in {year}: {event} — What does that stir in you?",
        "History today: in {year}, {event}. Strange how the past shows up in the present.",
        "On this day in {year}: {event}. If you could send a message back to that moment, what would it say?",
        "History note: in {year}, {event}. What does this era's version of that story look like to you?",
    ],

    # ---- HN trending ----
    "world_pulse": [
        "The internet is buzzing about: \"{topic}\". What's your honest take on how it touches your life?",
        "\"{topic}\" is trending right now. What do you actually think about it — not what you're supposed to think?",
        "People are talking about \"{topic}\" today. Where do you sit in that conversation?",
        "Right now, the world is thinking about \"{topic}\". Does that feel relevant to you, or distant?",
    ],

    # ---- No goals / first visit ----
    "no_goals_first_visit": [
        "Ora doesn't know you yet — and that's kind of exciting. What's the one area of your life that feels most alive right now?",
        "Before goals, there's just life. What happened today that surprised you, even a little?",
        "You don't need a plan to begin. What would you do today if you knew you couldn't fail?",
        "Every great journey starts with a single honest question: what do I actually want?",
        "No pressure, no agenda. Just: what's on your mind today that you haven't told anyone?",
        "Ora's first question isn't about goals. It's about you. What's alive in you right now?",
    ],

    # ---- Try something (micro-experiences by domain) ----
    "try_ivive": [  # inner self / personal growth
        "Spend 10 minutes writing about the version of yourself you want to become. Be specific — not just feelings, but behaviours.",
        "Name one thing you keep telling yourself you'll 'deal with later.' Today is later.",
        "What belief about yourself might not be true anymore? Think about it seriously.",
        "Try 5 minutes of real silence — no podcast, no music, no phone. Just you and whatever comes up.",
        "Write down 3 things you're grateful for that you've never written down before.",
        "Ask someone who knows you well: 'What do you think I'm really good at?' and actually listen.",
        "Rate your life in 5 areas from 1-10: health, relationships, work, purpose, fun. What would moving one number look like?",
        "What did 15-year-old you dream about that you've quietly given up on? Is it actually gone, or just deferred?",
        "Spend 20 minutes on something purely creative — no goal, no audience, no standard. Just make something.",
        "Write a letter to yourself 5 years from now. Be honest about what you're afraid of.",
    ],
    "try_eviva": [  # contribution / community / impact
        "Do something kind for a stranger today — specifically, intentionally, without expecting anything.",
        "Text someone you know who might be struggling. Just check in. It costs nothing and means a lot.",
        "What cause or community could use an hour of your attention this week? Name it, then act.",
        "Reach out to someone older than you and ask them what they wish they'd known at your age.",
        "What's something you know well enough to teach? Find someone who'd benefit from knowing it.",
        "Share something you've learned recently — a thought, an insight, an article — with someone who'd find it valuable.",
        "Do the thing you've been meaning to do for a community but keep postponing. Even a small version of it.",
        "Nominate someone in your life for a compliment they'd never expect. Give it genuinely.",
        "What's something broken in your neighbourhood or community you could help fix — even a little?",
        "Leave a review for a small local business you love. It takes 3 minutes and matters more than you think.",
    ],
    "try_aventi": [  # experience / adventure / joy
        "Go somewhere in your city you've never been. No plan — just pick a direction and start walking.",
        "Eat at a restaurant you know nothing about. Pick a cuisine you've never tried.",
        "Plan something small to look forward to this week — even if it's just a walk in a new park.",
        "Watch a film from a country you've never visited. Let it show you something.",
        "Try cooking something you've never made before — from memory, intuition, or a recipe from your past.",
        "Call or visit somewhere you used to love but haven't been in years.",
        "Do one physical thing you haven't done in a long time — swim, climb, dance, run, jump.",
        "Go somewhere beautiful purely to sit and exist in it. No phone, no agenda.",
        "Find live music tonight — even if it's just a busker — and stop to actually listen.",
        "Revisit something from your past that brought you joy. A game, a show, a food, a place.",
    ],
}


# ---------------------------------------------------------------------------
# WorldDiscoveryAgent
# ---------------------------------------------------------------------------

class WorldDiscoveryAgent:
    """
    Generates world-aware life suggestion cards for Ora.

    Card types:
      - right_now    : weather + time
      - on_this_day  : Wikipedia historical event
      - try_something: micro-experience from domain pool
      - world_pulse  : HN trending topic
      - seasonal     : season + moon phase
    """

    AGENT_NAME = "WorldDiscoveryAgent"

    def __init__(self, openai_client=None):
        self.openai = openai_client
        self.signal_agent = get_world_signal_agent()

    async def generate_screen(
        self,
        user_context: Dict[str, Any],
        variant: str = "A",
        force_card_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generate a world-aware discovery card.
        Entry point compatible with OraBrain's agent interface.
        """
        city = (
            user_context.get("user_city")
            or user_context.get("city")
            or "Vancouver"
        )
        signals = await self.signal_agent.get_signals(city=city)

        # Decide card type
        card_type = force_card_type or self._pick_card_type(signals, user_context)

        # Generate the card
        spec = await self._generate_card(card_type, signals, user_context, variant)
        return spec

    async def generate_card_batch(
        self,
        user_context: Dict[str, Any],
        count: int = 3,
    ) -> List[Dict[str, Any]]:
        """
        Generate multiple diverse world-aware cards for no-goals users.
        """
        city = user_context.get("user_city") or user_context.get("city") or "Vancouver"
        signals = await self.signal_agent.get_signals(city=city)

        # Pick diverse card types — avoid repeating the same type
        available_types = ["right_now", "on_this_day", "try_something", "world_pulse", "seasonal"]
        seed = signals.get("serendipity_seed", 0.5)
        rng = random.Random(seed)
        rng.shuffle(available_types)
        selected_types = available_types[:count]

        cards = []
        for i, card_type in enumerate(selected_types):
            variant = "A" if i % 2 == 0 else "B"
            card = await self._generate_card(card_type, signals, user_context, variant)
            cards.append(card)

        return cards

    # ------------------------------------------------------------------
    # Card type selection
    # ------------------------------------------------------------------

    def _pick_card_type(
        self, signals: Dict[str, Any], user_context: Dict[str, Any]
    ) -> str:
        """
        Pick the most relevant card type for this moment.
        Uses serendipity seed + time + signals to vary the choice.
        """
        seed = signals.get("serendipity_seed", 0.5)
        hour = signals.get("hour", 12)
        has_goals = bool(user_context.get("active_goals"))
        on_this_day = signals.get("on_this_day", [])
        hn_titles = signals.get("trending_hn", [])

        # Weight the card types by context
        weights = {
            "right_now": 0.30,         # always relevant
            "try_something": 0.25,     # always relevant
            "on_this_day": 0.20 if on_this_day else 0.05,
            "world_pulse": 0.15 if hn_titles else 0.05,
            "seasonal": 0.15,
        }

        # Boost certain types by time of day
        if hour in range(6, 10):  # morning
            weights["right_now"] += 0.10
        elif hour in range(17, 21):  # evening
            weights["seasonal"] += 0.10

        # Normalize
        total = sum(weights.values())
        normalized = {k: v / total for k, v in weights.items()}

        # Seeded selection
        rng = random.Random(seed * 10000)
        r = rng.random()
        cumulative = 0.0
        for card_type, weight in normalized.items():
            cumulative += weight
            if r <= cumulative:
                return card_type

        return "try_something"

    # ------------------------------------------------------------------
    # Card generation
    # ------------------------------------------------------------------

    async def _generate_card(
        self,
        card_type: str,
        signals: Dict[str, Any],
        user_context: Dict[str, Any],
        variant: str,
    ) -> Dict[str, Any]:
        """Dispatch to the correct card generator."""
        generators = {
            "right_now": self._card_right_now,
            "on_this_day": self._card_on_this_day,
            "try_something": self._card_try_something,
            "world_pulse": self._card_world_pulse,
            "seasonal": self._card_seasonal,
        }
        fn = generators.get(card_type, self._card_right_now)
        content = await fn(signals, user_context)

        # Try LLM enhancement if OpenAI available
        if self.openai and settings.has_openai and content.get("_enhance", False):
            enhanced = await self._enhance_with_llm(content, signals, user_context)
            if enhanced:
                content["body"] = enhanced
        content.pop("_enhance", None)

        return self._build_spec(content, signals, card_type, variant, user_context)

    async def _card_right_now(
        self, signals: Dict[str, Any], user_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Card based on current weather + time of day."""
        weather = signals.get("weather", {})
        condition = weather.get("condition", "clear")
        time_of_day = signals.get("time_of_day", "afternoon")
        temp_c = weather.get("temp_c")
        city = signals.get("city", "")

        # Build template key
        template_key = f"{condition}_{time_of_day}"
        templates = WORLD_CARD_TEMPLATES.get(template_key, [])

        # Fallback combos
        if not templates:
            if condition in ("rainy", "stormy"):
                templates = WORLD_CARD_TEMPLATES.get("rainy_evening", [])
            elif condition in ("snowy",):
                templates = WORLD_CARD_TEMPLATES.get("snowy", [])
            elif condition in ("sunny", "clear", "partly_cloudy"):
                templates = WORLD_CARD_TEMPLATES.get("sunny_morning", [])
            elif temp_c is not None and temp_c < 5:
                templates = WORLD_CARD_TEMPLATES.get("cold_evening", [])
            else:
                templates = WORLD_CARD_TEMPLATES.get("cloudy_afternoon", [])

        seed = signals.get("serendipity_seed", 0.5)
        body = _pick_template(templates, seed)

        # Build a natural title
        condition_titles = {
            "rainy": "It's raining right now",
            "stormy": "There's a storm outside",
            "snowy": "It's snowing",
            "sunny": "The sun is out",
            "clear": "Clear skies today",
            "cloudy": "Overcast today",
            "partly_cloudy": "Partly cloudy today",
            "foggy": "It's foggy out there",
            "unknown": f"It's {time_of_day}",
        }
        title = condition_titles.get(condition, f"Right now in {city}" if city else "Right now")
        if temp_c is not None:
            title += f" · {temp_c:.0f}°C"

        return {
            "title": title,
            "body": body,
            "cta": "I'm in",
            "signal_source": "weather",
            "world_context": f"{condition} {time_of_day} in {city}" if city else f"{condition} {time_of_day}",
            "_enhance": True,
        }

    async def _card_on_this_day(
        self, signals: Dict[str, Any], user_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Card based on Wikipedia 'On This Day'."""
        events = signals.get("on_this_day", [])
        seed = signals.get("serendipity_seed", 0.5)

        if not events:
            # Fallback to try_something
            return await self._card_try_something(signals, user_context)

        # Pick the most interesting event (use seed for variation)
        rng = random.Random(seed)
        evt = rng.choice(events)
        year = evt.get("year", "")
        text = evt.get("text", "")

        templates = WORLD_CARD_TEMPLATES.get("on_this_day", [])
        template = _pick_template(templates, seed)
        body = template.format(year=year, event=text)

        return {
            "title": f"On this day in {year}",
            "body": body,
            "cta": "Reflect on this",
            "signal_source": "history",
            "world_context": f"Wikipedia OTD: {year} — {text[:80]}",
            "_enhance": False,
        }

    async def _card_try_something(
        self, signals: Dict[str, Any], user_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Card from the micro-experience domain pool."""
        seed = signals.get("serendipity_seed", 0.5)
        time_of_day = signals.get("time_of_day", "afternoon")

        # Determine domain from user context or rotate
        domain = user_context.get("domain", "iVive")
        domain_map = {
            "iVive": "try_ivive",
            "Eviva": "try_eviva",
            "Aventi": "try_aventi",
        }
        template_key = domain_map.get(domain, "try_ivive")

        # Rotate domains for variety using seed
        rng = random.Random(seed)
        if rng.random() > 0.7:
            template_key = rng.choice(["try_ivive", "try_eviva", "try_aventi"])

        templates = WORLD_CARD_TEMPLATES.get(template_key, WORLD_CARD_TEMPLATES["try_ivive"])
        body = _pick_template(templates, seed * 2)

        domain_titles = {
            "try_ivive": "Try this for yourself",
            "try_eviva": "Something for someone else",
            "try_aventi": "Do something you'll remember",
        }
        title = domain_titles.get(template_key, "Try something today")

        return {
            "title": title,
            "body": body,
            "cta": "Let's do it",
            "signal_source": "random",
            "world_context": f"Serendipity pick (seed {seed:.2f}, {time_of_day})",
            "_enhance": False,
        }

    async def _card_world_pulse(
        self, signals: Dict[str, Any], user_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Card based on HN trending topic."""
        hn_titles = signals.get("trending_hn", [])
        seed = signals.get("serendipity_seed", 0.5)

        if not hn_titles:
            return await self._card_try_something(signals, user_context)

        rng = random.Random(seed)
        topic = rng.choice(hn_titles[:5])

        templates = WORLD_CARD_TEMPLATES.get("world_pulse", [])
        template = _pick_template(templates, seed)
        body = template.format(topic=topic)

        return {
            "title": "What the world is talking about",
            "body": body,
            "cta": "What I think",
            "signal_source": "trending",
            "world_context": f"HN trending: {topic[:60]}",
            "_enhance": False,
        }

    async def _card_seasonal(
        self, signals: Dict[str, Any], user_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Card based on current season + moon phase."""
        season = signals.get("season", "spring")
        moon_phase = signals.get("moon_phase", "waxing_crescent")
        moon_emoji = signals.get("moon_emoji", "🌒")
        seed = signals.get("serendipity_seed", 0.5)

        # Prefer moon phase if it's a notable phase (new/full)
        rng = random.Random(seed)
        use_moon = moon_phase in ("new_moon", "full_moon") or rng.random() < 0.4

        if use_moon:
            templates = WORLD_CARD_TEMPLATES.get(moon_phase, [])
            signal_source = "seasonal"
            world_context = f"{moon_phase.replace('_', ' ')} in {season}"
            title = f"{moon_emoji} {moon_phase.replace('_', ' ').title()}"
        else:
            templates = WORLD_CARD_TEMPLATES.get(season, [])
            signal_source = "seasonal"
            world_context = f"{season} season"
            season_titles = {
                "spring": "It's spring",
                "summer": "It's summer",
                "autumn": "It's autumn",
                "winter": "It's winter",
            }
            title = season_titles.get(season, f"{season.title()}")

        body = _pick_template(templates, seed * 3)

        return {
            "title": title,
            "body": body,
            "cta": "Feel it",
            "signal_source": signal_source,
            "world_context": world_context,
            "_enhance": False,
        }

    # ------------------------------------------------------------------
    # LLM enhancement
    # ------------------------------------------------------------------

    async def _enhance_with_llm(
        self,
        content: Dict[str, Any],
        signals: Dict[str, Any],
        user_context: Dict[str, Any],
    ) -> Optional[str]:
        """
        Use GPT-4o to write a more alive, contextual version of the card body.
        Falls back to template if LLM fails.
        """
        try:
            weather = signals.get("weather", {})
            time_of_day = signals.get("time_of_day", "")
            season = signals.get("season", "")
            moon_phase = signals.get("moon_phase", "")
            city = signals.get("city", "")
            goals = [g.get("title", "") for g in user_context.get("active_goals", [])]

            prompt = f"""You are Ora — a warm, poetic, and practical AI companion for human growth.

Write a 2-3 sentence life suggestion card for this moment:
- Weather: {weather.get('condition_raw', weather.get('condition', 'clear'))} in {city}
- Temperature: {weather.get('temp_c', '?')}°C (feels like {weather.get('feels_like_c', '?')}°C)
- Time: {time_of_day} in {season}
- Moon: {moon_phase.replace('_', ' ')}
- User's active goals: {goals or 'none yet'}

Base template: {content.get('body', '')}

Rules:
- Reference the actual world signal naturally (weather, time, season)
- Be poetic but practical — suggest something doable RIGHT NOW
- Vary your tone: this one can be {'philosophical' if signals.get('serendipity_seed', 0.5) > 0.6 else 'warm and direct'}
- End with a gentle question or invitation, not a command
- 2-3 sentences max
- Return ONLY the suggestion text, no quotes, no preamble"""

            response = await self.openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.85,
                max_tokens=120,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.warning(f"WorldDiscoveryAgent LLM enhance failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Spec builder
    # ------------------------------------------------------------------

    def _build_spec(
        self,
        content: Dict[str, Any],
        signals: Dict[str, Any],
        card_type: str,
        variant: str,
        user_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Build a ScreenSpec-compatible dict from card content."""
        has_goals = bool(user_context.get("active_goals"))
        is_serendipity = has_goals  # when injected into a goals flow, it's serendipity

        type_labels = {
            "right_now": "world_pulse",
            "on_this_day": "discovery",
            "try_something": "serendipity",
            "world_pulse": "world_pulse",
            "seasonal": "discovery",
        }
        screen_type = type_labels.get(card_type, "discovery")

        components = [
            {
                "type": "category_badge",
                "text": self._card_type_badge(card_type),
                "color": "#6366f1",
            },
            {
                "type": "headline",
                "text": content.get("title", "Right now"),
                "style": "large_bold",
            },
            {
                "type": "body_text",
                "text": content.get("body", ""),
            },
            {
                "type": "action_button",
                "label": content.get("cta", "Explore"),
                "action": {"type": "next_screen", "context": "world_discovery_continue"},
            },
        ]

        return {
            "type": screen_type,
            "layout": "card_stack",
            "components": components,
            "feedback_overlay": {
                "type": "star_rating",
                "position": "bottom_right",
                "always_visible": True,
            },
            "metadata": {
                "agent": self.AGENT_NAME,
                "variant": variant,
                "is_world_aware": True,
                "is_serendipity": is_serendipity,
                "signal_source": content.get("signal_source", "random"),
                "world_context": content.get("world_context", ""),
                "card_type": card_type,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    def _card_type_badge(self, card_type: str) -> str:
        badges = {
            "right_now": "🌍 RIGHT NOW",
            "on_this_day": "📅 TODAY IN HISTORY",
            "try_something": "⚡ TRY SOMETHING",
            "world_pulse": "🔥 WORLD PULSE",
            "seasonal": "🌿 THIS SEASON",
        }
        return badges.get(card_type, "✨ DISCOVERY")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pick_template(templates: List[str], seed: float) -> str:
    """Pick a template deterministically from seed. Fallback to first if empty."""
    if not templates:
        return "What would make today worth remembering?"
    rng = random.Random(int(seed * 100000))
    return rng.choice(templates)
