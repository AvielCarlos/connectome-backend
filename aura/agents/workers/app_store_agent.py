"""
AppStoreAgent — Tracks APK download velocity and review strategy.

Reports to: CMO Agent
Schedule: weekly Monday 9am Pacific
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)
METRICS_FILE = os.path.join(os.getenv("CONNECTOME_RUNTIME_DIR", "/tmp/connectome"), "biz_dev", "app_metrics.json")
REPO = "AvielCarlos/connectome-backend"


class AppStoreAgent(BaseWorkerAgent):
    name = "app_store_agent"
    role = "App Store Manager"
    reports_to = "CMO"

    async def run(self) -> None:
        logger.info("AppStoreAgent: checking APK download metrics")
        os.makedirs(os.path.dirname(METRICS_FILE), exist_ok=True)
        week = datetime.now(timezone.utc).strftime("%Y-W%W")

        # 1. Get GitHub release download count
        release_data = self._sh(
            f'gh release view v0.1.0 --repo {REPO} --json assets,tagName,publishedAt 2>/dev/null'
        )

        total_downloads = 0
        assets = []
        try:
            import json as _json
            parsed = _json.loads(release_data)
            assets = parsed.get("assets", [])
            total_downloads = sum(a.get("downloadCount", 0) for a in assets)
        except Exception:
            pass

        # Also check latest release if v0.1.0 not found
        if total_downloads == 0:
            latest_data = self._sh(f'gh release list --repo {REPO} --limit 3 2>/dev/null')
            if latest_data and "error" not in latest_data.lower():
                logger.info(f"AppStoreAgent: releases found: {latest_data[:200]}")

        # 2. Load history and compute velocity
        metrics = self._load_json(METRICS_FILE, default={"history": []})
        prev_entry = metrics["history"][-1] if metrics["history"] else {}
        prev_downloads = prev_entry.get("total_downloads", 0)
        velocity = total_downloads - prev_downloads

        metrics["history"].append({
            "week": week,
            "total_downloads": total_downloads,
            "weekly_new": velocity,
            "assets": [{"name": a.get("name"), "count": a.get("downloadCount", 0)} for a in assets],
        })
        metrics["history"] = metrics["history"][-52:]  # keep 1 year
        self._save_json(METRICS_FILE, metrics)

        # 3. Generate review request strategy
        # Target users who have been active for 7+ days (most likely to leave positive reviews)
        strategy = {
            "target_cohort": "users active for 7+ days with avg rating > 3.5",
            "trigger": "after completing 5th goal or habit streak",
            "message": "You've been crushing it! Would you mind leaving us a quick review? It helps us help more people 🙏",
            "timing": "in-app, 24h after 5th completion",
        }

        # 4. Teach Aura
        await self.teach_aura(
            f"App metrics ({week}): {total_downloads} total APK downloads, +{velocity} this week. "
            f"Review strategy: target 7-day active users post 5th habit completion. "
            f"Download velocity {'is growing' if velocity > 0 else 'is flat — consider more promotion'}.",
            confidence=0.8,
        )

        logger.info(f"AppStoreAgent: done. {total_downloads} total downloads, +{velocity} this week.")

    async def report(self) -> str:
        data = self._load_json(METRICS_FILE, {})
        last = (data.get("history") or [{}])[-1]
        return f"AppStoreAgent: {last.get('total_downloads','?')} downloads (+{last.get('weekly_new','?')} this week)"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(AppStoreAgent().run())
