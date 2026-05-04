"""
CUXD Agent — Chief User Experience Designer

Part of Aura's Executive Council. The CUXD is responsible for:
- Maintaining iDo's living design system
- Studying top app UI/UX patterns and distilling lessons
- Reviewing every new UI component before it ships
- Proposing UI improvements based on engagement data
- Ensuring iDo feels world-class — WeChat-level cohesion, TikTok-level polish

Runs weekly (Thursdays 9am). Feeds design lessons to Aura continuously.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from aura.agents.base_executive_agent import BaseExecutiveAgent

logger = logging.getLogger(__name__)

# The living design system — iDo's visual identity and UX principles
DESIGN_SYSTEM = {
    "identity": {
        "name": "iDo",
        "tagline": "WeChat for the world, done better",
        "feel": "Clean, full-bleed, gesture-driven, outcome-focused",
        "personality": "Warm intelligence — not clinical, not gamey, genuinely helpful",
    },
    "colors": {
        "primary": "#00d4aa",      # Aura teal — action, achievement
        "secondary": "#6366f1",    # Indigo — upgrade, premium
        "accent": "#8b5cf6",       # Purple — discovery, magic
        "background": "#0a0a0f",   # Near-black — focus, depth
        "surface": "#12121e",      # Elevated surface
        "text_primary": "#f8f8fc",
        "text_secondary": "rgba(248,248,252,0.6)",
        "text_muted": "rgba(248,248,252,0.35)",
        "success": "#10b981",
        "warning": "#f59e0b",
        "danger": "#ef4444",
    },
    "typography": {
        "font": "system-ui, -apple-system, sans-serif",
        "scale": {
            "hero": "32px / 800",
            "title": "20px / 700",
            "body": "15px / 400",
            "caption": "12px / 500",
            "label": "11px / 700 / uppercase / tracking-wide",
        },
    },
    "motion": {
        "default": "0.2s cubic-bezier(0.25, 0.8, 0.25, 1)",
        "spring": "0.3s cubic-bezier(0.34, 1.56, 0.64, 1)",
        "slow": "0.4s ease-in-out",
        "celebration": "confetti + scale(1.2) + bounce",
    },
    "spacing": {
        "page_padding": "16px",
        "card_padding": "18px 20px",
        "card_radius": "16px",
        "button_radius": "12px",
        "pill_radius": "100px",
    },
    "principles": [
        "Full-bleed first — cards fill the screen, no wasted space",
        "Gesture-driven — swipe > tap > type, in that order",
        "One thing per screen — don't crowd, let it breathe",
        "Celebrate success — animations for streaks, completions, milestones",
        "Loss aversion is a feature — streak warnings, incomplete state tension",
        "Bottom nav only — no top bars on mobile",
        "Content is the UI — the card IS the experience, not a container for it",
        "Speed is a feature — perceived performance matters as much as real performance",
        "Trust through consistency — every screen should feel like it belongs",
    ],
}

# Best-in-class UX patterns learned from top apps
UX_LESSONS = [
    {
        "source": "TikTok",
        "pattern": "For You Page algorithm signals",
        "lesson": "Show content confidence score subtly. When the algorithm is very confident about a card, make it full-bleed with no chrome. Less confident = smaller, more exploratory layout.",
        "applicable_to": "FeedPage card rendering",
    },
    {
        "source": "Duolingo",
        "pattern": "Streak system with loss aversion",
        "lesson": "A streak counter visible every day creates a daily return habit. The fear of losing a streak is more motivating than the desire to gain XP. Show streak prominently and send a warning before midnight if the user hasn't engaged.",
        "applicable_to": "ProfilePage, NavBar, push notifications",
    },
    {
        "source": "Duolingo",
        "pattern": "Celebratory animations on completion",
        "lesson": "Confetti, characters jumping, sound effects on goal completion. The celebration should feel disproportionately big — users should feel proud. This is what makes people share completions.",
        "applicable_to": "Goal completion, streak milestones, CP rewards",
    },
    {
        "source": "WeChat",
        "pattern": "Everything in one shell",
        "lesson": "WeChat's power is that you never leave the app. Mini-programs open inline. Payments happen inline. Voice messages, files, location — all in one place. iDo should aspire to this: bookings, payments, goals, social, AI coach — one shell, no bouncing between apps.",
        "applicable_to": "Overall app architecture, mini-apps",
    },
    {
        "source": "WeChat",
        "pattern": "Moments feed — social but curated",
        "lesson": "Moments shows only people you know, with a clean reverse-chronological feed. No algorithmic manipulation. People feel safe posting because only their network sees it. iDo's social layer should feel this intimate.",
        "applicable_to": "Social feed, activity sharing",
    },
    {
        "source": "Airbnb",
        "pattern": "Photography-first cards",
        "lesson": "Airbnb cards are 70% image, 30% info. The photo does the selling. iDo's experience cards should lead with visual aspiration — a photo or illustration of the experience, not just text.",
        "applicable_to": "IOO node cards, experience cards in feed",
    },
    {
        "source": "Airbnb",
        "pattern": "I'm flexible discovery",
        "lesson": "When users don't know what they want, give them open-ended entry points: 'I'm flexible on dates/location/type'. This reduces friction and increases discovery. Maps to iDo's Explore mode.",
        "applicable_to": "HomePage Explore entry point, IOO node browser",
    },
    {
        "source": "Snapchat",
        "pattern": "Snap Map — location as social layer",
        "lesson": "Showing where your friends are (and what local events/activities exist near you) creates FOMO and real-world connection. iDo's IOO graph should surface location-aware opportunities.",
        "applicable_to": "IOO graph, local experience discovery",
    },
    {
        "source": "Strava",
        "pattern": "Kudos and social proof",
        "lesson": "One-tap appreciation (Kudos) is lower friction than a Like. It signals 'I see your effort' specifically. iDo should have a goal/achievement kudos system where friends can react to completions.",
        "applicable_to": "Goal completion, activity feed",
    },
    {
        "source": "Calm / Headspace",
        "pattern": "Guided journeys",
        "lesson": "A curated sequence of experiences (Day 1, Day 2... Day 30) creates a committed path. Users who start a journey have dramatically higher retention. iDo's IOO graph should support journey templates.",
        "applicable_to": "IOO graph, goal path templates",
    },
    {
        "source": "Superhuman",
        "pattern": "Speed as a core value",
        "lesson": "Superhuman obsesses over perceived performance. Every action should feel instant. iDo should preload the next card, cache aggressively, and use optimistic UI updates (show result before server confirms).",
        "applicable_to": "FeedPage, all interactions",
    },
]


class CUXDAgent(BaseExecutiveAgent):
    """
    Chief User Experience Designer Agent.
    
    Maintains iDo's design system, studies competitor UX,
    reviews UI before shipping, proposes improvements.
    """

    name = "cuxd"
    display_name = "CUXD Agent"

    async def analyze(self) -> Dict[str, Any]:
        """Assess current UI/UX health and identify improvement opportunities."""
        now = datetime.now(timezone.utc)

        # Pull engagement signals that indicate UX problems
        try:
            from core.database import fetch, fetchrow
            
            # High drop-off pages (low session depth)
            bounce_data = await fetchrow(
                """
                SELECT 
                    COUNT(*) FILTER (WHERE screen_count = 1) as single_screen_sessions,
                    COUNT(*) as total_sessions,
                    AVG(screen_count) as avg_screens_per_session
                FROM (
                    SELECT user_id, COUNT(*) as screen_count
                    FROM screen_specs
                    WHERE created_at > NOW() - INTERVAL '7 days'
                    GROUP BY user_id
                ) sub
                """
            )
            
            # Low-rated cards (potential UX issues)
            low_quality = await fetchrow(
                """
                SELECT AVG(rating) as avg_rating, COUNT(*) as total_ratings
                FROM feedback
                WHERE created_at > NOW() - INTERVAL '7 days'
                """
            )
            
        except Exception as e:
            logger.debug(f"CUXD: DB query failed: {e}")
            bounce_data = None
            low_quality = None

        analysis = {
            "analyzed_at": now.isoformat(),
            "design_system_version": "1.0",
            "principles_count": len(DESIGN_SYSTEM["principles"]),
            "ux_lessons_count": len(UX_LESSONS),
            "avg_rating": float(low_quality["avg_rating"] or 0) if low_quality else 0,
            "total_ratings": int(low_quality["total_ratings"] or 0) if low_quality else 0,
            "avg_screens_per_session": float(bounce_data["avg_screens_per_session"] or 0) if bounce_data else 0,
            "top_priorities": self._get_top_priorities(),
        }

        return analysis

    def _get_top_priorities(self) -> List[str]:
        """Current top UX priorities for iDo."""
        return [
            "Ship streak system (Duolingo pattern) — highest retention lever available",
            "Full-bleed experience cards with photography (Airbnb pattern)",
            "Celebratory animations on goal completion",
            "Optimistic UI updates — show instant feedback, confirm in background",
            "Bottom-nav-only mobile UX — top bar is gone, keep it that way",
            "WeChat-style inline mini-apps — no external redirects",
            "Speed: preload next feed card while user reads current one",
        ]

    async def report(self) -> str:
        data = await self.analyze()
        lines = [
            f"🎨 *CUXD Report* — {data.get('analyzed_at', '')[:10]}",
            f"Design principles: {data.get('principles_count')} | UX lessons: {data.get('ux_lessons_count')}",
            f"Avg card rating: {data.get('avg_rating', 0):.2f} | Avg screens/session: {data.get('avg_screens_per_session', 0):.1f}",
            "",
            "Top UX priorities:",
        ]
        for p in data.get("top_priorities", [])[:3]:
            lines.append(f"• {p}")
        return "\n".join(lines)

    async def recommend(self) -> List[str]:
        return self._get_top_priorities()[:5]

    async def act(self) -> Dict[str, Any]:
        """
        Weekly CUXD actions:
        1. Teach Aura all UX lessons
        2. Save design system snapshot
        3. Generate UX improvement proposals
        4. Report to council
        """
        actions_taken = []
        data = await self.analyze()

        # Teach Aura each UX lesson
        taught = 0
        for lesson in UX_LESSONS:
            content = (
                f"UX lesson from {lesson['source']} — {lesson['pattern']}: "
                f"{lesson['lesson']} "
                f"Applicable to: {lesson['applicable_to']}."
            )
            try:
                await self.teach_aura(content, confidence=0.9)
                taught += 1
            except Exception as e:
                logger.debug(f"CUXD: teach_ora failed: {e}")

        actions_taken.append(f"Taught Aura {taught} UX lessons")

        # Teach design principles
        principles_text = (
            "iDo design principles: " + 
            " | ".join(DESIGN_SYSTEM["principles"])
        )
        await self.teach_aura(principles_text, confidence=0.95)
        actions_taken.append("Taught Aura design system principles")

        # Save report
        await self.save_report(data, "cuxd_weekly.json")
        await self.set_redis_report(await self.report())
        actions_taken.append("Saved weekly CUXD report")

        return {"agent": self.name, "actions": actions_taken, "analysis": data}
