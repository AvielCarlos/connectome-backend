"""
PerformanceAgent — Hourly response time monitoring.

Reports to: CTO Agent
Schedule: every 1h (lightweight), weekly Wednesday summary
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

import httpx

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)
METRICS_FILE = "/Users/avielcarlos/.openclaw/workspace/tmp/perf/metrics.json"
P95_THRESHOLD_MS = 5000


class PerformanceAgent(BaseWorkerAgent):
    name = "performance_agent"
    role = "Performance Monitor"
    reports_to = "CTO"

    async def run(self) -> None:
        logger.info("PerformanceAgent: timing /api/screens/next")
        os.makedirs(os.path.dirname(METRICS_FILE), exist_ok=True)
        now = datetime.now(timezone.utc)
        token = await self._get_jwt()
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        # Take 3 samples for better accuracy
        samples = []
        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(3):
                t0 = time.perf_counter()
                try:
                    resp = await client.post(
                        "https://connectome-api-production.up.railway.app/api/screens/next",
                        headers=headers,
                    )
                    elapsed_ms = round((time.perf_counter() - t0) * 1000)
                    samples.append({"elapsed_ms": elapsed_ms, "status": resp.status_code})
                except Exception as e:
                    elapsed_ms = round((time.perf_counter() - t0) * 1000)
                    samples.append({"elapsed_ms": elapsed_ms, "status": 0, "error": str(e)})
                await asyncio.sleep(1)

        valid = [s["elapsed_ms"] for s in samples if s.get("status") in (200, 201)]
        p50 = sorted(valid)[len(valid)//2] if valid else 0
        p95 = sorted(valid)[int(len(valid)*0.95)] if valid else sorted([s["elapsed_ms"] for s in samples])[int(len(samples)*0.95)]

        # Append to metrics file
        metrics = self._load_json(METRICS_FILE, default={"samples": []})
        metrics["samples"].append({
            "ts": now.isoformat(),
            "p50_ms": p50,
            "p95_ms": p95,
            "raw": samples,
        })
        # Keep last 7 days of hourly data = ~168 entries
        metrics["samples"] = metrics["samples"][-200:]
        self._save_json(METRICS_FILE, metrics)

        logger.info(f"PerformanceAgent: p50={p50}ms p95={p95}ms")

        # Alert if degraded
        if p95 > P95_THRESHOLD_MS:
            await self.escalate(
                f"⚠️ Performance degraded!\n"
                f"p95 latency: {p95}ms (threshold: {P95_THRESHOLD_MS}ms)\n"
                f"p50 latency: {p50}ms\n"
                f"Endpoint: /api/screens/next\n"
                f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}"
            )
            await self.teach_ora(
                f"Performance degraded: /api/screens/next p95={p95}ms at {now.strftime('%Y-%m-%d %H:%M')}. "
                f"This impacts user experience — slow screens reduce engagement by ~30%.",
                confidence=0.9,
            )

        # Weekly report check (Wednesday)
        if now.weekday() == 2 and now.hour == 8:
            await self._weekly_report(metrics)

    async def _weekly_report(self, metrics: dict) -> None:
        samples = metrics.get("samples", [])
        if not samples:
            return
        all_p95 = [s["p95_ms"] for s in samples if s.get("p95_ms")]
        if not all_p95:
            return
        avg_p95 = round(sum(all_p95) / len(all_p95))
        max_p95 = max(all_p95)
        min_p95 = min(all_p95)
        await self.teach_ora(
            f"Weekly performance summary: avg p95={avg_p95}ms, max={max_p95}ms, min={min_p95}ms "
            f"over {len(all_p95)} hourly checks. API performance is "
            f"{'healthy' if avg_p95 < P95_THRESHOLD_MS else 'degraded'}.",
            confidence=0.85,
        )

    async def report(self) -> str:
        metrics = self._load_json(METRICS_FILE, {})
        last = (metrics.get("samples") or [{}])[-1]
        return f"PerformanceAgent: last p50={last.get('p50_ms','?')}ms p95={last.get('p95_ms','?')}ms"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(PerformanceAgent().run())
