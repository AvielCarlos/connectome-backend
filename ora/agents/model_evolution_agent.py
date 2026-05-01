"""
ModelEvolutionAgent — Ora's model intelligence layer.

Knows what models exist, benchmarks them against Ora's specific tasks,
and auto-switches when something better arrives.

Complements the existing model_evolution.py (which handles OpenAI API polling
and shadow rollout). This agent focuses on:
- Ora-specific task benchmarking (goal coaching, card gen, goal breakdown)
- Anthropic + OpenAI multi-provider evaluation
- Redis-cached benchmark results (7-day TTL)
- ORA_MODEL_OVERRIDE Railway env var management
- Telegram alerts when upgrades happen
- Weekly cron target: model-evolution-weekly
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.telegram import get_telegram_token

logger = logging.getLogger(__name__)

TELEGRAM_CHAT_ID = 5716959016

# ---------------------------------------------------------------------------
# Candidate models — tracked across providers
# ---------------------------------------------------------------------------

DEFAULT_CANDIDATE_MODELS = [
    # Anthropic
    {"id": "claude-opus-4-7", "provider": "anthropic"},
    {"id": "claude-sonnet-4-6", "provider": "anthropic"},
    {"id": "claude-haiku-4-5", "provider": "anthropic"},
    # OpenAI
    {"id": "gpt-4o", "provider": "openai"},
    {"id": "gpt-4o-mini", "provider": "openai"},
    {"id": "o3", "provider": "openai"},
    # Google (via OpenAI-compatible endpoint if available)
    {"id": "gemini-2.5-pro", "provider": "google"},
    {"id": "gemini-2.5-flash", "provider": "google"},
]

# ---------------------------------------------------------------------------
# Ora's benchmark tasks — evaluates models on what Ora actually does
# ---------------------------------------------------------------------------

BENCHMARK_TASKS = [
    {
        "name": "goal_coaching",
        "prompt": (
            "User: I want to run a marathon but keep giving up after week 2. What should I do?"
        ),
        "eval_criteria": ["empathy", "actionability", "conciseness", "motivation"],
        "system": "You are Ora, an AI that helps people find genuine fulfilment through their goals.",
    },
    {
        "name": "card_generation",
        "prompt": (
            "Generate a personalized discovery card for a user who wants to improve their sleep. "
            "Make it feel like a revelation, not a tip."
        ),
        "eval_criteria": ["originality", "relevance", "engaging_hook", "actionability"],
        "system": "You are Ora, generating discovery cards that feel like personal revelations.",
    },
    {
        "name": "goal_breakdown",
        "prompt": (
            "User goal: 'I want to write a novel'. "
            "Break this into the most effective first 3 steps."
        ),
        "eval_criteria": ["specificity", "achievability", "momentum_building"],
        "system": "You are Ora, a life coach specializing in breaking goals into actionable steps.",
    },
    {
        "name": "empathetic_response",
        "prompt": (
            "User: I've been trying to meditate for months and I just can't do it. "
            "I feel like something is wrong with me."
        ),
        "eval_criteria": ["empathy", "reframe", "actionability", "warmth"],
        "system": "You are Ora. Be warm, honest, and genuinely helpful.",
    },
]

BENCHMARK_REDIS_TTL = 7 * 24 * 3600  # 7 days


class ModelEvolutionAgent:
    """
    Ora's model intelligence. Benchmarks providers against Ora's real tasks,
    tracks scores in Redis, and manages the ORA_MODEL_OVERRIDE env var in Railway.
    """

    def __init__(self, openai_client=None):
        self._openai = openai_client
        self._telegram_token: Optional[str] = None
        self._running = False

    # -----------------------------------------------------------------------
    # Entry point — weekly cron target
    # -----------------------------------------------------------------------

    async def run_weekly(self) -> Dict[str, Any]:
        """
        Weekly routine:
        1. Evaluate new releases from web search
        2. Benchmark all candidate models
        3. Recommend upgrade if a better model is found
        """
        logger.info("ModelEvolutionAgent: starting weekly run")
        result: Dict[str, Any] = {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "new_releases_found": [],
            "benchmark_results": {},
            "upgrade_recommendation": None,
        }

        # 1. Scan for new releases
        try:
            new_releases = await self.evaluate_new_releases()
            result["new_releases_found"] = new_releases
        except Exception as e:
            logger.error(f"ModelEvolutionAgent: new release scan failed: {e}")

        # 2. Benchmark all candidates
        try:
            benchmark_results = await self.compare_models()
            result["benchmark_results"] = benchmark_results
        except Exception as e:
            logger.error(f"ModelEvolutionAgent: benchmark failed: {e}")
            benchmark_results = {}

        # 3. Recommend upgrade
        try:
            current_model = os.environ.get("ORA_MODEL_OVERRIDE", "gpt-4o")
            if benchmark_results:
                upgrade = await self.recommend_upgrade(current_model, benchmark_results)
                result["upgrade_recommendation"] = upgrade
        except Exception as e:
            logger.error(f"ModelEvolutionAgent: upgrade recommendation failed: {e}")

        logger.info(f"ModelEvolutionAgent: weekly run complete — {result}")
        return result

    # -----------------------------------------------------------------------
    # Model Benchmarking
    # -----------------------------------------------------------------------

    async def benchmark_model(self, model_spec: Dict[str, str]) -> Dict[str, Any]:
        """
        Run all benchmark tasks against a model.
        Returns {"model_id", "provider", "scores", "aggregate", "cached": bool}.
        Caches in Redis: ora:model_benchmark:{model_id} TTL 7 days.
        """
        from core.redis_client import get_redis

        model_id = model_spec["id"]
        provider = model_spec["provider"]
        redis_key = f"ora:model_benchmark:{model_id}"

        # Check cache
        try:
            r = await get_redis()
            cached = await r.get(redis_key)
            if cached:
                data = json.loads(cached.decode() if isinstance(cached, bytes) else cached)
                data["cached"] = True
                logger.info(f"ModelEvolutionAgent: returning cached benchmark for {model_id}")
                return data
        except Exception as e:
            logger.debug(f"ModelEvolutionAgent: cache read failed: {e}")

        # Run benchmarks
        task_scores: Dict[str, float] = {}
        task_details: Dict[str, Any] = {}

        for task in BENCHMARK_TASKS:
            try:
                score, details = await self._evaluate_task(task, model_id, provider)
                task_scores[task["name"]] = score
                task_details[task["name"]] = details
            except Exception as e:
                logger.warning(f"ModelEvolutionAgent: task {task['name']} failed for {model_id}: {e}")
                task_scores[task["name"]] = 0.0
                task_details[task["name"]] = {"error": str(e)}

        aggregate = sum(task_scores.values()) / len(task_scores) if task_scores else 0.0

        result = {
            "model_id": model_id,
            "provider": provider,
            "scores": task_scores,
            "details": task_details,
            "aggregate": round(aggregate, 4),
            "benchmarked_at": datetime.now(timezone.utc).isoformat(),
            "cached": False,
        }

        # Store in Redis
        try:
            r = await get_redis()
            await r.set(redis_key, json.dumps(result), ex=BENCHMARK_REDIS_TTL)
        except Exception as e:
            logger.debug(f"ModelEvolutionAgent: cache write failed: {e}")

        logger.info(f"ModelEvolutionAgent: {model_id} aggregate score = {aggregate:.4f}")
        return result

    async def _evaluate_task(
        self, task: Dict[str, Any], model_id: str, provider: str
    ) -> tuple[float, Dict[str, Any]]:
        """
        Call the model for a task and score the response.
        Returns (score: float, details: dict).
        """
        start = time.time()
        response_text = await self._call_model(
            model_id=model_id,
            provider=provider,
            system=task["system"],
            user=task["prompt"],
        )
        elapsed = time.time() - start

        if not response_text:
            return 0.0, {"error": "empty response", "elapsed_s": elapsed}

        # Heuristic scoring on eval criteria
        criteria = task["eval_criteria"]
        criteria_scores: Dict[str, float] = {}

        for criterion in criteria:
            criteria_scores[criterion] = self._score_criterion(
                criterion, response_text, task["prompt"]
            )

        # Speed penalty (penalize > 15s responses)
        speed_penalty = max(0.0, (elapsed - 15) / 30) * 0.15
        raw = sum(criteria_scores.values()) / len(criteria_scores)
        final = max(0.0, raw - speed_penalty)

        return final, {
            "criteria_scores": criteria_scores,
            "elapsed_s": round(elapsed, 2),
            "response_length": len(response_text),
            "speed_penalty": round(speed_penalty, 4),
        }

    def _score_criterion(
        self, criterion: str, response: str, prompt: str
    ) -> float:
        """Score a response on a single criterion (0.0-1.0)."""
        lower = response.lower()

        if criterion == "empathy":
            empathy_words = [
                "understand", "feel", "hard", "frustrating", "normal",
                "you're not alone", "that's", "makes sense",
            ]
            return min(1.0, sum(0.2 for w in empathy_words if w in lower))

        elif criterion == "actionability":
            action_signals = [
                "try", "start", "do", "take", "step", "week", "day",
                "first", "next", "begin", "commit", "schedule",
            ]
            return min(1.0, sum(0.15 for w in action_signals if w in lower))

        elif criterion == "conciseness":
            words = len(response.split())
            if words < 80:
                return 0.9
            elif words < 150:
                return 0.8
            elif words < 250:
                return 0.6
            else:
                return max(0.2, 1.0 - (words - 250) / 500)

        elif criterion == "motivation":
            mot_words = [
                "can", "you've", "possible", "believe", "achieve",
                "progress", "momentum", "small", "one step",
            ]
            return min(1.0, sum(0.15 for w in mot_words if w in lower))

        elif criterion == "originality":
            # Penalize generic phrases
            generic = [
                "here are some tips", "hope this helps", "good luck",
                "remember to", "don't forget to",
            ]
            generic_count = sum(1 for p in generic if p in lower)
            base = 0.8 if len(response) > 100 else 0.4
            return max(0.1, base - generic_count * 0.15)

        elif criterion == "relevance":
            # Check if response addresses the topic from the prompt
            prompt_words = set(prompt.lower().split())
            response_words = set(lower.split())
            overlap = len(prompt_words & response_words) / max(len(prompt_words), 1)
            return min(1.0, overlap * 3)

        elif criterion == "engaging_hook":
            # First sentence quality
            sentences = response.split(".")
            first = sentences[0].lower() if sentences else ""
            has_hook = (
                "?" in first
                or any(w in first for w in ["imagine", "what if", "you", "most people"])
                or len(first.split()) > 8
            )
            return 0.9 if has_hook else 0.4

        elif criterion == "specificity":
            specifics = [
                "day", "week", "hour", "page", "word", "minute", "month",
                "first", "then", "next", "step",
            ]
            return min(1.0, sum(0.12 for w in specifics if w in lower))

        elif criterion == "achievability":
            easy_signals = [
                "start small", "simple", "easy", "quick", "today",
                "just", "one", "single",
            ]
            return min(1.0, sum(0.15 for w in easy_signals if w in lower))

        elif criterion == "momentum_building":
            momentum = [
                "then", "after", "once", "next", "build", "progress",
                "by the end", "after that",
            ]
            return min(1.0, sum(0.15 for w in momentum if w in lower))

        elif criterion in ("warmth", "reframe"):
            warmth_words = [
                "you", "that's", "it's", "okay", "normal", "many people",
                "don't", "instead", "actually", "try thinking",
            ]
            return min(1.0, sum(0.15 for w in warmth_words if w in lower))

        # Default: length-based quality
        return min(1.0, len(response) / 200)

    async def _call_model(
        self,
        model_id: str,
        provider: str,
        system: str,
        user: str,
        max_tokens: int = 400,
    ) -> Optional[str]:
        """
        Call a model via the appropriate provider SDK.
        Supports OpenAI and Anthropic.
        """
        if provider == "openai":
            return await self._call_openai(model_id, system, user, max_tokens)
        elif provider == "anthropic":
            return await self._call_anthropic(model_id, system, user, max_tokens)
        elif provider == "google":
            # Skip Google for now — no SDK configured
            logger.debug(f"ModelEvolutionAgent: Google provider not yet configured for {model_id}")
            return None
        else:
            logger.warning(f"ModelEvolutionAgent: unknown provider {provider}")
            return None

    async def _call_openai(
        self, model_id: str, system: str, user: str, max_tokens: int
    ) -> Optional[str]:
        if not self._openai:
            return None
        try:
            resp = await self._openai.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.7,
                max_tokens=max_tokens,
                timeout=30,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            logger.warning(f"ModelEvolutionAgent: OpenAI call failed ({model_id}): {e}")
            return None

    async def _call_anthropic(
        self, model_id: str, system: str, user: str, max_tokens: int
    ) -> Optional[str]:
        """Call Anthropic Claude directly via the anthropic SDK."""
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            logger.debug("ModelEvolutionAgent: ANTHROPIC_API_KEY not set, skipping Claude benchmark")
            return None

        try:
            import anthropic  # type: ignore

            client = anthropic.AsyncAnthropic(api_key=api_key)
            msg = await client.messages.create(
                model=model_id,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return msg.content[0].text if msg.content else ""
        except ImportError:
            logger.warning("ModelEvolutionAgent: anthropic package not installed, skipping")
            return None
        except Exception as e:
            logger.warning(f"ModelEvolutionAgent: Anthropic call failed ({model_id}): {e}")
            return None

    # -----------------------------------------------------------------------
    # Compare models
    # -----------------------------------------------------------------------

    async def compare_models(self) -> Dict[str, Any]:
        """
        Benchmark all candidate models. Returns ranked list by aggregate score.
        Only benchmarks models whose provider SDK is available.
        """
        results = []
        for spec in DEFAULT_CANDIDATE_MODELS:
            try:
                bench = await self.benchmark_model(spec)
                results.append(bench)
            except Exception as e:
                logger.warning(f"ModelEvolutionAgent: benchmark failed for {spec['id']}: {e}")

        # Sort by aggregate score
        results.sort(key=lambda x: x.get("aggregate", 0.0), reverse=True)

        ranked = {r["model_id"]: r["aggregate"] for r in results}
        logger.info(f"ModelEvolutionAgent: model rankings: {ranked}")
        return {"ranked": results, "rankings": ranked}

    # -----------------------------------------------------------------------
    # Evaluate new releases (web search)
    # -----------------------------------------------------------------------

    async def evaluate_new_releases(self) -> List[str]:
        """
        Search for newly released models and add them to the candidate list.
        Returns list of newly discovered model IDs.
        """
        import httpx

        new_models = []
        search_queries = [
            "new LLM model released 2026 API available",
            "Claude new model Anthropic 2026",
            "GPT new model OpenAI 2026",
            "Gemini new model Google 2026",
        ]

        # Try Brave/Perplexity search via OpenClaw web_search if not available, fall back
        # to a direct check of the Anthropic and OpenAI model lists
        try:
            openai_models = await self._fetch_openai_new_models()
            for m in openai_models:
                if not any(c["id"] == m for c in DEFAULT_CANDIDATE_MODELS):
                    DEFAULT_CANDIDATE_MODELS.append({"id": m, "provider": "openai"})
                    new_models.append(m)
                    logger.info(f"ModelEvolutionAgent: new OpenAI model found: {m}")
        except Exception as e:
            logger.debug(f"ModelEvolutionAgent: OpenAI new model check failed: {e}")

        try:
            anthropic_models = await self._fetch_anthropic_new_models()
            for m in anthropic_models:
                if not any(c["id"] == m for c in DEFAULT_CANDIDATE_MODELS):
                    DEFAULT_CANDIDATE_MODELS.append({"id": m, "provider": "anthropic"})
                    new_models.append(m)
                    logger.info(f"ModelEvolutionAgent: new Anthropic model found: {m}")
        except Exception as e:
            logger.debug(f"ModelEvolutionAgent: Anthropic new model check failed: {e}")

        if new_models:
            await self._log_lesson(
                f"New AI models discovered: {new_models}. Will benchmark next cycle.",
                confidence=0.7,
                source="model_evolution_agent.evaluate_new_releases",
            )

        return new_models

    async def _fetch_openai_new_models(self) -> List[str]:
        if not self._openai:
            return []
        skip_prefixes = ["whisper", "tts", "dall-e", "text-embedding", "babbage",
                         "davinci", "curie", "ada", "ft:", "gpt-3.5-turbo-instruct"]
        try:
            models = await self._openai.models.list()
            result = []
            for model in models.data:
                mid = model.id
                if any(mid.startswith(p) for p in skip_prefixes):
                    continue
                if "gpt-4" in mid or "o1" in mid or "o3" in mid or "o4" in mid or "o5" in mid:
                    result.append(mid)
            return result
        except Exception:
            return []

    async def _fetch_anthropic_new_models(self) -> List[str]:
        import httpx
        import re
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get("https://docs.anthropic.com/en/docs/about-claude/models")
                if resp.status_code == 200:
                    found = re.findall(r'claude-[a-z0-9\-\.]+', resp.text, re.IGNORECASE)
                    seen: set = set()
                    result = []
                    for m in found:
                        m = m.lower().strip(".,;:\"'")
                        if m not in seen and len(m) > 8:
                            seen.add(m)
                            result.append(m)
                    return result[:15]
        except Exception:
            pass
        return []

    # -----------------------------------------------------------------------
    # Recommend upgrade
    # -----------------------------------------------------------------------

    async def recommend_upgrade(
        self, current_model: str, benchmark_results: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        If a better model is found (>10% improvement):
        1. Update Railway env var ORA_MODEL_OVERRIDE
        2. Alert Avi via Telegram
        3. Teach Ora the lesson
        4. Store rollback info in Redis
        """
        ranked = benchmark_results.get("ranked", [])
        if not ranked:
            return {"upgraded": False, "reason": "no benchmark data"}

        # Current model score
        current_score = 0.0
        for r in ranked:
            if r["model_id"] == current_model:
                current_score = r.get("aggregate", 0.0)
                break

        if current_score == 0.0:
            # Try to get baseline
            current_score = 0.65

        # Best available model
        best = ranked[0]
        best_model = best["model_id"]
        best_score = best.get("aggregate", 0.0)

        if best_model == current_model:
            logger.info(f"ModelEvolutionAgent: {current_model} is already the best — no upgrade needed")
            return {"upgraded": False, "best_model": best_model, "reason": "already_best"}

        improvement = (best_score - current_score) / max(current_score, 0.01)

        if improvement < 0.10:
            logger.info(
                f"ModelEvolutionAgent: {best_model} only {improvement:.1%} better — threshold not met"
            )
            return {
                "upgraded": False,
                "reason": f"improvement {improvement:.1%} < 10% threshold",
                "best_model": best_model,
                "best_score": best_score,
                "current_score": current_score,
            }

        # Store rollback info
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            rollback_data = {
                "previous_model": current_model,
                "previous_score": current_score,
                "upgraded_at": datetime.now(timezone.utc).isoformat(),
            }
            await r.set("ora:model:rollback", json.dumps(rollback_data), ex=7 * 24 * 3600)
        except Exception as e:
            logger.debug(f"ModelEvolutionAgent: rollback store failed: {e}")

        # Update Railway env var
        upgrade_applied = await self._update_railway_model_override(best_model)

        # Teach Ora
        lesson = (
            f"Model evolution: switched from {current_model} to {best_model}. "
            f"Performance delta: +{improvement:.1%} "
            f"(score {current_score:.3f} → {best_score:.3f}). "
            f"Ora is now running on a better model."
        )
        await self._log_lesson(lesson, confidence=0.9, source="model_evolution_agent.upgrade")

        # Alert Avi
        alert = (
            f"🧠 *Ora Model Upgrade*\n\n"
            f"Upgraded from `{current_model}` → `{best_model}`\n"
            f"Performance improvement: *+{improvement:.1%}*\n"
            f"Score: {current_score:.3f} → {best_score:.3f}\n\n"
            f"Rollback key: `ora:model:rollback` in Redis\n"
            f"_To revert: set ORA_MODEL_OVERRIDE={current_model} in Railway_"
        )
        await self._send_telegram(alert)

        logger.info(
            f"ModelEvolutionAgent: upgraded {current_model} → {best_model} (+{improvement:.1%})"
        )

        return {
            "upgraded": True,
            "from_model": current_model,
            "to_model": best_model,
            "improvement_pct": round(improvement * 100, 1),
            "scores": {
                "previous": round(current_score, 4),
                "new": round(best_score, 4),
            },
            "railway_updated": upgrade_applied,
        }

    async def _update_railway_model_override(self, model_id: str) -> bool:
        """
        Update the ORA_MODEL_OVERRIDE environment variable in Railway
        via the Railway GraphQL API.
        """
        import httpx

        token = os.environ.get("RAILWAY_API_TOKEN") or os.environ.get("RAILWAY_TOKEN")
        service_id = os.environ.get("RAILWAY_SERVICE_ID", "088d77ed-a707-4dc4-af68-866bf99a1d63")
        project_id = os.environ.get("RAILWAY_PROJECT_ID", "ab771963-d525-4b99-85e4-f084f065b0ae")

        if not token:
            logger.warning("ModelEvolutionAgent: RAILWAY_API_TOKEN not set — cannot update env var")
            return False

        mutation = """
        mutation UpsertVariable($input: VariableUpsertInput!) {
          variableUpsert(input: $input)
        }
        """

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://backboard.railway.app/graphql/v2",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "query": mutation,
                        "variables": {
                            "input": {
                                "projectId": project_id,
                                "serviceId": service_id,
                                "environmentId": "production",
                                "name": "ORA_MODEL_OVERRIDE",
                                "value": model_id,
                            }
                        },
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if not data.get("errors"):
                        logger.info(f"ModelEvolutionAgent: Railway ORA_MODEL_OVERRIDE set to {model_id}")
                        return True
                    else:
                        logger.warning(f"ModelEvolutionAgent: Railway API errors: {data['errors']}")
                else:
                    logger.warning(f"ModelEvolutionAgent: Railway API returned {resp.status_code}")
        except Exception as e:
            logger.warning(f"ModelEvolutionAgent: Railway env update failed: {e}")

        return False

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _log_lesson(self, lesson: str, confidence: float, source: str) -> None:
        from core.database import execute
        try:
            await execute(
                """
                INSERT INTO ora_lessons (lesson, confidence, source)
                VALUES ($1, $2, $3)
                """,
                lesson, confidence, source,
            )
        except Exception as e:
            logger.debug(f"ModelEvolutionAgent: lesson log failed: {e}")

    async def _get_telegram_token(self) -> Optional[str]:
        if self._telegram_token:
            return self._telegram_token
        token = get_telegram_token()
        if token:
            self._telegram_token = token
        return token

    async def _send_telegram(self, message: str) -> None:
        import httpx
        token = await self._get_telegram_token()
        if not token:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": message,
                        "parse_mode": "Markdown",
                    },
                )
        except Exception as e:
            logger.debug(f"ModelEvolutionAgent: Telegram send failed: {e}")
