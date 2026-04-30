"""
ABResultsAgent — Computes A/B test results and auto-applies winners.

Reports to: CPO Agent
Schedule: daily 4am Pacific
"""

import asyncio
import logging
import math
from datetime import datetime, timezone

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)


class ABResultsAgent(BaseWorkerAgent):
    name = "ab_results_agent"
    role = "A/B Test Analyst"
    reports_to = "CPO"

    EXPERIMENTS = ["home_page", "onboarding", "feed", "pricing", "cta_button"]

    async def run(self) -> None:
        logger.info("ABResultsAgent: checking A/B experiment results")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        results = []

        for experiment in self.EXPERIMENTS:
            data = await self._get(f"/api/ab/experiments/{experiment}") or {}
            if not data:
                # Try generic list endpoint
                continue

            winner = self._compute_winner(data)
            if winner:
                # Auto-apply winner
                applied = await self._put(f"/api/ab/winner", {
                    "experiment": experiment,
                    "variant": winner["variant"],
                    "confidence": winner["confidence"],
                })
                status = "applied" if applied else "failed to apply"
                results.append({
                    "experiment": experiment,
                    "winner": winner["variant"],
                    "confidence": winner["confidence"],
                    "status": status,
                })
                logger.info(f"ABResultsAgent: {experiment} winner={winner['variant']} ({status})")
            else:
                results.append({
                    "experiment": experiment,
                    "winner": None,
                    "status": "insufficient data",
                })

        # Also check generic endpoint
        all_experiments = await self._get("/api/ab/experiments") or {}
        experiments_list = all_experiments.get("experiments", []) if isinstance(all_experiments, dict) else []
        
        for exp in experiments_list:
            exp_id = exp.get("id") or exp.get("name")
            if exp_id and exp_id not in self.EXPERIMENTS:
                winner = self._compute_winner(exp)
                if winner:
                    await self._put("/api/ab/winner", {
                        "experiment": exp_id,
                        "variant": winner["variant"],
                        "confidence": winner["confidence"],
                    })
                    results.append({"experiment": exp_id, "winner": winner["variant"], "status": "applied"})

        # Teach Ora
        winners = [r for r in results if r.get("winner")]
        no_data = [r for r in results if r.get("status") == "insufficient data"]
        if winners:
            summary = "; ".join(f"{r['experiment']}→{r['winner']}" for r in winners)
            await self.teach_aura(
                f"A/B results ({today}): {len(winners)} winner(s) found and applied — {summary}. "
                f"{len(no_data)} experiments still gathering data.",
                confidence=0.9,
            )
        else:
            await self.teach_aura(
                f"A/B results ({today}): No clear winners yet. {len(results)} experiments running. "
                f"Need more traffic to reach statistical significance.",
                confidence=0.75,
            )

        logger.info(f"ABResultsAgent: done. {len(winners)} winners applied.")

    def _compute_winner(self, data: dict) -> dict | None:
        """Basic z-test for proportions. Returns winner if p < 0.05."""
        variants = data.get("variants", [])
        if len(variants) < 2:
            return None
        try:
            best = max(variants, key=lambda v: v.get("conversion_rate", 0))
            baseline = variants[0]
            if best == baseline:
                return None

            p1 = best.get("conversion_rate", 0)
            p2 = baseline.get("conversion_rate", 0)
            n1 = best.get("sample_size", 0)
            n2 = baseline.get("sample_size", 0)

            if n1 < 100 or n2 < 100:
                return None

            p_pool = (p1 * n1 + p2 * n2) / (n1 + n2)
            se = math.sqrt(p_pool * (1 - p_pool) * (1/n1 + 1/n2))
            if se == 0:
                return None
            z = abs(p1 - p2) / se
            # z > 1.96 → p < 0.05
            if z > 1.96:
                confidence = round(min(0.99, 0.5 + z * 0.02), 2)
                return {"variant": best.get("name", "B"), "confidence": confidence}
        except Exception:
            pass
        return None

    async def report(self) -> str:
        return "ABResultsAgent: A/B experiment analysis complete."


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(ABResultsAgent().run())
