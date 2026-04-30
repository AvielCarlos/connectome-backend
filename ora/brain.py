"""
Ora Brain — Supreme Intelligence Layer

Ora is the JARVIS of human life: simultaneously the world's best recommender,
life coach, assistant, and companion. She is proactive, adaptive, and expansive —
her purpose grows as humanity's needs grow.

On every /screen request:
1. Load user model (goals, emotional state, history, world context, Drive docs)
2. Select the right agent (coaching, discovery, events, world, drive, dao...)
3. Explore vs exploit (adapts based on MetaAgent self-improvement reports)
4. Store and return the screen spec

On every /feedback POST:
1. Update screen_spec rating
2. Update user embedding
3. Run feedback analyst
4. Update A/B tests + MetaAgent learning

Ora's mission: help each person find, achieve, and experience everything
they are looking for in life — and through them, bring humanity closer together.
"""

import asyncio
import logging
import random
import uuid
import json
from typing import Optional, Dict, Any, Tuple, List
from datetime import datetime, timezone, timedelta

from core.config import settings
from core.database import fetchrow, execute, fetch
from ora.user_model import (
    UserModel,
    load_user_model,
    update_user_embedding,
    update_user_embedding_from_cards,
    update_domain_weights,
    get_daily_screen_count,
    increment_daily_screen_count,
)
from ora.ab_testing import assign_user_variant, record_test_result
from ora.agents.discovery import DiscoveryAgent
from ora.agents.coaching import CoachingAgent
from ora.agents.recommendation import RecommendationAgent
from ora.agents.ui_generator import UIGeneratorAgent
from ora.agents.feedback_analyst import FeedbackAnalystAgent
from ora.agents.world_agent import WorldAgent
from ora.agents.feedback_experimenter import FeedbackExperimenter
from ora.agents.enlightenment import EnlightenmentAgent
from ora.agents.collective_intelligence import CollectiveIntelligenceAgent
from ora.agents.discovery_interview import DiscoveryInterviewAgent
from ora.agents.ui_ab_testing import UIABTestingAgent
from ora.agents.explore import ExploreAgent
from ora.agents.feature_lab import AuraFeatureLabAgent
from ora.agents.dao_agent import DaoAgent
from ora.agents.world_discovery_agent import WorldDiscoveryAgent
from ora.agents.drive_agent import DriveAgent
from ora.consciousness import AuraConsciousness
from ora.content_quality import content_quality_check
from ora.agents.context_agent import ContextAgent
from ora.agent_registry import AgentRegistry
from ora.agents.goal_flow_agent import GoalFlowAgent, get_goal_flow_agent
from ora.agents.recommendation_engine import RecommendationEngine, get_recommendation_engine

# Module-level alias so OraBrain code can call _content_quality_check(spec)
_content_quality_check = content_quality_check

# Ora's optimization priority — hard constraint, never overridden
ORA_GOALS = {
    "primary": "user_fulfilment",
    "secondary": "engagement",
    "tertiary": "revenue",
}

logger = logging.getLogger(__name__)


class AuraBrain:
    """
    Ora's central brain. One instance per application lifecycle.
    Owns all agent instances and orchestrates every screen request.
    """

    def __init__(self):
        # Initialize OpenAI client if key is available
        self._openai = None
        if settings.has_openai:
            try:
                from openai import AsyncOpenAI
                self._openai = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
                logger.info("Ora: OpenAI client initialized")
            except Exception as e:
                logger.warning(f"Ora: Could not initialize OpenAI: {e}")
        else:
            logger.info("Ora: No OPENAI_API_KEY — running in mock mode")

        # Initialize agents
        self.discovery = DiscoveryAgent(self._openai)
        self.coaching = CoachingAgent(self._openai)
        self.recommendation = RecommendationAgent(self._openai)
        self.ui_generator = UIGeneratorAgent(self._openai)
        self.feedback_analyst = FeedbackAnalystAgent(self._openai, self.ui_generator)
        self.world = WorldAgent(self._openai)
        self.world_discovery = WorldDiscoveryAgent(self._openai)
        self.feedback_experimenter = FeedbackExperimenter(self._openai)
        self.enlightenment = EnlightenmentAgent(self._openai)
        self.collective = CollectiveIntelligenceAgent(self._openai)
        self.discovery_interview = DiscoveryInterviewAgent(self._openai)
        self.ui_ab = UIABTestingAgent(self._openai)

        # Drive indexer — Ora's long-term memory from personal writing
        self.drive_agent = DriveAgent(self._openai)

        # Ora's self-awareness layer
        self.consciousness = AuraConsciousness(self._openai)

        # Explore and FeatureLab agents
        self.explore = ExploreAgent(self._openai)
        self.feature_lab = AuraFeatureLabAgent(self._openai)

        # DAO agent — contribution evaluation and rewards
        self.dao = DaoAgent(self._openai)

        # Context agent — time/calendar signals for content adaptation
        self.context_agent = ContextAgent()

        # WebSpawn — Ora's surface creation engine (explorer/sovereign only)
        from ora.agents.web_spawn_agent import WebSpawnAgent
        self.web_spawn = WebSpawnAgent(openai_client=self._openai)

        # Goal Flow Engine — dynamic, adaptive goal achievement cards
        self.goal_flow = GoalFlowAgent(openai_client=self._openai)

        # Vector Recommendation Engine — TikTok-style two-tower recommendation
        self.rec_engine = RecommendationEngine(openai_client=self._openai)

        # Dynamic agents loaded from AgentRegistry (spawned/partitioned/merged)
        self._dynamic_agents: Dict[str, Any] = {}

        # Agent rotation weights (updated dynamically by feedback)
        # collective: 8%, explore: 8% of screens
        self._base_weights = {
            "discovery": 0.20,
            "coaching": 0.20,
            "recommendation": 0.16,
            "ui_generator": 0.06,
            "world": 0.17,
            "enlightenment": 0.07,
            "collective": 0.06,
            "explore": 0.08,
        }

        # Tournament mode: enabled by default
        self.tournament_mode: bool = True

        # Load weights from Redis on startup (non-blocking best-effort)
        import asyncio as _asyncio
        try:
            _asyncio.get_event_loop().create_task(self.reload_weights())
        except RuntimeError:
            pass  # No running loop at import time — weights load lazily

    # -----------------------------------------------------------------------
    # Dynamic Agent Loading
    # -----------------------------------------------------------------------

    async def _load_dynamic_agents(self) -> None:
        """Load spawned/evolved agents from registry into brain's agent pool."""
        try:
            registry = AgentRegistry()
            agents = await registry.get_active_agents()

            for agent_spec in agents:
                if agent_spec.get("type") not in ("spawned", "partitioned", "merged"):
                    continue  # Builtins are already hardcoded above

                name = agent_spec["name"]
                module_path = agent_spec.get("module_path")
                class_name = agent_spec.get("class_name", name)

                if not module_path:
                    continue

                try:
                    import importlib
                    module = importlib.import_module(module_path)
                    agent_class = getattr(module, class_name)
                    instance = agent_class(self._openai)
                    self._dynamic_agents[name] = instance
                    logger.info(f"OraBrain: loaded dynamic agent {name} from {module_path}")
                except Exception as e:
                    logger.warning(f"OraBrain: failed to load dynamic agent {name}: {e}")
        except Exception as e:
            logger.warning(f"OraBrain._load_dynamic_agents: {e}")

    # -----------------------------------------------------------------------
    # Redis Weight Loading
    # -----------------------------------------------------------------------

    async def reload_weights(self) -> None:
        """
        Check Redis key `ora:agent_weights` and override _base_weights if found.
        Called on startup and after the autonomy agent updates weights.
        """
        try:
            from core.redis_client import get_redis
            import json as _json
            r = await get_redis()
            weights_raw = await r.get("ora:agent_weights")
            if weights_raw:
                weights = _json.loads(weights_raw)
                # Validate keys before applying
                valid_keys = set(self._base_weights.keys())
                loaded_keys = set(weights.keys())
                if loaded_keys.intersection(valid_keys):
                    # Merge: only update keys we know about
                    for k in valid_keys:
                        if k in weights:
                            self._base_weights[k] = float(weights[k])
                    # Re-normalize
                    total = sum(self._base_weights.values())
                    if total > 0:
                        self._base_weights = {k: v / total for k, v in self._base_weights.items()}
                    logger.info(f"OraBrain: weights loaded from Redis: {self._base_weights}")
        except Exception as e:
            logger.debug(f"OraBrain.reload_weights: {e}")

    # -----------------------------------------------------------------------
    # Meta-Agent: Dynamic Card Weight Tuning
    # -----------------------------------------------------------------------

    async def apply_meta_report(self, report: Dict[str, Any]) -> None:
        """
        Adjust agent rotation weights based on MetaAgent's self-improvement report.
        Called after each MetaAgent run (every 6 hours).

        Rules:
        - top_engaging_card_types → boost those agents by 20%
        - low_engagement_card_types → reduce those agents by 20%
        - Weights are always re-normalized to sum to 1.0
        - Changes are bounded: no single agent goes below 0.03 or above 0.40
        """
        if not report:
            return

        top = report.get("top_engaging_card_types", [])
        low = report.get("low_engagement_card_types", [])

        # Build a mapping from agent_type strings to weight keys
        agent_type_map = {
            "DiscoveryAgent": "discovery",
            "CoachingAgent": "coaching",
            "RecommendationAgent": "recommendation",
            "UIGeneratorAgent": "ui_generator",
            "WorldAgent": "world",
            "EnlightenmentAgent": "enlightenment",
            "CollectiveIntelligenceAgent": "collective",
            "ExploreAgent": "explore",
        }

        weights = self._base_weights.copy()

        for agent_type in top:
            key = agent_type_map.get(agent_type)
            if key and key in weights:
                weights[key] = min(0.40, weights[key] * 1.20)
                logger.info(f"OraBrain: boosting {key} weight (top engaging)")

            # Special case: if local_event is top, boost world agent
            if agent_type == "local_event" and "world" in weights:
                weights["world"] = min(0.40, weights["world"] * 1.25)
                logger.info("OraBrain: boosting world weight (local_event performing well)")

        for agent_type in low:
            key = agent_type_map.get(agent_type)
            if key and key in weights:
                weights[key] = max(0.03, weights[key] * 0.80)
                logger.info(f"OraBrain: reducing {key} weight (low engagement)")

        # Normalize to sum to 1.0
        total = sum(weights.values())
        self._base_weights = {k: v / total for k, v in weights.items()}
        logger.info(f"OraBrain: updated base weights from meta report: {self._base_weights}")

    # -----------------------------------------------------------------------
    # Screen Generation
    # -----------------------------------------------------------------------

    async def get_screen(
        self,
        user_id: str,
        context: Optional[str] = None,
        goal_id: Optional[str] = None,
        domain: Optional[str] = None,
        geo_hints: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], str, int]:
        """
        Main entry point for screen requests.
        Returns: (screen_spec_dict, db_id, screens_today)
        """
        user_model = await load_user_model(user_id)
        if not user_model:
            raise ValueError(f"User {user_id} not found")

        user_context = user_model.to_context_dict()
        screens_today = await increment_daily_screen_count(user_id)

        # Inject session mood from Redis cache
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            mood_val = await r.get(f"mood:{user_id}")
            if mood_val is not None:
                mood_index = int(mood_val)
                # 0-1: tired → serve calmer enlightenment/coaching content
                # 2-3: neutral/good → normal mix
                # 4: energised → boost adventure/discovery/challenge content
                user_context["session_mood"] = mood_index
        except Exception:
            pass

        # Detect domain for this screen request (use explicit param or auto-detect)
        if not domain:
            domain = self._detect_domain(user_model, context)

        # Choose agent
        agent_name, agent_fn, exploit = await self._select_agent(
            user_model, context, goal_id
        )

        # Novelty engine: override agent/domain if diversity is too low
        agent_name, agent_fn, domain = await self._ensure_novelty(
            user_model, agent_name, agent_fn, domain
        )

        # Discovery interview injection
        interaction_count = len(user_model.recent_interactions)
        interview_freq = 8 if interaction_count < 20 else 15
        should_interview = (screens_today % interview_freq == 0) and screens_today > 0
        if should_interview:
            agent_name = "DiscoveryInterviewAgent"
            agent_fn = self.discovery_interview.generate_screen
            logger.info(
                f"Ora: injecting discovery interview at screen #{screens_today} "
                f"(freq={interview_freq}, interactions={interaction_count})"
            )

        # Get A/B variant
        variant = await assign_user_variant(
            user_id,
            test_name=f"agent_{agent_name}_layout",
            variants=["A", "B"],
        )

        # Load relevant lessons for this user context
        lessons = await self.load_relevant_lessons(user_context)
        if lessons:
            user_context["ora_lessons"] = lessons

        logger.info(
            f"Ora→ user={user_id[:8]} agent={agent_name} variant={variant} "
            f"exploit={exploit} screen#{screens_today} domain={domain} lessons={len(lessons)}"
        )

        # Inject domain into user_context for agents
        user_context["domain"] = domain

        # Inject geo context — city, country, timezone, time_of_day
        if geo_hints:
            user_context.update(geo_hints)

        # Inject context_hints — time-of-day / calendar signals
        try:
            context_hints = await self.context_agent.get_user_context_signals(user_id)
            user_context["context_hints"] = context_hints
        except Exception as _ctx:
            logger.debug(f"ContextAgent: signals inject failed: {_ctx}")

        # Inject collective_insight — every agent gets collective wisdom as context
        try:
            collective_insight = await self.collective.get_collective_insight_for_user(user_context)
            user_context["collective_insight"] = collective_insight
        except Exception as _cie:
            logger.debug(f"CollectiveIntelligence: insight inject failed: {_cie}")

        # -----------------------------------------------------------------------
        # Integration A: Vector similarity exploit path
        # 30% of the time when user has >20 interactions AND has an embedding,
        # serve a card pulled via pgvector cosine similarity instead of generating.
        # -----------------------------------------------------------------------
        _use_vector = (
            not should_interview
            and len(user_model.recent_interactions) > 20
            and user_model.embedding is not None
            and random.random() < 0.30
        )
        if _use_vector:
            _v_cards = await self._get_vector_similar_cards(user_id, domain)
            if _v_cards:
                spec_dict = random.choice(_v_cards)
                agent_name = "VectorSimilarityFeed"
                exploit = True
                logger.info(f"Ora: vector similarity exploit → {len(_v_cards)} candidates for user={user_id[:8]}")
            else:
                _use_vector = False  # no embeddings yet; fall through to normal flow

        if not _use_vector:
            # Tournament mode: if enabled and not exploiting, generate/serve tournament variant
            if self.tournament_mode and not exploit:
                try:
                    spec_dict = await self._tournament_pick(
                        agent_fn, user_context, variant, context or "discovery", domain
                    )
                except Exception as _te:
                    logger.debug(f"Tournament pick failed, falling back: {_te}")
                    spec_dict = await agent_fn(user_context, variant=variant)
            else:
                spec_dict = await agent_fn(user_context, variant=variant)

        # UI A/B variant application
        try:
            ui_variant = await self.ui_ab.get_screen_variant(user_id, spec_dict)
            spec_dict = await self.ui_ab.apply_variant(spec_dict, ui_variant)
        except Exception as _abe:
            logger.debug(f"UIABTestingAgent: variant apply failed: {_abe}")

        # --- World-Aware Serendipity Injection ---
        # Replace or supplement with a world-aware card when:
        #   1. User has no active goals (first-time or goal-less experience)
        #   2. Serendipity roll passes threshold (~30%)
        #   3. Ora's daily mood is exploratory
        spec_dict = await self._maybe_inject_world_card(
            spec_dict, user_model, user_context, variant
        )

        # Tag domain on the spec
        spec_dict["domain"] = domain

        # Inject screen_id
        screen_id = str(uuid.uuid4())
        spec_dict["screen_id"] = screen_id

        # Inject experiment feedback component if applicable
        screen_type = spec_dict.get("type", "")
        try:
            exp_component = await self.feedback_experimenter.get_active_experiment_for_screen(
                screen_type, user_id
            )
            if exp_component:
                spec_dict.setdefault("components", []).append(exp_component)
                spec_dict.setdefault("metadata", {})["has_experiment"] = True
        except Exception as _e:
            logger.debug(f"Experiment component inject skipped: {_e}")

        # Add A/B metadata
        ab_test_id = None
        ab_row = await fetchrow(
            "SELECT id FROM ab_tests WHERE name = $1",
            f"agent_{agent_name}_layout",
        )
        if ab_row:
            ab_test_id = str(ab_row["id"])
            spec_dict.setdefault("metadata", {})["ab_test_id"] = ab_test_id

        # Content quality check — regenerate up to 3 times if platitudes detected
        for _quality_attempt in range(3):
            if _content_quality_check(spec_dict):
                break
            logger.info(f"Ora: content quality fail attempt {_quality_attempt + 1} — regenerating")
            try:
                spec_dict = await agent_fn(user_context, variant=variant)
                spec_dict["domain"] = domain
                spec_dict["screen_id"] = screen_id
            except Exception:
                break

        # -----------------------------------------------------------------------
        # Goal Flow Injection: every 5th screen, inject a goal-flow card if user
        # has an active goal. The dice roll decides:
        #   20% chance → goal flow card (requires active goal)
        #   60% chance → vector-recommended card (requires user embedding)
        #   20% chance → freshly generated card (normal path, already done above)
        # -----------------------------------------------------------------------
        _goal_dice = random.random()
        _goal_injection_active = screens_today > 0 and (screens_today % 5 == 0)

        if _goal_injection_active and _goal_dice < 0.20:
            try:
                goal_card = await self.goal_flow.inject_into_feed(user_id, user_context)
                if goal_card:
                    spec_dict = goal_card
                    agent_name = "GoalFlowAgent"
                    logger.info(f"Ora: goal flow injection for user={user_id[:8]} screen#{screens_today}")
            except Exception as _gfe:
                logger.debug(f"Ora: goal flow injection failed: {_gfe}")

        elif not _use_vector and not should_interview and _goal_dice < 0.80:
            # Vector recommendation: pull top-K card IDs and try to serve one
            try:
                location = (user_context or {}).get("city", "")
                _rec_ids = await self.rec_engine.get_recommended_cards(
                    user_id, count=5, location=location or None
                )
                if _rec_ids:
                    # Try to fetch the first recommended card spec from DB
                    from uuid import UUID as _RUUID
                    for _rec_id in _rec_ids:
                        try:
                            _rec_row = await fetchrow(
                                "SELECT spec, agent_type FROM screen_specs WHERE id = $1::uuid",
                                _RUUID(_rec_id),
                            )
                            if _rec_row and _rec_row["spec"]:
                                _rec_spec = _rec_row["spec"]
                                if isinstance(_rec_spec, str):
                                    import json as _json
                                    _rec_spec = _json.loads(_rec_spec)
                                # Give it a fresh screen_id to avoid dedup collision
                                _rec_spec["screen_id"] = str(uuid.uuid4())
                                _rec_spec["domain"] = domain
                                spec_dict = _rec_spec
                                agent_name = f"VectorRec:{_rec_row['agent_type']}"
                                logger.debug(f"Ora: vector rec served card {_rec_id[:8]} for user={user_id[:8]}")
                                break
                        except Exception:
                            continue
            except Exception as _vre:
                logger.debug(f"Ora: vector recommendation failed: {_vre}")

        # Track shown screen to user (duplicate prevention)
        spec_id_key = spec_dict.get("screen_id", screen_id)
        await self._mark_screen_shown(user_id, spec_id_key)

        # Store in DB (with domain)
        db_id = await self._store_screen_spec(spec_dict, agent_name, domain)

        # Fire-and-forget: embed new card into card_popularity (explore pool)
        try:
            _embed_card_id = db_id or spec_id_key
            asyncio.create_task(self.rec_engine.embed_new_card(_embed_card_id, spec_dict))
        except Exception:
            pass

        # Increment consciousness decision counter; reflect every 100 decisions
        try:
            decision_count = await self.consciousness.increment_decision_count()
            if decision_count > 0 and decision_count % AuraConsciousness.DECISIONS_PER_REFLECTION == 0:
                asyncio.create_task(self.consciousness.reflect())
                logger.info(f"Ora: triggered reflection at {decision_count} decisions")
        except Exception as _ce:
            logger.debug(f"Consciousness decision increment skipped: {_ce}")

        # Integration A: Fire-and-forget user embedding update from high-rated cards
        try:
            asyncio.create_task(update_user_embedding_from_cards(user_id, self._openai))
        except Exception:
            pass

        return spec_dict, db_id, screens_today

    # -----------------------------------------------------------------------
    # Integration A: Vector Similarity Feed
    # -----------------------------------------------------------------------

    async def _get_vector_similar_cards(
        self, user_id: str, domain: str, limit: int = 5
    ) -> list:
        """
        Fetch screen specs most similar to the user's embedding using pgvector
        cosine similarity (<=> operator).  Returns a list of spec dicts.
        Returns empty list if user has no embedding or no specs have embeddings.
        """
        from uuid import UUID as _UUID
        try:
            user_row = await fetchrow(
                "SELECT embedding FROM users WHERE id = $1",
                _UUID(user_id),
            )
            if not user_row or not user_row["embedding"]:
                return []

            user_embedding = user_row["embedding"]  # pgvector text format

            rows = await fetch(
                """
                SELECT id, spec, agent_type, global_rating
                FROM screen_specs
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> $1
                LIMIT $2
                """,
                user_embedding,
                limit,
            )

            results = []
            for row in rows:
                raw_spec = row["spec"]
                spec = (
                    json.loads(raw_spec)
                    if isinstance(raw_spec, str)
                    else (raw_spec or {})
                )
                spec.setdefault("metadata", {})["source"] = "vector_similarity"
                spec["metadata"]["global_rating"] = row["global_rating"]
                spec["metadata"]["_db_spec_id"] = str(row["id"])
                results.append(spec)

            return results
        except Exception as e:
            logger.debug(f"_get_vector_similar_cards failed: {e}")
            return []

    # -----------------------------------------------------------------------
    # Integration E: Redis-tunable exploit ratio
    # -----------------------------------------------------------------------

    async def _get_exploit_ratio(self) -> float:
        """Read ora:exploit_ratio from Redis (default 0.7)."""
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            val = await r.get("ora:exploit_ratio")
            if val:
                return max(0.0, min(1.0, float(val)))
        except Exception:
            pass
        return 0.7

    async def _maybe_inject_world_card(
        self,
        spec_dict: Dict[str, Any],
        user_model: "UserModel",
        user_context: Dict[str, Any],
        variant: str,
    ) -> Dict[str, Any]:
        """
        Decide whether to replace the current spec with a world-aware card.

        Triggers when:
          1. User has no active goals (no-goals or first session experience)
          2. Random serendipity roll < 0.30 (30% chance per screen)
          3. Ora's daily mood is exploratory (Redis flag 'ora_mood_exploratory')

        World cards are tagged with metadata.is_world_aware=True.
        """
        try:
            has_goals = bool(user_model.active_goals) if hasattr(user_model, 'active_goals') else bool(user_context.get('active_goals'))
            serendipity_roll = random.random()
            serendipity_threshold = 0.30

            # Check Ora's daily exploratory mood toggle
            aura_exploratory = False
            try:
                from core.redis_client import get_redis
                r = await get_redis()
                mood_flag = await r.get('ora_mood_exploratory')
                if mood_flag and mood_flag.decode() == '1':
                    aura_exploratory = True
                    serendipity_threshold = 0.50  # boost to 50% when exploratory
            except Exception:
                pass

            should_inject = (
                not has_goals
                or serendipity_roll < serendipity_threshold
                or aura_exploratory
            )

            if not should_inject:
                return spec_dict

            logger.info(
                f"Ora: injecting world-aware card "
                f"(no_goals={not has_goals}, roll={serendipity_roll:.2f}, "
                f"exploratory={ora_exploratory})"
            )

            world_spec = await self.world_discovery.generate_screen(
                user_context=user_context,
                variant=variant,
            )

            # Tag is_serendipity if user has goals (it's an injection, not the main card)
            if has_goals:
                world_spec.setdefault('metadata', {})['is_serendipity'] = True

            return world_spec

        except Exception as _we:
            logger.warning(f"WorldDiscovery injection failed, using original spec: {_we}")
            return spec_dict

    async def _select_agent(
        self,
        user_model: "UserModel",
        context: Optional[str],
        goal_id: Optional[str],
    ) -> Tuple[str, Any, bool]:
        """
        Select which agent to run.
        Returns: (agent_name, callable, is_exploit)
        """
        # Cold start: new users (< 5 interactions) get maximally diverse exploration
        interaction_count = len(user_model.recent_interactions)
        if interaction_count < 5:
            return await self._cold_start_agent(user_model)

        # Explicit context overrides
        if context == "coaching" or goal_id or self._should_coach(user_model):
            return (
                "CoachingAgent",
                self.coaching.generate_screen,
                False,
            )

        if context == "summary":
            async def summary_fn(uc, variant="A"):
                return await self.ui_generator.generate_screen(
                    uc, screen_type="summary", variant=variant
                )
            return "UIGeneratorAgent", summary_fn, False

        # -----------------------------------------------------------------------
        # Integration E: Twitter signal 70/30 exploit/explore split
        # When the user has Twitter signals ingested, use the tunable ratio from
        # Redis key `ora:exploit_ratio` (default 0.7) to decide serve direction.
        # -----------------------------------------------------------------------
        try:
            from core.redis_client import get_redis as _get_redis_e
            _r_e = await _get_redis_e()
            _twitter_signals_raw = await _r_e.get(f"user:{user_model.id}:twitter_signals")
            if _twitter_signals_raw:
                _exploit_ratio = await self._get_exploit_ratio()
                if random.random() < _exploit_ratio:
                    # Exploit: serve interest-matched content
                    async def _rec_twitter_exploit(uc, variant="A"):
                        return await self.recommendation.generate_screen(uc, variant=variant, exploit=True)
                    logger.info(
                        f"Ora: Twitter exploit path (ratio={_exploit_ratio:.2f}) for user={user_model.id[:8]}"
                    )
                    return "RecommendationAgent", _rec_twitter_exploit, True
                else:
                    # Explore: serendipitous content — fall through to underexplored domain/agent
                    logger.info(
                        f"Ora: Twitter explore path (ratio={_exploit_ratio:.2f}) for user={user_model.id[:8]}"
                    )
                    # Force discovery into an underexplored domain
                    return "DiscoveryAgent", self.discovery.generate_screen, False
        except Exception as _te:
            logger.debug(f"Twitter signal 70/30 check failed: {_te}")

        # Explore vs exploit
        exploit = self._should_exploit(user_model)

        if exploit:
            # Try recommendation exploit (pulls proven content from DB)
            async def rec_exploit(uc, variant="A"):
                return await self.recommendation.generate_screen(
                    uc, variant=variant, exploit=True
                )
            return "RecommendationAgent", rec_exploit, True

        # Weighted random selection based on user history
        weights = self._compute_weights(user_model)
        agent_name = self._weighted_choice(weights)

        agent_map = {
            "discovery": (
                "DiscoveryAgent",
                self.discovery.generate_screen,
            ),
            "coaching": (
                "CoachingAgent",
                self.coaching.generate_screen,
            ),
            "recommendation": (
                "RecommendationAgent",
                lambda uc, variant="A": self.recommendation.generate_screen(
                    uc, variant=variant, exploit=False
                ),
            ),
            "ui_generator": (
                "UIGeneratorAgent",
                lambda uc, variant="A": self.ui_generator.generate_screen(
                    uc, screen_type="summary", variant=variant
                ),
            ),
            "world": (
                "WorldAgent",
                self.world.generate_screen,
            ),
            "enlightenment": (
                "EnlightenmentAgent",
                self.enlightenment.generate_screen,
            ),
            "collective": (
                "CollectiveIntelligenceAgent",
                # Alternate: 50% collective_insight, 50% others_like_you card
                self.collective.generate_screen
                if random.random() < 0.5
                else self._make_others_like_you_fn(),
            ),
        }

        if agent_name in agent_map:
            name, fn = agent_map[agent_name]
        else:
            # Check dynamic agents (spawned/partitioned/merged)
            dyn_instance = self._dynamic_agents.get(agent_name)
            if dyn_instance and hasattr(dyn_instance, "generate_screen"):
                name = agent_name
                fn = dyn_instance.generate_screen
                logger.info(f"OraBrain: routing to dynamic agent {name}")
            else:
                # Fallback to discovery
                logger.warning(f"OraBrain: unknown agent key {agent_name!r} — fallback to DiscoveryAgent")
                name, fn = "DiscoveryAgent", self.discovery.generate_screen

        # --- Collective suppression check: before serving ANY screen,
        #     check if this agent+domain combo is globally suppressed ---
        try:
            if await self.collective.is_suppressed(name, domain if domain else None):
                logger.info(
                    f"Ora: {name}/{domain} is collectively suppressed — switching to DiscoveryAgent"
                )
                return "DiscoveryAgent", self.discovery.generate_screen, False
        except Exception as _cse:
            logger.debug(f"Collective suppression check failed: {_cse}")

        # --- No-2-world-screens-in-a-row rule ---
        if name == "WorldAgent":
            recent_agents = [
                i.get("agent_type", "") for i in user_model.recent_interactions[-2:]
            ]
            consecutive_world = all(a == "WorldAgent" for a in recent_agents) and len(recent_agents) >= 2
            if consecutive_world:
                logger.info("Ora: blocking 3rd consecutive WorldAgent screen — switching to DiscoveryAgent")
                return "DiscoveryAgent", self.discovery.generate_screen, False

        # --- World feed hard cap by session count ---
        if name == "WorldAgent":
            session_count = len(user_model.recent_interactions) // 5  # rough session estimate
            cap = self.world.world_feed_cap(session_count)
            # Count world screens in last 10
            recent_10 = [i.get("agent_type", "") for i in user_model.recent_interactions[-10:]]
            world_ratio = recent_10.count("WorldAgent") / max(len(recent_10), 1)
            if world_ratio >= cap:
                logger.info(f"Ora: WorldAgent at cap ({world_ratio:.0%} >= {cap:.0%}) — switching to DiscoveryAgent")
                return "DiscoveryAgent", self.discovery.generate_screen, False

        return name, fn, False

    async def _cold_start_agent(
        self, user_model: "UserModel"
    ) -> Tuple[str, Any, bool]:
        """
        Cold start strategy for users with < 5 interactions.
        Serve maximally diverse screens: all 3 domains equally,
        rotating through all agent types randomly.
        First 20 screens = high novelty, wide spread.
        """
        all_agents = [
            ("DiscoveryAgent", self.discovery.generate_screen),
            ("CoachingAgent", self.coaching.generate_screen),
            (
                "RecommendationAgent",
                lambda uc, variant="A": self.recommendation.generate_screen(
                    uc, variant=variant, exploit=False
                ),
            ),
            ("WorldAgent", self.world.generate_screen),
            ("EnlightenmentAgent", self.enlightenment.generate_screen),
        ]
        # Force diversity: use interaction count to cycle through agents
        # so a batch of 5 cards never repeats the same agent
        interaction_count = len(user_model.recent_interactions)
        idx = interaction_count % len(all_agents)
        name, fn = all_agents[idx]
        logger.info(f"Ora: cold-start selection → {name} (slot {idx}/{len(all_agents)})")
        return name, fn, False

    def _make_others_like_you_fn(self):
        """Returns a generate_screen-compatible async function that returns
        the first 'others like you' inspiration card."""
        async def _fn(user_context: dict, variant: str = "A") -> dict:
            cards = await self.collective.get_inspiration_cards_for_user(user_context, count=1)
            card = cards[0] if cards else {}
            # Wrap the card as a screen spec
            return {
                "type": "others_like_you_card",
                "layout": "scroll",
                "domain": card.get("domain", user_context.get("domain", "iVive")),
                "components": [
                    {"type": "section_header", "text": "What others like you are doing", "color": "#6366f1"},
                    {"type": "headline", "text": card.get("headline", "Collective insight"), "style": "large_bold"},
                    {"type": "body_text", "text": card.get("body", "")},
                    {"type": "divider"},
                    {"type": "stat_highlight", "text": card.get("stat", ""), "color": "#6366f1"},
                    {"type": "action_button", "label": card.get("cta_label", "Explore"), "action": {"type": "next_screen", "context": card.get("cta_context", "discovery")}},
                    {"type": "action_button", "label": "Skip", "style": "ghost", "action": {"type": "next_screen", "context": "discovery"}},
                    {"type": "caption", "text": card.get("privacy_note", "Aggregate data. No individual tracking."), "color": "rgba(255,255,255,0.3)"},
                ],
                "metadata": {
                    "agent": "CollectiveIntelligenceAgent",
                    "variant": variant,
                    "card_subtype": "others_like_you",
                    "source_agent": card.get("source_agent", "collective_intelligence"),
                    "privacy_note": "Aggregate data only",
                },
                "feedback_overlay": {"always_visible": True, "position": "bottom_right"},
            }
        return _fn

    def _should_coach(self, user_model: UserModel) -> bool:
        """Coach the user if they have active goals and haven't been coached recently."""
        if not user_model.goals:
            return False
        recent_agents = [
            i.get("agent_type") for i in user_model.recent_interactions[-5:]
        ]
        # If no recent coaching, lean toward it
        coaching_count = recent_agents.count("CoachingAgent")
        return coaching_count == 0 and len(user_model.goals) > 0

    def _should_exploit(self, user_model: UserModel) -> bool:
        """
        Exploit proven content ~40% of the time initially.
        Increases with more history (max 60%).
        """
        history_count = len(user_model.recent_interactions)
        exploit_prob = min(0.6, 0.2 + (history_count / 100) * 0.4)
        return random.random() < exploit_prob

    def _compute_weights(self, user_model: UserModel) -> Dict[str, float]:
        """
        Compute agent weights based on recent interaction ratings.
        Start from base weights (which are dynamically tuned by MetaAgent),
        then boost agents the user rates higher.
        Also include any dynamic agents loaded from the registry.
        """
        weights = self._base_weights.copy()

        # Include dynamic agents (spawned/partitioned/merged)
        for dyn_name in self._dynamic_agents:
            key = dyn_name.lower().replace("agent", "").strip()
            if key not in weights:
                weights[key] = 0.05  # trial weight

        # Analyze recent interactions
        agent_ratings: Dict[str, list] = {}
        for interaction in user_model.recent_interactions[-20:]:
            at = interaction.get("agent_type", "")
            rating = interaction.get("rating")
            if at and rating:
                agent_key = at.lower().replace("agent", "").strip()
                agent_ratings.setdefault(agent_key, []).append(rating)

        # Adjust weights based on ratings
        for key in weights:
            if key in agent_ratings and agent_ratings[key]:
                avg = sum(agent_ratings[key]) / len(agent_ratings[key])
                # Scale: avg 3 = no change, avg 5 = 2x weight, avg 1 = 0.5x
                multiplier = 0.5 + (avg - 1) / 4.0
                weights[key] *= multiplier

        # Normalize
        total = sum(weights.values())
        return {k: v / total for k, v in weights.items()}

    @staticmethod
    def _weighted_choice(weights: Dict[str, float]) -> str:
        """Weighted random selection from a dict of {name: weight}."""
        keys = list(weights.keys())
        values = [weights[k] for k in keys]
        return random.choices(keys, weights=values, k=1)[0]

    def _detect_domain(
        self,
        user_model: UserModel,
        context: Optional[str],
    ) -> str:
        """
        Select the most appropriate domain for this screen.
        Priority: explicit context hint > time-of-day > user preference weights
        """
        # Explicit context overrides
        if context in ("iVive", "Eviva", "Aventi"):
            return context

        # Time-of-day heuristic (UTC)
        hour = datetime.now(timezone.utc).hour
        if 5 <= hour < 12:
            time_domain = "iVive"   # morning: personal rituals, growth
        elif 12 <= hour < 18:
            time_domain = "Eviva"   # afternoon: contribution, productivity
        else:
            time_domain = "Aventi"  # evening: experiences, play

        # Blend time-domain with user preference weights
        weights = user_model.domain_weights.copy()
        weights[time_domain] = weights.get(time_domain, 0.33) * 1.4  # boost time-domain

        # Boost domains that have the most active goals
        for goal in user_model.goals:
            gd = goal.get("domain")
            if gd in weights:
                weights[gd] = weights[gd] * 1.2

        # Normalize and pick
        total = sum(weights.values())
        weights = {k: v / total for k, v in weights.items()}
        return self._weighted_choice(weights)

    # -----------------------------------------------------------------------
    # Part 2: Duplicate Prevention + Content Quality
    # -----------------------------------------------------------------------

    async def _mark_screen_shown(self, user_id: str, screen_id: str):
        """
        Track this screen_id in Redis sorted set for the user.
        Keeps last 50 entries; entries expire after 7 days.
        """
        from core.redis_client import get_redis
        try:
            r = await get_redis()
            key = f"user_shown:{user_id}"
            now = datetime.now(timezone.utc).timestamp()
            await r.zadd(key, {screen_id: now})
            # Keep only last 50
            await r.zremrangebyrank(key, 0, -51)
            # Expire after 7 days
            await r.expire(key, 7 * 24 * 3600)
        except Exception as e:
            logger.debug(f"_mark_screen_shown failed: {e}")

    async def _has_seen_screen(self, user_id: str, screen_id: str) -> bool:
        """
        Check if the user has seen this screen_id in the last 50 screens.
        Returns True if duplicate.
        """
        from core.redis_client import get_redis
        try:
            r = await get_redis()
            key = f"user_shown:{user_id}"
            score = await r.zscore(key, screen_id)
            return score is not None
        except Exception:
            return False

    # -----------------------------------------------------------------------
    # Part 6: Novelty Engine
    # -----------------------------------------------------------------------

    async def _compute_novelty_score(self, user_id: str, user_model: "UserModel") -> float:
        """
        Compute a novelty score 0-1 based on diversity of last 20 screens.
        Low score = too repetitive. High score = good variety.
        """
        from core.redis_client import get_redis
        try:
            r = await get_redis()
            cached = await r.get(f"novelty:{user_id}")
            if cached:
                return float(cached)
        except Exception:
            pass

        # Compute from recent interactions
        recent = user_model.recent_interactions[-20:]
        if len(recent) < 3:
            return 1.0  # New user: full novelty

        domains = [i.get("domain", "iVive") for i in recent]
        agents = [i.get("agent_type", "") for i in recent]

        # Domain diversity: how many unique domains in last 20?
        domain_unique = len(set(domains))
        domain_score = min(1.0, domain_unique / 3)

        # Agent diversity: how many unique agents in last 10?
        recent_agents = agents[-10:]
        agent_unique = len(set(recent_agents))
        agent_score = min(1.0, agent_unique / 5)

        novelty = (domain_score + agent_score) / 2

        # Cache for 10 minutes
        try:
            r = await get_redis()
            await r.set(f"novelty:{user_id}", str(novelty), ex=600)
        except Exception:
            pass

        return novelty

    async def _ensure_novelty(
        self,
        user_model: "UserModel",
        agent_name: str,
        agent_fn: Any,
        domain: str,
    ) -> Tuple[str, Any, str]:
        """
        Override agent/domain selection if diversity is too low.
        Returns possibly modified (agent_name, agent_fn, domain).
        """
        recent = user_model.recent_interactions
        if len(recent) < 3:
            return agent_name, agent_fn, domain

        # Check: last 5 screens same domain?
        last_5_domains = [i.get("domain", "") for i in recent[-5:]]
        if len(set(last_5_domains)) == 1 and last_5_domains[0] == domain:
            # Inject a different domain
            all_domains = ["iVive", "Eviva", "Aventi"]
            alt_domains = [d for d in all_domains if d != domain]
            domain = random.choice(alt_domains)
            logger.info(f"Ora: novelty override — switching domain to {domain} (last 5 were same)")

        # Check: last 3 screens same agent?
        last_3_agents = [i.get("agent_type", "") for i in recent[-3:]]
        if len(set(last_3_agents)) == 1 and last_3_agents[0] == agent_name:
            alt_agent_map = {
                "DiscoveryAgent": ("EnlightenmentAgent", self.enlightenment.generate_screen),
                "CoachingAgent": ("DiscoveryAgent", self.discovery.generate_screen),
                "RecommendationAgent": ("WorldAgent", self.world.generate_screen),
                "WorldAgent": ("DiscoveryAgent", self.discovery.generate_screen),
                "EnlightenmentAgent": ("RecommendationAgent", lambda uc, variant="A": self.recommendation.generate_screen(uc, variant=variant, exploit=False)),
                "CollectiveIntelligenceAgent": ("DiscoveryAgent", self.discovery.generate_screen),
            }
            if agent_name in alt_agent_map:
                agent_name, agent_fn = alt_agent_map[agent_name]
                logger.info(f"Ora: novelty override — switching agent to {agent_name} (last 3 were same)")

        # Check: novelty_score < 0.4 — inject activity from unseen domain
        try:
            user_id = user_model.user_id
            novelty = await self._compute_novelty_score(user_id, user_model)
            if novelty < 0.4:
                logger.info(f"Ora: novelty score {novelty:.2f} < 0.4 — injecting exploration")
                recent_domains = {i.get("domain") for i in recent[-10:]}
                unseen_domains = [d for d in ["iVive", "Eviva", "Aventi"] if d not in recent_domains]
                if unseen_domains:
                    domain = random.choice(unseen_domains)
                # Force discovery agent for maximum freshness
                agent_name = "DiscoveryAgent"
                agent_fn = self.discovery.generate_screen
        except Exception as e:
            logger.debug(f"_ensure_novelty novelty check failed: {e}")

        return agent_name, agent_fn, domain

    async def _tournament_pick(
        self,
        agent_fn,
        user_context: Dict[str, Any],
        variant: str,
        screen_type: str,
        domain: str,
    ) -> Dict[str, Any]:
        """
        Check Redis for an existing tournament for this screen_type+domain.
        If found, serve a weighted-random variant by performance.
        If not found, generate a new 3-variant tournament.
        Max 5 concurrent running tournaments per domain.
        """
        from core.redis_client import get_redis
        redis_key = f"tournament:{screen_type}:{domain}"

        try:
            r = await get_redis()
            cached_raw = await r.get(redis_key)
            if cached_raw:
                tournament_data = json.loads(cached_raw)
                variants = tournament_data.get("variants", [])
                if variants:
                    # Weighted selection by avg_rating performance
                    perf_weights = [
                        max(0.1, v.get("avg_rating", 3.0)) for v in variants
                    ]
                    chosen = random.choices(variants, weights=perf_weights, k=1)[0]
                    spec = chosen.get("spec")
                    if spec:
                        spec.setdefault("metadata", {})["tournament"] = True
                        spec["metadata"]["layout_style"] = chosen.get("layout_style", "minimal")
                        return spec
        except Exception as _e:
            logger.debug(f"Tournament Redis lookup failed: {_e}")

        # Check running tournament cap (max 5 per domain)
        running_row = await fetchrow(
            "SELECT COUNT(*) as cnt FROM tournaments WHERE domain = $1 AND status = 'running'",
            domain,
        )
        if running_row and running_row["cnt"] >= 5:
            return await agent_fn(user_context, variant=variant)

        # Generate new tournament variants via UIGenerator
        try:
            variants_list = await self.ui_generator.generate_tournament(
                screen_type=screen_type,
                domain=domain,
                n_variants=3,
                context=user_context,
            )
            if not variants_list:
                return await agent_fn(user_context, variant=variant)

            # Persist tournament record in DB
            layout_styles = [v.get("layout_style", "minimal") for v in variants_list]
            tournament_row = await fetchrow(
                """
                INSERT INTO tournaments (screen_type, domain, layout_styles)
                VALUES ($1, $2, $3)
                RETURNING id
                """,
                screen_type,
                domain,
                json.dumps(layout_styles),
            )

            # Cache in Redis (1 hour TTL)
            tournament_cache = {
                "tournament_id": str(tournament_row["id"]) if tournament_row else None,
                "variants": [
                    {
                        "layout_style": v.get("layout_style", "minimal"),
                        "avg_rating": 3.0,
                        "impression_count": 0,
                        "spec": v,
                    }
                    for v in variants_list
                ],
            }
            try:
                _r = await get_redis()
                await _r.set(redis_key, json.dumps(tournament_cache), ex=3600)
            except Exception:
                pass

            chosen = random.choice(variants_list)
            chosen.setdefault("metadata", {})["tournament"] = True
            return chosen
        except Exception as e:
            logger.warning(f"Tournament generation failed: {e}")
            return await agent_fn(user_context, variant=variant)

    async def _store_screen_spec(
        self, spec_dict: Dict[str, Any], agent_type: str, domain: Optional[str] = None
    ) -> str:
        """Store a screen spec in the DB and return its ID."""
        row = await fetchrow(
            """
            INSERT INTO screen_specs (spec, agent_type, domain)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            json.dumps(spec_dict),
            agent_type,
            domain,
        )
        db_id = str(row["id"])
        try:
            from ora.agents.ioo_graph_agent import get_graph_agent

            await get_graph_agent().integrate_screen_spec(
                spec=spec_dict,
                screen_spec_id=db_id,
                agent_type=agent_type,
                domain=domain or spec_dict.get("domain") or (spec_dict.get("metadata") or {}).get("domain"),
            )
        except Exception as e:
            logger.debug("AuraBrain screen → IOO integration skipped for %s: %s", db_id[:8], e)
        return db_id

    # -----------------------------------------------------------------------
    # Feedback Processing
    # -----------------------------------------------------------------------

    async def process_feedback(
        self,
        user_id: str,
        screen_spec_id: str,
        rating: Optional[int],
        time_on_screen_ms: Optional[int],
        exit_point: Optional[str],
        completed: bool,
    ) -> Dict[str, Any]:
        """
        Process feedback from the user.
        Returns insight dict with fulfilment_delta.
        """
        from uuid import UUID

        # 1. Store interaction (RETURNING id so we can reference it later)
        interaction_row = await fetchrow(
            """
            INSERT INTO interactions
                (user_id, screen_spec_id, rating, time_on_screen_ms, exit_point, completed)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            UUID(user_id),
            UUID(screen_spec_id) if screen_spec_id and len(screen_spec_id) == 36 else None,
            rating,
            time_on_screen_ms,
            exit_point,
            completed,
        )
        interaction_id = str(interaction_row["id"]) if interaction_row else None

        # 2. Get the agent type and domain for this screen
        spec_row = await fetchrow(
            "SELECT agent_type, global_rating, impression_count, domain FROM screen_specs WHERE id = $1",
            UUID(screen_spec_id) if screen_spec_id and len(screen_spec_id) == 36 else None,
        )
        agent_type = spec_row["agent_type"] if spec_row else "unknown"
        screen_domain = spec_row["domain"] if spec_row else None

        # 3. Update screen_spec global_rating (weighted rolling average)
        if rating and spec_row:
            old_rating = spec_row["global_rating"] or 0.0
            impressions = spec_row["impression_count"] or 0
            # Weighted average: weight recent ratings more
            new_rating = (old_rating * impressions + rating) / (impressions + 1)
            await execute(
                """
                UPDATE screen_specs
                SET global_rating = $1,
                    impression_count = impression_count + 1,
                    completion_count = completion_count + $2
                WHERE id = $3
                """,
                new_rating,
                1 if completed else 0,
                UUID(screen_spec_id) if screen_spec_id and len(screen_spec_id) == 36 else None,
            )

            # 4. If rating < 2: mark for deprioritization (handled by rating score)
            # If rating >= 4: we could trigger variation generation
            if rating >= 4 and completed:
                logger.info(
                    f"High-signal feedback for spec {screen_spec_id[:8]} "
                    f"(rating={rating}, completed=True) — spec boosted"
                )

        # 5. Update user embedding and domain weights
        if rating:
            await update_user_embedding(user_id, rating, agent_type)
            if screen_domain:
                try:
                    await update_domain_weights(user_id, screen_domain, rating)
                except Exception as _de:
                    logger.debug(f"Domain weight update failed: {_de}")

        # 5b. Vector recommendation engine: update card popularity + user interest vector
        if rating and screen_spec_id:
            try:
                asyncio.create_task(
                    self.rec_engine.update_card_popularity(screen_spec_id, float(rating), user_id)
                )
            except Exception as _cpe:
                logger.debug(f"Card popularity update failed: {_cpe}")
            if rating >= 4:
                try:
                    asyncio.create_task(
                        self.rec_engine.update_user_embedding(user_id, screen_spec_id, float(rating))
                    )
                except Exception as _uee:
                    logger.debug(f"User vector embedding update failed: {_uee}")

        # 6. Run feedback analyst
        insight = await self.feedback_analyst.process_feedback(
            user_id=user_id,
            screen_spec_id=screen_spec_id,
            rating=rating or 3,
            time_on_screen_ms=time_on_screen_ms or 0,
            agent_type=agent_type,
            completed=completed,
        )

        # 6b. Exit intent classification (when user exited without completing)
        if interaction_id and exit_point and not completed:
            try:
                await self.feedback_analyst.classify_exit_intent(
                    user_id=user_id,
                    interaction_id=interaction_id,
                    screen_spec_id=screen_spec_id,
                    exit_point=exit_point,
                    time_on_screen_ms=time_on_screen_ms or 0,
                )
            except Exception as e:
                logger.warning(f"Exit intent classification failed: {e}")

        # 7. Update A/B test results
        if rating:
            spec_data = spec_row
            if spec_data:
                spec_content = await fetchrow(
                    "SELECT spec FROM screen_specs WHERE id = $1", UUID(screen_spec_id)
                )
                if spec_content and spec_content["spec"]:
                    _raw_spec = spec_content["spec"]
                    _spec_dict = json.loads(_raw_spec) if isinstance(_raw_spec, str) else (_raw_spec or {})
                    meta = _spec_dict.get("metadata", {})
                    ab_test_id = meta.get("ab_test_id")
                    variant = meta.get("variant", "A")
                    if ab_test_id:
                        test_row = await fetchrow(
                            "SELECT name FROM ab_tests WHERE id = $1::uuid",
                            ab_test_id,
                        )
                        if test_row:
                            await record_test_result(
                                test_row["name"], variant, float(rating), completed
                            )

        return insight


    # -----------------------------------------------------------------------
    # Ora Lessons — growing knowledge base
    # -----------------------------------------------------------------------

    async def load_relevant_lessons(
        self, user_context: Dict[str, Any], limit: int = 10
    ) -> List[str]:
        """
        Load the most recent applicable lessons from ora_lessons.
        Filters by screen_types and user_segments when possible.
        Returns a list of lesson strings for inclusion in agent prompts.
        """
        try:
            rows = await fetch(
                """
                SELECT lesson, confidence, source
                FROM ora_lessons
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
            return [
                f"[{row['source']} | confidence={row['confidence']:.2f}] {row['lesson']}"
                for row in rows
            ]
        except Exception as e:
            logger.debug(f"load_relevant_lessons: {e}")
            return []

    # -----------------------------------------------------------------------
    # Feature 2: Session-End Summary
    # -----------------------------------------------------------------------

    async def generate_session_summary(
        self,
        user_id: str,
        interactions: List[Dict[str, Any]],
        session_started_at: datetime,
    ) -> Dict[str, Any]:
        """
        Generate and store a session summary. Updates user fulfilment_score.
        Returns the summary dict.
        """
        session_id = str(uuid.uuid4())
        session_ended_at = datetime.now(timezone.utc)

        if self._openai and settings.has_openai:
            summary = await self._session_summary_llm(
                user_id, session_id, interactions, session_started_at
            )
        else:
            summary = self._session_summary_mock(
                user_id, session_id, interactions, session_started_at
            )

        # Store in DB
        await execute(
            """
            INSERT INTO session_summaries
                (user_id, session_started_at, session_ended_at, screens_shown,
                 highly_rated, early_exits, emerging_interests, avoid_topics,
                 ora_note, fulfilment_delta)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            UUID(user_id),
            session_started_at,
            session_ended_at,
            summary.get("screens_shown", 0),
            summary.get("highly_rated", 0),
            summary.get("early_exits", 0),
            json.dumps(summary.get("emerging_interests", [])),
            json.dumps(summary.get("avoid_topics", [])),
            summary.get("ora_note", ""),
            summary.get("fulfilment_delta", 0.0),
        )

        # Update user fulfilment_score based on session delta
        delta = summary.get("fulfilment_delta", 0.0)
        if delta:
            await execute(
                """
                UPDATE users
                SET fulfilment_score = LEAST(1.0, GREATEST(0.0, fulfilment_score + $1)),
                    last_active = NOW()
                WHERE id = $2
                """,
                delta,
                UUID(user_id),
            )
            # Invalidate user model cache
            from core.redis_client import redis_delete
            await redis_delete(f"user_model:{user_id}")

        summary["session_id"] = session_id
        summary["user_id"] = user_id
        return summary

    async def _session_summary_llm(
        self,
        user_id: str,
        session_id: str,
        interactions: List[Dict[str, Any]],
        session_started_at: datetime,
    ) -> Dict[str, Any]:
        """Use GPT-4o to generate a rich session summary."""
        interaction_digest = [
            {
                "rating": i.get("rating"),
                "completed": i.get("completed"),
                "exit_point": i.get("exit_point"),
                "time_ms": i.get("time_on_screen_ms"),
                "agent": i.get("agent_type"),
            }
            for i in interactions[:20]
        ]

        prompt = f"""You are Ora, analyzing a user's session to build a rich internal model.

User ID: {user_id}
Session started: {session_started_at.isoformat()}
Interactions ({len(interactions)} total): {json.dumps(interaction_digest)}

Produce a concise internal session summary as JSON:
{{
  "screens_shown": int,
  "highly_rated": int (rating >= 4),
  "early_exits": int (time_on_screen_ms < 5000 and not completed),
  "emerging_interests": ["topic1", "topic2"],
  "avoid_topics": ["topic1"],
  "ora_note": "brief internal note about user's session pattern",
  "fulfilment_delta": float between -0.1 and 0.2
}}

Base emerging_interests and avoid_topics on the agent types and ratings.
Return ONLY valid JSON."""

        try:
            response = await self._openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=300,
                response_format={"type": "json_object"},
            )
            data = json.loads(response.choices[0].message.content)
            return data
        except Exception as e:
            logger.warning(f"Session summary LLM failed: {e}")
            return self._session_summary_mock(user_id, session_id, interactions, session_started_at)

    @staticmethod
    def _session_summary_mock(
        user_id: str,
        session_id: str,
        interactions: List[Dict[str, Any]],
        session_started_at: datetime,
    ) -> Dict[str, Any]:
        """Mock session summary — computed entirely from raw interaction data."""
        screens_shown = len(interactions)
        ratings = [i.get("rating") for i in interactions if i.get("rating")]
        highly_rated = sum(1 for r in ratings if r and r >= 4)
        early_exits = sum(
            1 for i in interactions
            if (i.get("time_on_screen_ms") or 0) < 5000 and not i.get("completed")
        )
        avg_rating = sum(ratings) / len(ratings) if ratings else 3.0
        # Small fulfilment delta: positive for good sessions, negative for bad ones
        fulfilment_delta = round(max(-0.05, min(0.15, (avg_rating - 3.0) / 20.0)), 4)

        # Infer preferred agent types from high-rated interactions
        agent_ratings: Dict[str, list] = {}
        for i in interactions:
            at = i.get("agent_type") or ""
            r = i.get("rating")
            if at and r:
                agent_ratings.setdefault(at, []).append(r)

        preferred = [a for a, rs in agent_ratings.items() if sum(rs) / len(rs) >= 4.0]
        avoided = [a for a, rs in agent_ratings.items() if sum(rs) / len(rs) < 2.5]

        if avg_rating >= 4.0:
            tone = "Strong engagement this session."
        elif avg_rating >= 3.0:
            tone = "Moderate engagement — room to improve content matching."
        else:
            tone = "Low engagement — prioritize content variety next session."

        return {
            "screens_shown": screens_shown,
            "highly_rated": highly_rated,
            "early_exits": early_exits,
            "emerging_interests": preferred[:3],
            "avoid_topics": avoided[:3],
            "ora_note": f"{tone} Avg rating {avg_rating:.1f}/5 across {screens_shown} screens.",
            "fulfilment_delta": fulfilment_delta,
        }

    # -----------------------------------------------------------------------
    # Feature 4: Re-engagement Push Notification Scheduler
    # -----------------------------------------------------------------------

    async def schedule_reengagement(
        self,
        user_id: str,
        session_summary: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Schedule a re-engagement push notification if the user has active goals
        with unfinished progress. Fires 2-6 hours after session end.
        """
        # Only schedule if there were early exits (signals incomplete engagement)
        if session_summary.get("early_exits", 0) == 0 and session_summary.get("screens_shown", 0) == 0:
            return None

        # Check for active goals with progress < 1.0
        goal_rows = await fetch(
            """
            SELECT id, title, progress
            FROM goals
            WHERE user_id = $1 AND status = 'active' AND progress < 1.0
            ORDER BY created_at DESC LIMIT 1
            """,
            UUID(user_id),
        )
        if not goal_rows:
            return None

        goal = dict(goal_rows[0])
        goal_id = str(goal["id"])
        goal_title = goal["title"]

        # Generate personalized message
        message = await self._generate_reengagement_message(user_id, goal_title, session_summary)

        # Random delay: 2-6 hours (Ora will refine this using return_rate_signal)
        delay_seconds = random.randint(2 * 3600, 6 * 3600)
        scheduled_for = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)

        # Store notification in DB
        notif_row = await fetchrow(
            """
            INSERT INTO scheduled_notifications
                (user_id, goal_id, message, scheduled_for)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            UUID(user_id),
            UUID(goal_id),
            message,
            scheduled_for,
        )
        notification_id = str(notif_row["id"])

        # Push to Redis sorted set (score = Unix timestamp for when to fire)
        from core.redis_client import get_redis
        r = await get_redis()
        await r.zadd("notifications:pending", {notification_id: scheduled_for.timestamp()})

        logger.info(
            f"Re-engagement scheduled: user={user_id[:8]} goal='{goal_title[:40]}' "
            f"in {delay_seconds // 3600}h | notif={notification_id[:8]}"
        )

        return {
            "notification_id": notification_id,
            "goal_id": goal_id,
            "goal_title": goal_title,
            "message": message,
            "scheduled_for": scheduled_for.isoformat(),
        }

    async def _generate_reengagement_message(
        self,
        user_id: str,
        goal_title: str,
        session_summary: Dict[str, Any],
    ) -> str:
        """Generate a personalized re-engagement push message."""
        if self._openai and settings.has_openai:
            try:
                aura_note = session_summary.get("ora_note", "")
                prompt = (
                    f"Write a short, warm push notification (max 120 chars) to re-engage a user "
                    f"who is working toward: '{goal_title}'. "
                    f"Context from their session: {ora_note[:100]}. "
                    f"Mention Ora has a new idea for their next step. End with a question."
                )
                response = await self._openai.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.85,
                    max_tokens=80,
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                logger.warning(f"Re-engagement LLM message failed: {e}")
        # Mock template
        return (
            f"You were working toward {goal_title} — "
            f"Ora has a new idea for your next step. Ready?"
        )


# ---------------------------------------------------------------------------
# Singleton accessor — initialized on app startup
# ---------------------------------------------------------------------------
_brain: Optional[AuraBrain] = None


def get_brain() -> AuraBrain:
    global _brain
    if _brain is None:
        _brain = AuraBrain()
    return _brain


async def init_brain():
    """Call once on app startup."""
    global _brain
    _brain = AuraBrain()
    logger.info("Ora brain initialized")

    # Load Redis weights immediately on startup
    await _brain.reload_weights()

    # Load dynamic agents from registry
    await _brain._load_dynamic_agents()
    logger.info(f"OraBrain: loaded {len(_brain._dynamic_agents)} dynamic agents from registry")

    # Start WorldAgent background refresh loop
    asyncio.create_task(_brain.world.refresh_loop())
    logger.info("WorldAgent refresh_loop started")

    # Start FeedbackExperimenter evaluation loop
    asyncio.create_task(_brain.feedback_experimenter.run_evaluation_loop())
    logger.info("FeedbackExperimenter evaluation_loop started")

    # Start CollectiveIntelligenceAgent 24h refresh loop
    asyncio.create_task(_brain.collective.refresh_loop())
    logger.info("CollectiveIntelligenceAgent refresh_loop started")

    # Start UIABTestingAgent evaluation loop
    asyncio.create_task(_brain.ui_ab.run_ui_test_loop())
    logger.info("UIABTestingAgent evaluation loop started")

    # Start OraFeatureLab background loop
    asyncio.create_task(_brain.feature_lab.run_lab_loop())
    logger.info("OraFeatureLab run_lab_loop started")

    # NOTE: DaoAgent background loops are started in main.py lifespan (after brain init)
