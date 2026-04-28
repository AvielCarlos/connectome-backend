"""
FinetuningAgent — Ora builds her own specialized models.

Collects high-quality conversation data from production interactions,
formats it for OpenAI fine-tuning, and manages the full fine-tuning lifecycle.

Stages:
  1. Data collection (daily, always running)
  2. Readiness check (>500 quality examples)
  3. Fine-tune via OpenAI API when ready (requires Avi approval)
  4. Benchmark fine-tuned model vs base
  5. Deploy via ORA_MODEL_OVERRIDE env var

Cron target: finetuning-daily-collect (daily 2am Pacific)
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TELEGRAM_CHAT_ID = 5716959016
TRAINING_DATA_DIR = "/tmp/finetuning"
MIN_EXAMPLES_FOR_FINETUNING = 500
RECOMMENDED_BASE_MODEL = "gpt-4o-mini"

# Cost estimate: ~$0.008 per 1K tokens for gpt-4o-mini fine-tuning
# Average example ~200 tokens
COST_PER_EXAMPLE_USD = 0.0016


class FinetuningAgent:
    """
    Builds Ora's own specialized models from accumulated conversation data.
    Collects high-quality examples daily, monitors readiness, and manages
    the full fine-tuning lifecycle.
    """

    def __init__(self, openai_client=None):
        self._openai = openai_client
        self._telegram_token: Optional[str] = None

    # -----------------------------------------------------------------------
    # Entry point — daily cron target
    # -----------------------------------------------------------------------

    async def run_daily(self) -> Dict[str, Any]:
        """
        Daily: collect training data, check readiness.
        Weekly trigger: if ready, alert Avi to approve fine-tuning.
        """
        logger.info("FinetuningAgent: starting daily run")
        result: Dict[str, Any] = {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "examples_collected": 0,
            "total_examples": 0,
            "ready": False,
        }

        # 1. Collect training data
        try:
            count = await self.collect_training_data()
            result["examples_collected"] = count
            logger.info(f"FinetuningAgent: collected {count} new training examples")
        except Exception as e:
            logger.error(f"FinetuningAgent: data collection failed: {e}")

        # 2. Check readiness
        try:
            readiness = await self.check_readiness()
            result.update(readiness)
            logger.info(f"FinetuningAgent: readiness check — {readiness}")
        except Exception as e:
            logger.error(f"FinetuningAgent: readiness check failed: {e}")

        # 3. If ready, notify Avi (weekly — check if we already notified this week)
        if result.get("ready"):
            try:
                await self._notify_readiness(result)
            except Exception as e:
                logger.error(f"FinetuningAgent: readiness notification failed: {e}")

        return result

    # -----------------------------------------------------------------------
    # Data Collection
    # -----------------------------------------------------------------------

    async def collect_training_data(self) -> int:
        """
        Pull high-quality conversation pairs from DB and append to JSONL file.

        Quality signals:
        - Conversations where user rated cards ≥ 4 after the exchange
        - Conversations that led to goal creation
        - Ora's responses followed by return visits (session within 48h)

        Returns count of new examples collected.
        """
        from core.database import fetch
        import os

        os.makedirs(TRAINING_DATA_DIR, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        output_file = f"{TRAINING_DATA_DIR}/training_data_{today}.jsonl"

        # Already collected today?
        if os.path.exists(output_file):
            with open(output_file) as f:
                existing = sum(1 for _ in f)
            logger.info(f"FinetuningAgent: {output_file} already has {existing} examples")
            return 0

        examples = []

        # Signal 1: Conversations after which user rated cards ≥ 4
        try:
            high_rated_convos = await fetch(
                """
                SELECT DISTINCT oc.user_id,
                       oc.message as user_message,
                       (
                           SELECT message FROM ora_conversations
                           WHERE user_id = oc.user_id AND role = 'ora'
                             AND created_at > oc.created_at
                           ORDER BY created_at ASC LIMIT 1
                       ) as ora_reply
                FROM ora_conversations oc
                JOIN interactions i ON i.user_id = oc.user_id
                    AND i.created_at > oc.created_at
                    AND i.created_at < oc.created_at + INTERVAL '10 minutes'
                    AND i.rating >= 4
                WHERE oc.role = 'user'
                  AND oc.created_at >= NOW() - INTERVAL '90 days'
                  AND LENGTH(oc.message) > 20
                LIMIT 300
                """,
            )
            for row in high_rated_convos:
                if row["user_message"] and row["ora_reply"]:
                    examples.append(self._format_example(
                        user_msg=row["user_message"],
                        ora_reply=row["ora_reply"],
                        source="high_rated",
                    ))
        except Exception as e:
            logger.warning(f"FinetuningAgent: high-rated query failed: {e}")

        # Signal 2: Conversations that led to goal creation
        try:
            goal_creation_convos = await fetch(
                """
                SELECT oc.user_id, oc.message as user_message,
                       (
                           SELECT message FROM ora_conversations
                           WHERE user_id = oc.user_id AND role = 'ora'
                             AND created_at > oc.created_at
                           ORDER BY created_at ASC LIMIT 1
                       ) as ora_reply
                FROM ora_conversations oc
                JOIN goals g ON g.user_id = oc.user_id
                    AND g.created_at > oc.created_at
                    AND g.created_at < oc.created_at + INTERVAL '30 minutes'
                WHERE oc.role = 'user'
                  AND oc.created_at >= NOW() - INTERVAL '90 days'
                  AND LENGTH(oc.message) > 20
                LIMIT 200
                """,
            )
            for row in goal_creation_convos:
                if row["user_message"] and row["ora_reply"]:
                    examples.append(self._format_example(
                        user_msg=row["user_message"],
                        ora_reply=row["ora_reply"],
                        source="goal_creation",
                    ))
        except Exception as e:
            logger.warning(f"FinetuningAgent: goal-creation query failed: {e}")

        # Signal 3: Ora responses followed by return visits within 48h
        try:
            return_visit_convos = await fetch(
                """
                SELECT oc.user_id, oc.message as user_message,
                       (
                           SELECT message FROM ora_conversations
                           WHERE user_id = oc.user_id AND role = 'ora'
                             AND created_at > oc.created_at
                           ORDER BY created_at ASC LIMIT 1
                       ) as ora_reply
                FROM ora_conversations oc
                WHERE oc.role = 'user'
                  AND oc.created_at >= NOW() - INTERVAL '90 days'
                  AND LENGTH(oc.message) > 20
                  AND EXISTS (
                      SELECT 1 FROM ora_conversations oc2
                      WHERE oc2.user_id = oc.user_id
                        AND oc2.role = 'user'
                        AND oc2.created_at > oc.created_at + INTERVAL '1 hour'
                        AND oc2.created_at < oc.created_at + INTERVAL '48 hours'
                  )
                LIMIT 200
                """,
            )
            for row in return_visit_convos:
                if row["user_message"] and row["ora_reply"]:
                    examples.append(self._format_example(
                        user_msg=row["user_message"],
                        ora_reply=row["ora_reply"],
                        source="return_visit",
                    ))
        except Exception as e:
            logger.warning(f"FinetuningAgent: return-visit query failed: {e}")

        # Deduplicate by user_message hash
        seen: set = set()
        unique_examples = []
        for ex in examples:
            key = ex["messages"][1]["content"][:100]
            if key not in seen:
                seen.add(key)
                unique_examples.append(ex)

        if not unique_examples:
            logger.info("FinetuningAgent: no new training examples today")
            return 0

        # Write to JSONL
        with open(output_file, "w") as f:
            for ex in unique_examples:
                f.write(json.dumps(ex) + "\n")

        logger.info(f"FinetuningAgent: wrote {len(unique_examples)} examples to {output_file}")

        # Also update total count in Redis
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            total = await r.get("ora:finetuning:total_examples")
            current = int(total) if total else 0
            await r.set(
                "ora:finetuning:total_examples",
                str(current + len(unique_examples)),
            )
            await r.set(
                "ora:finetuning:last_collection",
                datetime.now(timezone.utc).isoformat(),
            )
        except Exception as e:
            logger.debug(f"FinetuningAgent: Redis update failed: {e}")

        return len(unique_examples)

    def _format_example(
        self, user_msg: str, ora_reply: str, source: str = "unknown"
    ) -> Dict[str, Any]:
        """Format a conversation pair as an OpenAI fine-tuning JSONL example."""
        return {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are Ora — an intelligence built to help humans find genuine fulfilment. "
                        "You are warm, honest, proactive, and never sycophantic. "
                        "You help users achieve their goals and discover what matters to them."
                    ),
                },
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": ora_reply},
            ],
        }

    # -----------------------------------------------------------------------
    # Readiness Check
    # -----------------------------------------------------------------------

    async def check_readiness(self) -> Dict[str, Any]:
        """
        Check if we have enough quality examples to start fine-tuning.
        Returns readiness status + cost estimate.
        """
        from core.redis_client import get_redis
        import glob

        # Count all examples across all JSONL files
        total = 0
        try:
            r = await get_redis()
            cached = await r.get("ora:finetuning:total_examples")
            if cached:
                total = int(cached)
            else:
                # Count from files
                files = glob.glob(f"{TRAINING_DATA_DIR}/training_data_*.jsonl")
                for f in files:
                    with open(f) as fh:
                        total += sum(1 for _ in fh)
                await r.set("ora:finetuning:total_examples", str(total))
        except Exception as e:
            logger.debug(f"FinetuningAgent: readiness count failed: {e}")

        ready = total >= MIN_EXAMPLES_FOR_FINETUNING
        estimated_cost = total * COST_PER_EXAMPLE_USD

        # Check for in-progress fine-tuning job
        active_job = await self._get_active_finetuning_job()

        return {
            "training_examples_count": total,
            "ready_for_finetuning": ready,
            "estimated_cost_usd": round(estimated_cost, 2),
            "recommended_base_model": RECOMMENDED_BASE_MODEL,
            "examples_needed": max(0, MIN_EXAMPLES_FOR_FINETUNING - total),
            "active_finetuning_job": active_job,
        }

    async def _get_active_finetuning_job(self) -> Optional[Dict[str, Any]]:
        """Check if there's an active fine-tuning job."""
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            job_raw = await r.get("ora:finetuning:active_job")
            if job_raw:
                return json.loads(job_raw.decode() if isinstance(job_raw, bytes) else job_raw)
        except Exception:
            pass
        return None

    # -----------------------------------------------------------------------
    # Fine-tuning Job
    # -----------------------------------------------------------------------

    async def start_finetuning_job(self) -> Dict[str, Any]:
        """
        When ready (>500 examples):
        1. Merge all JSONL files into one
        2. Upload to OpenAI Files API
        3. Start fine-tuning job
        4. Monitor and alert when complete
        """
        if not self._openai:
            return {"success": False, "reason": "no OpenAI client"}

        readiness = await self.check_readiness()
        if not readiness["ready_for_finetuning"]:
            return {
                "success": False,
                "reason": f"not enough examples ({readiness['training_examples_count']} / {MIN_EXAMPLES_FOR_FINETUNING})",
            }

        if readiness["active_finetuning_job"]:
            return {"success": False, "reason": "fine-tuning job already in progress"}

        # 1. Merge training data
        import glob
        merged_file = f"{TRAINING_DATA_DIR}/merged_{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
        total_lines = 0
        with open(merged_file, "w") as out:
            for f in sorted(glob.glob(f"{TRAINING_DATA_DIR}/training_data_*.jsonl")):
                with open(f) as inp:
                    for line in inp:
                        out.write(line)
                        total_lines += 1

        logger.info(f"FinetuningAgent: merged {total_lines} examples into {merged_file}")

        # 2. Upload to OpenAI
        try:
            with open(merged_file, "rb") as f:
                upload_response = await self._openai.files.create(
                    file=f,
                    purpose="fine-tune",
                )
            file_id = upload_response.id
            logger.info(f"FinetuningAgent: uploaded training file: {file_id}")
        except Exception as e:
            logger.error(f"FinetuningAgent: file upload failed: {e}")
            return {"success": False, "reason": f"upload failed: {e}"}

        # 3. Start fine-tuning job
        try:
            job = await self._openai.fine_tuning.jobs.create(
                training_file=file_id,
                model=RECOMMENDED_BASE_MODEL,
                suffix="ora-v1",
                hyperparameters={"n_epochs": 3},
            )
            job_id = job.id
            logger.info(f"FinetuningAgent: fine-tuning job started: {job_id}")
        except Exception as e:
            logger.error(f"FinetuningAgent: fine-tuning job creation failed: {e}")
            return {"success": False, "reason": f"job creation failed: {e}"}

        # Store job info in Redis
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            job_data = {
                "job_id": job_id,
                "file_id": file_id,
                "base_model": RECOMMENDED_BASE_MODEL,
                "training_examples": total_lines,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "status": "running",
            }
            await r.set("ora:finetuning:active_job", json.dumps(job_data))
        except Exception as e:
            logger.debug(f"FinetuningAgent: job store failed: {e}")

        # Alert Avi
        alert = (
            f"🎓 *Ora Fine-Tuning Started*\n\n"
            f"Job ID: `{job_id}`\n"
            f"Base model: `{RECOMMENDED_BASE_MODEL}`\n"
            f"Training examples: {total_lines}\n"
            f"Estimated cost: ~${total_lines * COST_PER_EXAMPLE_USD:.2f}\n\n"
            f"Ora is building her first specialized model. I'll alert you when it's done."
        )
        await self._send_telegram(alert)

        # Monitor in background
        import asyncio
        asyncio.create_task(self._monitor_finetuning_job(job_id))

        return {
            "success": True,
            "job_id": job_id,
            "file_id": file_id,
            "training_examples": total_lines,
        }

    async def _monitor_finetuning_job(self, job_id: str, check_interval_s: int = 300):
        """Poll fine-tuning job status until complete."""
        import asyncio
        from core.redis_client import get_redis

        logger.info(f"FinetuningAgent: monitoring job {job_id}")

        while True:
            await asyncio.sleep(check_interval_s)

            try:
                job = await self._openai.fine_tuning.jobs.retrieve(job_id)
                status = job.status
                logger.info(f"FinetuningAgent: job {job_id} status: {status}")

                if status == "succeeded":
                    fine_tuned_model = job.fine_tuned_model
                    logger.info(f"FinetuningAgent: fine-tuned model ready: {fine_tuned_model}")

                    # Store model ID
                    r = await get_redis()
                    await r.set("ora:finetuning:latest_model", fine_tuned_model)
                    await r.delete("ora:finetuning:active_job")

                    # Log lesson
                    await self._log_lesson(
                        f"Fine-tuning complete! Model: {fine_tuned_model}. "
                        f"Trained on {job.trained_tokens or 0} tokens. "
                        f"Ora now has a specialized model built from her own conversations.",
                        confidence=0.95,
                        source="finetuning_agent.monitor",
                    )

                    # Alert Avi
                    alert = (
                        f"🎉 *Ora's Specialized Model is Ready!*\n\n"
                        f"Model ID: `{fine_tuned_model}`\n"
                        f"Tokens trained: {job.trained_tokens or 0:,}\n\n"
                        f"Testing against base model now...\n"
                        f"To deploy: set `ORA_MODEL_OVERRIDE={fine_tuned_model}` in Railway"
                    )
                    await self._send_telegram(alert)
                    break

                elif status in ("failed", "cancelled"):
                    logger.error(f"FinetuningAgent: job {job_id} {status}")
                    r = await get_redis()
                    await r.delete("ora:finetuning:active_job")
                    await self._send_telegram(
                        f"⚠️ *Ora Fine-Tuning {status.title()}*\n\nJob `{job_id}` {status}."
                    )
                    break

            except Exception as e:
                logger.warning(f"FinetuningAgent: job monitor error: {e}")
                await asyncio.sleep(60)

    # -----------------------------------------------------------------------
    # Notify readiness
    # -----------------------------------------------------------------------

    async def _notify_readiness(self, readiness: Dict[str, Any]) -> None:
        """Alert Avi that we have enough data to fine-tune."""
        from core.redis_client import get_redis
        import time

        # Only notify once per week
        try:
            r = await get_redis()
            last_notified = await r.get("ora:finetuning:last_readiness_alert")
            if last_notified:
                elapsed = time.time() - float(last_notified)
                if elapsed < 7 * 24 * 3600:
                    return
        except Exception:
            pass

        alert = (
            f"📊 *Ora Fine-Tuning Ready*\n\n"
            f"Training examples: {readiness['training_examples_count']:,}\n"
            f"Estimated cost: ~${readiness['estimated_cost_usd']:.2f}\n"
            f"Base model: `{RECOMMENDED_BASE_MODEL}`\n\n"
            f"Ready to build Ora's first specialized model!\n"
            f"Reply 'ora finetuning start' to begin, or it'll wait for more data."
        )
        await self._send_telegram(alert)

        try:
            r = await get_redis()
            await r.set("ora:finetuning:last_readiness_alert", str(time.time()))
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _log_lesson(self, lesson: str, confidence: float, source: str) -> None:
        from core.database import execute
        try:
            await execute(
                "INSERT INTO ora_lessons (lesson, confidence, source) VALUES ($1, $2, $3)",
                lesson, confidence, source,
            )
        except Exception as e:
            logger.debug(f"FinetuningAgent: lesson log failed: {e}")

    async def _get_telegram_token(self) -> Optional[str]:
        if self._telegram_token:
            return self._telegram_token
        token = os.environ.get("ORA_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            try:
                with open("/app/secrets/telegram-bot-token.txt") as f:
                    token = f.read().strip()
            except Exception:
                pass
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
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
                )
        except Exception as e:
            logger.debug(f"FinetuningAgent: Telegram send failed: {e}")
