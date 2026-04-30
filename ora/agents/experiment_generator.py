"""
ExperimentGeneratorAgent — The evolutionary intelligence layer of Ora's A/B system.
Generates new experiments from winners, feedback, trends, and cross-experiment learning.
"""
import json
import os
import asyncio
import logging
import httpx
from datetime import datetime, timezone
from typing import Optional
import redis.asyncio as aioredis

from core.database import fetchrow, fetch, execute

logger = logging.getLogger(__name__)

API_URL = "https://connectome-api-production.up.railway.app"
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")


class ExperimentGeneratorAgent:

    async def _get_token(self) -> str:
        """Get API token for Ora calls."""
        token_file = "/Users/avielcarlos/.openclaw/workspace/tmp/connectome_jwt.txt"
        if os.path.exists(token_file):
            return open(token_file).read().strip()
        async with httpx.AsyncClient() as client:
            r = await client.post(f"{API_URL}/api/users/login",
                json={"email": "test@test.com", "password": "test1234"})
            return r.json()["access_token"]

    async def _ask_aura(self, prompt: str) -> str:
        """Ask Ora to generate content via the chat API."""
        try:
            token = await self._get_token()
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(f"{API_URL}/api/ora/chat",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"message": prompt})
                return r.json().get("reply", "")
        except Exception as e:
            logger.error(f"Ora API error: {e}")
            return ""

    async def _teach_aura(self, lesson: str):
        """Teach Ora a lesson."""
        try:
            token = await self._get_token()
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(f"{API_URL}/api/ora/learn",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"lesson": lesson, "source": "experiment_generator", "confidence": 0.8})
        except Exception as e:
            logger.error(f"Teach error: {e}")

    async def _create_experiment(self, name: str, page: str, metric: str,
                                  variants: dict, source: str = "generated",
                                  parent_experiment: str = None, generation: int = 0):
        """Create a new experiment in the DB."""
        exp_id = f"{name}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        try:
            await execute("""
                INSERT INTO ab_experiments (id, name, page, metric, variants, source, parent_experiment, generation)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)
                ON CONFLICT (id) DO NOTHING
            """, exp_id, name, page, metric, json.dumps(variants), source, parent_experiment, generation)
            logger.info(f"Created experiment: {exp_id}")
            return exp_id
        except Exception as e:
            logger.error(f"Create experiment error: {e}")
            return None

    async def evolve_winner(self, experiment_id: str, winner_variant: str):
        """When a variant wins, generate next-generation experiments."""
        exp = await fetchrow("SELECT * FROM ab_experiments WHERE id = $1", experiment_id)
        if not exp:
            return

        variants = exp["variants"] if isinstance(exp["variants"], dict) else json.loads(exp["variants"])
        winner_content = variants.get(winner_variant, "")

        prompt = f"""In an A/B experiment called "{exp['name']}" testing "{exp['metric']}", variant {winner_variant} won.
The winning variant was: "{winner_content}"

Generate 3 new variants for the next generation of this experiment that:
A) Push the winning insight further to its logical extreme
B) Test a closely related but different hypothesis
C) Apply a completely fresh creative angle inspired by what won

Format your response as exactly:
VARIANT_A: [content]
VARIANT_B: [content]
VARIANT_C: [content]"""

        response = await self._ask_aura(prompt)

        new_variants = {}
        for line in response.split('\n'):
            if line.startswith('VARIANT_A:'):
                new_variants['A'] = line.replace('VARIANT_A:', '').strip()
            elif line.startswith('VARIANT_B:'):
                new_variants['B'] = line.replace('VARIANT_B:', '').strip()
            elif line.startswith('VARIANT_C:'):
                new_variants['C'] = line.replace('VARIANT_C:', '').strip()

        if len(new_variants) >= 2:
            new_id = await self._create_experiment(
                name=f"{exp['name']}_gen{(exp['generation'] or 0) + 1}",
                page=exp['page'],
                metric=exp['metric'],
                variants=new_variants,
                source='generated',
                parent_experiment=experiment_id,
                generation=(exp['generation'] or 0) + 1
            )

            await self._teach_aura(
                f"Experiment evolution: '{exp['name']}' winner was variant {winner_variant} ({winner_content}). "
                f"New generation experiment '{new_id}' created with evolved variants. "
                f"This is generation {(exp['generation'] or 0) + 1} of testing."
            )
            logger.info(f"Evolved experiment {experiment_id} → {new_id}")

    async def generate_from_feedback(self, feedback_theme: str, page: str):
        """User feedback triggers new experiments."""
        prompt = f"""Users are giving feedback: "{feedback_theme}" on the {page} page.
Generate 3 A/B test variants to address this feedback.
Format:
VARIANT_A: [description]
VARIANT_B: [description]
VARIANT_C: [description]"""

        response = await self._ask_aura(prompt)
        variants = {}
        for line in response.split('\n'):
            for v in ['A', 'B', 'C']:
                if line.startswith(f'VARIANT_{v}:'):
                    variants[v] = line.replace(f'VARIANT_{v}:', '').strip()

        if variants:
            await self._create_experiment(
                name=f"{page}_user_feedback_{datetime.now(timezone.utc).strftime('%Y%m%d')}",
                page=page, metric="user_satisfaction", variants=variants,
                source='user_feedback'
            )

    async def generate_from_trend(self, trend: str, context: str):
        """Trend signal triggers new experiment."""
        prompt = f"""The trend "{trend}" is relevant to iDo ({context}).
What 3 UI/UX or copy variants should we A/B test to capitalize on this trend?
Format:
VARIANT_A: [description]
VARIANT_B: [description]
VARIANT_C: [description]"""

        response = await self._ask_aura(prompt)
        variants = {}
        for line in response.split('\n'):
            for v in ['A', 'B', 'C']:
                if line.startswith(f'VARIANT_{v}:'):
                    variants[v] = line.replace(f'VARIANT_{v}:', '').strip()

        if variants:
            trend_slug = trend[:30].lower().replace(' ', '_').replace('"', '')
            await self._create_experiment(
                name=f"trend_{trend_slug}_{datetime.now(timezone.utc).strftime('%Y%m%d')}",
                page='global', metric='engagement_rate', variants=variants,
                source='trend'
            )

    async def cross_pollinate(self):
        """Apply winning patterns from one context to other pages."""
        winners = await fetch("""
            SELECT name, page, winner, variants, metric
            FROM ab_experiments
            WHERE status = 'concluded' AND winner IS NOT NULL
            ORDER BY concluded_at DESC LIMIT 20
        """)

        if len(winners) < 3:
            return

        winner_contents = []
        for w in winners:
            variants = w['variants'] if isinstance(w['variants'], dict) else json.loads(w['variants'])
            winner_contents.append(f"- {w['name']} ({w['page']}): {variants.get(w['winner'], '')}")

        prompt = f"""These A/B test variants have consistently won recently:
{chr(10).join(winner_contents)}

Identify 1-2 patterns that appear across multiple winners.
Then suggest what experiments to run on OTHER pages to test those patterns there.
Format:
PATTERN: [description of what consistently wins]
NEW_EXPERIMENT: [page] | [experiment name] | [variant A] | [variant B] | [variant C]"""

        response = await self._ask_aura(prompt)

        # Simple parse: look for NEW_EXPERIMENT lines
        for line in response.split('\n'):
            if line.startswith('NEW_EXPERIMENT:'):
                parts = line.replace('NEW_EXPERIMENT:', '').split('|')
                if len(parts) >= 4:
                    page, name = parts[0].strip(), parts[1].strip()
                    variants = {chr(65+i): parts[i+2].strip() for i in range(len(parts)-2) if i < 4}
                    await self._create_experiment(
                        name=f"cross_{name[:40]}",
                        page=page, metric='engagement_rate', variants=variants,
                        source='cross_pollination'
                    )

    async def prune_losers(self):
        """Learn what doesn't work and teach Ora to avoid it."""
        losers = await fetch("""
            SELECT name, page, variants, winner
            FROM ab_experiments
            WHERE status = 'concluded' AND winner IS NOT NULL
            ORDER BY concluded_at DESC LIMIT 30
        """)

        if not losers:
            return

        loser_patterns = []
        for exp in losers:
            variants = exp['variants'] if isinstance(exp['variants'], dict) else json.loads(exp['variants'])
            for key, content in variants.items():
                if key != exp['winner'] and content:
                    loser_patterns.append(f"{exp['name']}: {content} (lost)")

        if loser_patterns:
            redis = aioredis.from_url(REDIS_URL)
            await redis.set('ora:ab:loser_patterns', json.dumps(loser_patterns[-50:]), ex=86400*30)
            await redis.aclose()

            await self._teach_aura(
                f"A/B learning — patterns that consistently lose and should NOT be used: "
                f"{'; '.join(loser_patterns[:5])}. Avoid these patterns in future experiments."
            )

    async def run_daily(self):
        """Daily evolution routine."""
        logger.info("ExperimentGeneratorAgent: starting daily run")

        # Find experiments concluded in last 24h
        recent_winners = await fetch("""
            SELECT id, winner FROM ab_experiments
            WHERE status = 'concluded'
            AND winner IS NOT NULL
            AND concluded_at > NOW() - INTERVAL '24 hours'
        """)

        evolutions = []
        for exp in recent_winners:
            await self.evolve_winner(exp['id'], exp['winner'])
            evolutions.append(exp['id'])

        # Cross-pollinate if 3+ winners this week
        weekly_winners = await fetch("""
            SELECT COUNT(*) as cnt FROM ab_experiments
            WHERE status = 'concluded' AND winner IS NOT NULL
            AND concluded_at > NOW() - INTERVAL '7 days'
        """)
        if weekly_winners and weekly_winners[0]['cnt'] >= 3:
            await self.cross_pollinate()

        await self.prune_losers()

        # Seed nav experiments if they don't exist
        await self._seed_nav_experiments()

        lesson = f"Daily experiment evolution: {len(evolutions)} experiments evolved. "
        if evolutions:
            lesson += f"Evolved: {', '.join(evolutions[:3])}."
        await self._teach_aura(lesson)

        logger.info(f"ExperimentGeneratorAgent: done. {len(evolutions)} evolutions.")

    async def _seed_nav_experiments(self):
        """Seed navigation experiments if they don't exist."""
        nav_experiments = {
            "nav_position": {
                "page": "global", "metric": "nav_engagement_rate",
                "variants": {
                    "A": "Fixed bottom tabs (standard)",
                    "B": "Auto-hide bottom tabs (hides on scroll)",
                    "C": "Floating pill (compact, expands on tap)",
                    "D": "Bottom sheet drawer (hamburger overlay)"
                }
            },
            "nav_ora_prominence": {
                "page": "global", "metric": "ora_session_starts",
                "variants": {
                    "A": "Ora same size as other tabs",
                    "B": "Ora elevated FAB-style center button",
                    "C": "Ora replaces home — primary tab",
                    "D": "Ora tab pulses when she has something to say"
                }
            },
            "nav_item_count": {
                "page": "global", "metric": "section_discovery_rate",
                "variants": {
                    "A": "3 items: Feed, Ora, Profile",
                    "B": "4 items: Feed, Ora, Goals, Profile",
                    "C": "5 items: Feed, Ora, Goals, DAO, Profile",
                    "D": "2+overflow: Feed, Ora, ··· (more)"
                }
            },
            "nav_active_indicator": {
                "page": "global", "metric": "nav_click_distribution",
                "variants": {
                    "A": "Purple underline on active",
                    "B": "Purple pill behind active item",
                    "C": "Icon scales up when active",
                    "D": "Color fills icon when active"
                }
            },
            "nav_labels_visibility": {
                "page": "global", "metric": "nav_error_rate",
                "variants": {
                    "A": "Labels always visible",
                    "B": "Labels only on active tab",
                    "C": "Icons only, no labels",
                    "D": "Labels appear on long press"
                }
            },
            "nav_gesture_support": {
                "page": "global", "metric": "session_depth",
                "variants": {
                    "A": "Tap only navigation",
                    "B": "Swipe left/right between tabs",
                    "C": "Swipe up from bottom reveals full nav"
                }
            },
            "nav_expand_behavior": {
                "page": "global", "metric": "feature_discovery_rate",
                "variants": {
                    "A": "Static tabs, no expansion",
                    "B": "Long press shows sub-items",
                    "C": "Tap active tab opens section sub-menu"
                }
            },
        }

        for name, config in nav_experiments.items():
            existing = await fetchrow("SELECT id FROM ab_experiments WHERE name = $1", name)
            if not existing:
                await self._create_experiment(
                    name=name, page=config["page"], metric=config["metric"],
                    variants=config["variants"], source="manual", generation=0
                )
                logger.info(f"Seeded nav experiment: {name}")
