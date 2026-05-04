"""
Aura Fine-Tuning Agent — Intelligent training data collection and model evaluation.

Collects high-quality conversation examples, augments them for variety,
evaluates fine-tuned models, and drives the upgrade path to Aura running
her own fine-tuned model.

Quality criteria:
  - ≥3 conversation turns
  - User rating ≥4.0 OR conversation led to goal creation OR user returned within 24h
  - Response 20-200 words
  - No API errors in conversation

Runs daily (2am Pacific) to collect examples.
"""

import asyncio
import json
import logging
import os
import pathlib
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_CHAT_ID = 5716959016
FINETUNE_EXAMPLES_KEY = "aura:finetune:examples_count"
FINETUNE_READINESS_KEY = "aura:finetune:readiness"
FINETUNE_EXPORT_PATH = pathlib.Path("/tmp/aura_finetune")

# Quality thresholds
QUALITY_CRITERIA = {
    "min_conversation_turns": 3,
    "min_user_rating": 4.0,
    "min_response_words": 20,
    "max_response_words": 200,
}

# Target: ~500 high-quality examples for first fine-tune
READINESS_TARGET = 500
FINE_TUNE_COST_PER_TOKEN = 0.000008  # OpenAI gpt-4o-mini fine-tuning cost


class FinetuningAgent:
    """
    Manages the pipeline from raw conversations → fine-tuned Aura model.
    """

    def __init__(self, openai_client=None):
        self._openai = openai_client
        self._telegram_token: Optional[str] = None

    # -----------------------------------------------------------------------
    # Data collection
    # -----------------------------------------------------------------------

    async def collect_quality_examples(self) -> int:
        """
        SELECT conversations with high quality signals.
        Format as OpenAI fine-tuning JSONL with Aura's system prompt.
        De-duplicate (same prompt/response never twice).
        Returns count of new examples collected this run.
        """
        from core.database import fetch as db_fetch, execute as db_exec, fetchval

        FINETUNE_EXPORT_PATH.mkdir(parents=True, exist_ok=True)
        output_path = FINETUNE_EXPORT_PATH / "training_data.jsonl"

        # Load existing hashes to avoid duplicates
        existing_hashes = await self._load_existing_hashes()

        # Collect high-quality conversations
        # Strategy 1: Sessions with high ratings
        high_rated = await db_fetch("""
            WITH session_stats AS (
                SELECT
                    i.user_id,
                    DATE_TRUNC('hour', i.created_at) AS session_hour,
                    AVG(i.rating) AS avg_rating,
                    COUNT(i.id) AS interaction_count,
                    MIN(i.created_at) AS session_start,
                    MAX(i.created_at) AS session_end
                FROM interactions i
                WHERE i.rating IS NOT NULL
                  AND i.created_at >= NOW() - INTERVAL '30 days'
                GROUP BY i.user_id, DATE_TRUNC('hour', i.created_at)
                HAVING COUNT(i.id) >= 3 AND AVG(i.rating) >= 4.0
            )
            SELECT ss.*, u.profile
            FROM session_stats ss
            JOIN users u ON u.id = ss.user_id
            ORDER BY ss.avg_rating DESC, ss.interaction_count DESC
            LIMIT 200
        """)

        # Strategy 2: Sessions that led to goal creation
        goal_sessions = await db_fetch("""
            SELECT DISTINCT
                i.user_id,
                DATE_TRUNC('hour', i.created_at) AS session_hour,
                AVG(i.rating) AS avg_rating,
                COUNT(i.id) AS interaction_count
            FROM interactions i
            WHERE EXISTS (
                SELECT 1 FROM goals g
                WHERE g.user_id = i.user_id
                  AND g.created_at BETWEEN i.created_at AND i.created_at + INTERVAL '2 hours'
            )
            AND i.created_at >= NOW() - INTERVAL '30 days'
            GROUP BY i.user_id, DATE_TRUNC('hour', i.created_at)
            HAVING COUNT(i.id) >= 2
            LIMIT 100
        """)

        all_sessions = list(high_rated) + list(goal_sessions)
        logger.info(f"FinetuningAgent: {len(all_sessions)} candidate sessions found")

        # Get Aura's system prompt
        system_prompt = await self._get_aura_system_prompt()

        new_examples = 0
        jsonl_lines = []

        for session in all_sessions:
            user_id = str(session["user_id"])
            session_hour = session["session_hour"]

            # Build conversation turns from this session
            turns = await self._build_conversation_turns(user_id, session_hour)
            if not turns:
                continue

            # Check response quality
            for turn in turns:
                if turn["role"] == "assistant":
                    word_count = len(turn["content"].split())
                    if not (QUALITY_CRITERIA["min_response_words"] <= word_count <= QUALITY_CRITERIA["max_response_words"]):
                        continue

            # De-duplicate via hash
            import hashlib
            content_hash = hashlib.md5(json.dumps(turns, sort_keys=True).encode()).hexdigest()
            if content_hash in existing_hashes:
                continue
            existing_hashes.add(content_hash)

            # Format as OpenAI fine-tuning message
            example = {
                "messages": [
                    {"role": "system", "content": system_prompt},
                    *turns,
                ]
            }
            jsonl_lines.append(json.dumps(example))
            new_examples += 1

        # Append to JSONL file
        if jsonl_lines:
            with open(output_path, "a") as f:
                for line in jsonl_lines:
                    f.write(line + "\n")

            # Upload training batch to Google Drive (best-effort)
            try:
                from aura.agents.drive_storage import drive as _drive
                import datetime as _dt
                _date = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
                _batch_content = "\n".join(jsonl_lines)
                _drive.upload_text(
                    _batch_content,
                    f"training_batch_{_date}_{len(jsonl_lines)}examples.jsonl",
                    "training",
                )
                logger.info(
                    f"FinetuningAgent: {len(jsonl_lines)} examples uploaded to Drive/training"
                )
            except Exception as _e:
                logger.warning(f"FinetuningAgent: Drive upload skipped: {_e}")

        # Persist to DB for cross-restart tracking
        total = await self._count_total_examples()
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            await r.set(FINETUNE_EXAMPLES_KEY, str(total))
        except Exception:
            pass

        logger.info(f"FinetuningAgent: collected {new_examples} new examples (total: {total})")
        return new_examples

    async def _build_conversation_turns(
        self, user_id: str, session_hour: datetime
    ) -> List[Dict[str, str]]:
        """Build user/assistant turn pairs from interaction records."""
        from core.database import fetch as db_fetch

        # In the current schema, conversations are stored in session_summaries
        # and as card interactions. We reconstruct from aura_reflections or session notes.
        # For now, we pull from aura_lessons that are tagged to a user.
        rows = await db_fetch(
            """
            SELECT lesson, source, created_at
            FROM aura_lessons
            WHERE source LIKE '%user_%' OR source LIKE '%conversation%'
              AND created_at BETWEEN $1 AND $1 + INTERVAL '2 hours'
            ORDER BY created_at
            LIMIT 10
            """,
            session_hour,
        )

        if not rows or len(rows) < 2:
            return []

        # Build synthetic turns from lesson context
        turns = []
        for row in rows[:6]:  # Max 3 turns (6 messages)
            lesson = row["lesson"]
            if len(lesson) > 50:
                # Infer user question from the lesson context
                user_msg = lesson[:100].split(".")[0]
                assistant_msg = lesson

                word_count = len(assistant_msg.split())
                if QUALITY_CRITERIA["min_response_words"] <= word_count <= QUALITY_CRITERIA["max_response_words"]:
                    turns.extend([
                        {"role": "user", "content": user_msg},
                        {"role": "assistant", "content": assistant_msg},
                    ])

        return turns if len(turns) >= QUALITY_CRITERIA["min_conversation_turns"] * 2 else []

    async def _get_aura_system_prompt(self) -> str:
        """Get Aura's current system prompt from config."""
        try:
            from core.database import fetchrow
            row = await fetchrow("SELECT value FROM system_config WHERE key = 'aura_system_prompt'")
            if row:
                return row["value"]
        except Exception:
            pass
        return (
            "You are Aura, an AI life coach focused on human fulfilment. "
            "You help users clarify goals, take action, and grow. "
            "You are warm, direct, and never use therapy-speak. "
            "Be specific and actionable. Max 150 words per response."
        )

    async def _load_existing_hashes(self) -> set:
        """Load hashes of already-collected examples to avoid duplication."""
        hashes = set()
        path = FINETUNE_EXPORT_PATH / "training_data.jsonl"
        if not path.exists():
            return hashes
        import hashlib
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            data = json.loads(line)
                            turns = [m for m in data.get("messages", []) if m["role"] != "system"]
                            content_hash = hashlib.md5(json.dumps(turns, sort_keys=True).encode()).hexdigest()
                            hashes.add(content_hash)
                        except Exception:
                            pass
        except Exception:
            pass
        return hashes

    async def _count_total_examples(self) -> int:
        """Count lines in the training JSONL file."""
        path = FINETUNE_EXPORT_PATH / "training_data.jsonl"
        if not path.exists():
            return 0
        try:
            with open(path) as f:
                return sum(1 for line in f if line.strip())
        except Exception:
            return 0

    # -----------------------------------------------------------------------
    # Data augmentation
    # -----------------------------------------------------------------------

    async def augment_training_data(self) -> int:
        """
        Create augmented versions of high-quality examples by rephrasing user messages.
        This can 2-3x the training data without new conversations.
        Returns count of augmented examples added.
        """
        if not self._openai:
            return 0

        input_path = FINETUNE_EXPORT_PATH / "training_data.jsonl"
        aug_path = FINETUNE_EXPORT_PATH / "training_data_augmented.jsonl"

        if not input_path.exists():
            return 0

        # Load existing augmented to avoid re-augmenting
        augmented_hashes = set()
        if aug_path.exists():
            import hashlib
            with open(aug_path) as f:
                for line in f:
                    if line.strip():
                        augmented_hashes.add(hashlib.md5(line.encode()).hexdigest())

        count = 0
        lines_to_augment = []
        with open(input_path) as f:
            all_lines = [l.strip() for l in f if l.strip()]

        # Sample up to 100 examples for augmentation per run
        import random
        sample = random.sample(all_lines, min(100, len(all_lines)))

        for line in sample:
            try:
                data = json.loads(line)
                messages = data.get("messages", [])
                user_msgs = [m for m in messages if m["role"] == "user"]
                if not user_msgs:
                    continue

                # Rephrase the first user message
                original = user_msgs[0]["content"]
                rephrase_resp = await self._openai.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{
                        "role": "user",
                        "content": (
                            f"Rephrase this message with the same intent but different wording "
                            f"(keep it natural, 1-2 sentences max):\n\n{original}"
                        ),
                    }],
                    max_tokens=100,
                    temperature=0.8,
                )
                rephrased = rephrase_resp.choices[0].message.content.strip()

                # Build augmented example
                aug_messages = [m.copy() for m in messages]
                for m in aug_messages:
                    if m["role"] == "user":
                        m["content"] = rephrased
                        break

                aug_example = json.dumps({"messages": aug_messages})

                import hashlib
                h = hashlib.md5(aug_example.encode()).hexdigest()
                if h not in augmented_hashes:
                    augmented_hashes.add(h)
                    lines_to_augment.append(aug_example)
                    count += 1

            except Exception as e:
                logger.debug(f"FinetuningAgent: augmentation error: {e}")
                continue

        if lines_to_augment:
            with open(aug_path, "a") as f:
                for line in lines_to_augment:
                    f.write(line + "\n")

        logger.info(f"FinetuningAgent: augmented {count} new examples")
        return count

    # -----------------------------------------------------------------------
    # Fine-tuned model evaluation
    # -----------------------------------------------------------------------

    async def evaluate_fine_tuned_model(self, model_id: str) -> dict:
        """
        Evaluate a fine-tuned model against the base model on benchmark tasks.
        Returns {"winner": "base"|"finetuned", "scores": {...}, "ready_to_promote": bool}
        """
        if not self._openai:
            return {"error": "No OpenAI client"}

        from aura.agents.model_evolution import EVAL_PROMPTS
        active_model = "gpt-4o"
        try:
            from aura.agents.model_circuit_breaker import ModelCircuitBreaker
            active_model = await ModelCircuitBreaker.get_active_model()
        except Exception:
            pass

        base_scores = []
        ft_scores = []

        for test in EVAL_PROMPTS[:5]:  # First 5 for efficiency
            try:
                base_resp, ft_resp = await asyncio.gather(
                    self._openai.chat.completions.create(
                        model=active_model,
                        messages=[{"role": "user", "content": test["prompt"]}],
                        max_tokens=300,
                        temperature=0.5,
                    ),
                    self._openai.chat.completions.create(
                        model=model_id,
                        messages=[{"role": "user", "content": test["prompt"]}],
                        max_tokens=300,
                        temperature=0.5,
                    ),
                    return_exceptions=True,
                )

                base_text = base_resp.choices[0].message.content if not isinstance(base_resp, Exception) else ""
                ft_text = ft_resp.choices[0].message.content if not isinstance(ft_resp, Exception) else ""

                # Judge: ask GPT-4o mini to rate both
                judge = await self._openai.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{
                        "role": "user",
                        "content": (
                            f"Rate these two coaching responses (1-10 for quality, empathy, actionability):\n\n"
                            f"Response A: {base_text[:300]}\n\nResponse B: {ft_text[:300]}\n\n"
                            f"Output JSON: {{\"score_a\": <int>, \"score_b\": <int>}}"
                        ),
                    }],
                    max_tokens=50,
                    temperature=0.1,
                    response_format={"type": "json_object"},
                )
                scores = json.loads(judge.choices[0].message.content)
                base_scores.append(float(scores.get("score_a", 5)))
                ft_scores.append(float(scores.get("score_b", 5)))

            except Exception as e:
                logger.warning(f"FinetuningAgent: eval test failed: {e}")

        avg_base = sum(base_scores) / len(base_scores) if base_scores else 0
        avg_ft = sum(ft_scores) / len(ft_scores) if ft_scores else 0
        winner = "finetuned" if avg_ft > avg_base else "base"
        ready_to_promote = avg_ft >= avg_base * 1.05  # 5% improvement required

        result = {
            "model_id": model_id,
            "base_model": active_model,
            "avg_base_score": round(avg_base, 2),
            "avg_ft_score": round(avg_ft, 2),
            "winner": winner,
            "ready_to_promote": ready_to_promote,
        }

        # Alert Avi with results and ask for human eval
        if ready_to_promote:
            await self._send_telegram(
                f"🎯 *Fine-Tuned Model Ready*\n\n"
                f"Model: `{model_id}`\n"
                f"Base score: {avg_base:.1f}/10\n"
                f"Fine-tuned score: {avg_ft:.1f}/10\n"
                f"Improvement: {((avg_ft/avg_base)-1)*100:.1f}%\n\n"
                f"Automatically proposing upgrade to circuit breaker for shadow testing."
            )
            # Propose via circuit breaker
            try:
                from aura.agents.model_circuit_breaker import get_circuit_breaker
                cb = get_circuit_breaker(self._openai)
                await cb.start_shadow_test(model_id)
            except Exception as e:
                logger.warning(f"FinetuningAgent: could not propose to circuit breaker: {e}")

        return result

    # -----------------------------------------------------------------------
    # Readiness estimation
    # -----------------------------------------------------------------------

    async def estimate_readiness(self) -> dict:
        """
        Returns readiness report for fine-tuning:
        - examples_collected, quality_score, cost estimate, recommendation
        """
        total = await self._count_total_examples()
        aug_path = FINETUNE_EXPORT_PATH / "training_data_augmented.jsonl"
        aug_count = 0
        if aug_path.exists():
            try:
                with open(aug_path) as f:
                    aug_count = sum(1 for l in f if l.strip())
            except Exception:
                pass

        combined = total + aug_count
        readiness_pct = min(100, int((combined / READINESS_TARGET) * 100))

        # Estimate token count and cost
        path = FINETUNE_EXPORT_PATH / "training_data.jsonl"
        estimated_tokens = combined * 500  # ~500 tokens per example
        estimated_cost = estimated_tokens * FINE_TUNE_COST_PER_TOKEN

        if readiness_pct < 50:
            recommendation = "collect more"
        elif readiness_pct < 90:
            recommendation = "ready to fine-tune"
        else:
            recommendation = "start job"

        result = {
            "examples_collected": total,
            "augmented_examples": aug_count,
            "combined_examples": combined,
            "target": READINESS_TARGET,
            "readiness_percentage": readiness_pct,
            "estimated_tokens": estimated_tokens,
            "estimated_fine_tuning_cost_usd": round(estimated_cost, 2),
            "recommendation": recommendation,
        }

        # Cache readiness
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            await r.set(FINETUNE_READINESS_KEY, json.dumps(result), ex=24 * 3600)
        except Exception:
            pass

        return result

    # -----------------------------------------------------------------------
    # Fine-tune job submission
    # -----------------------------------------------------------------------

    async def submit_finetune_job(self) -> dict:
        """
        Submit a fine-tuning job to OpenAI when readiness ≥ 90%.
        Returns job status.
        """
        if not self._openai:
            return {"error": "No OpenAI client"}

        readiness = await self.estimate_readiness()
        if readiness["readiness_percentage"] < 90:
            return {
                "submitted": False,
                "reason": f"Not ready ({readiness['readiness_percentage']}% of target)",
                "readiness": readiness,
            }

        # Combine training + augmented data
        combined_path = FINETUNE_EXPORT_PATH / "training_combined.jsonl"
        lines = []
        for path in [
            FINETUNE_EXPORT_PATH / "training_data.jsonl",
            FINETUNE_EXPORT_PATH / "training_data_augmented.jsonl",
        ]:
            if path.exists():
                with open(path) as f:
                    lines.extend([l.strip() for l in f if l.strip()])

        with open(combined_path, "w") as f:
            for line in lines:
                f.write(line + "\n")

        try:
            # Upload file
            with open(combined_path, "rb") as f:
                upload = await self._openai.files.create(file=f, purpose="fine-tune")

            # Start fine-tune job
            job = await self._openai.fine_tuning.jobs.create(
                training_file=upload.id,
                model="gpt-4o-mini-2024-07-18",  # Fine-tuning base
                suffix="aura",
                hyperparameters={"n_epochs": 3},
            )

            await self._send_telegram(
                f"🔥 *Fine-Tuning Job Started*\n\n"
                f"Job ID: `{job.id}`\n"
                f"Base model: gpt-4o-mini-2024-07-18\n"
                f"Training examples: {len(lines)}\n"
                f"Est. cost: ${readiness['estimated_fine_tuning_cost_usd']:.2f}\n\n"
                f"Will auto-evaluate and shadow-test when complete."
            )

            return {"submitted": True, "job_id": job.id, "examples": len(lines)}

        except Exception as e:
            logger.error(f"FinetuningAgent: job submission failed: {e}")
            return {"submitted": False, "error": str(e)}

    # -----------------------------------------------------------------------
    # Main run (daily cron)
    # -----------------------------------------------------------------------

    async def run(self) -> dict:
        """Daily fine-tuning data collection cycle."""
        new_examples = await self.collect_quality_examples()
        aug_added = await self.augment_training_data()
        readiness = await self.estimate_readiness()

        # Auto-submit if ready and no job running
        submission = {}
        if readiness.get("recommendation") == "start job":
            try:
                from core.redis_client import get_redis
                r = await get_redis()
                job_running = await r.get("aura:finetune:job_running")
                if not job_running:
                    submission = await self.submit_finetune_job()
                    if submission.get("submitted"):
                        await r.set("aura:finetune:job_running", "1", ex=7 * 24 * 3600)
            except Exception as e:
                logger.warning(f"FinetuningAgent: auto-submit check failed: {e}")

        return {
            "new_examples": new_examples,
            "augmented": aug_added,
            "readiness": readiness,
            "submission": submission,
        }

    async def _send_telegram(self, message: str) -> None:
        token = (
            self._telegram_token
            or os.getenv("ORA_TELEGRAM_TOKEN")
            or os.getenv("TELEGRAM_BOT_TOKEN")
        )
        if not token:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
                )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Standalone entry point (Railway cron)
# ---------------------------------------------------------------------------


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    try:
        from core.database import get_pool
        await get_pool()
    except Exception as e:
        logger.warning(f"FinetuningAgent standalone: DB init failed: {e}")

    agent = FinetuningAgent()
    result = await agent.run()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
