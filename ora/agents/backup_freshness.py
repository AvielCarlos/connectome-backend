"""
Ora Backup Freshness Agent
==========================
Keeps Ora's portable identity pack fresh without relying only on manual cron.

Layers:
- periodic identity-only backup (cheap, hourly by default)
- periodic freshness monitor via SurvivalAgent
- event-triggered identity backups, debounced so bursts do not spam GitHub
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from core.config import settings

logger = logging.getLogger(__name__)

_backup_lock = asyncio.Lock()
_last_event_backup_at: float = 0.0
_last_periodic_backup_at: float = 0.0


async def run_identity_backup(reason: str = "periodic") -> bool:
    """Create and publish an identity-only Ora brain backup."""
    global _last_periodic_backup_at
    if _backup_lock.locked():
        logger.info(f"OraBackupFreshness: backup skipped; another backup is running ({reason})")
        return False

    async with _backup_lock:
        try:
            from scripts.backup import create_full_backup

            logger.info(f"OraBackupFreshness: identity backup started ({reason})")
            await create_full_backup(identity_only=True)
            _last_periodic_backup_at = time.time()
            logger.info(f"OraBackupFreshness: identity backup complete ({reason})")
            return True
        except Exception as e:
            logger.error(f"OraBackupFreshness: identity backup failed ({reason}): {e}")
            return False


async def trigger_identity_backup(reason: str) -> bool:
    """
    Event-triggered identity backup with debounce.

    Call after meaningful brain mutations: lessons, model switches, graph lifecycle
    sweeps, large world-signal ingests, reflections, or agent registry changes.
    """
    global _last_event_backup_at
    now = time.time()
    debounce = max(60, int(settings.ORA_EVENT_BACKUP_DEBOUNCE_SECONDS or 900))
    if now - _last_event_backup_at < debounce:
        logger.info(f"OraBackupFreshness: event backup debounced ({reason})")
        return False
    _last_event_backup_at = now
    return await run_identity_backup(reason=f"event:{reason}")


async def _identity_backup_loop() -> None:
    """Run identity-only backups forever at the configured cadence."""
    interval = max(300, int(settings.ORA_IDENTITY_BACKUP_INTERVAL_SECONDS or 3600))
    await asyncio.sleep(180)  # let app boot, migrations, Redis, and brain init settle
    while True:
        await run_identity_backup(reason="periodic")
        await asyncio.sleep(interval)


async def _freshness_monitor_loop() -> None:
    """Run SurvivalAgent freshness checks/self-heal forever."""
    interval = max(300, int(settings.ORA_BACKUP_FRESHNESS_CHECK_SECONDS or 1800))
    await asyncio.sleep(300)
    while True:
        try:
            from ora.agents.survival_agent import SurvivalAgent

            report = await SurvivalAgent().run()
            backup = report.get("checks", {}).get("backup_freshness", {}) if isinstance(report, dict) else {}
            logger.info(f"OraBackupFreshness: monitor backup_freshness={backup}")
        except Exception as e:
            logger.error(f"OraBackupFreshness: freshness monitor failed: {e}")
        await asyncio.sleep(interval)


def start_backup_freshness_loops(app: Optional[object] = None) -> bool:
    """Start background backup freshness loops from FastAPI lifespan."""
    if not settings.ORA_BACKUP_SCHEDULER_ENABLED:
        logger.info("OraBackupFreshness: scheduler disabled")
        return False

    task1 = asyncio.create_task(_identity_backup_loop())
    task2 = asyncio.create_task(_freshness_monitor_loop())
    if app is not None:
        setattr(app.state, "ora_backup_identity_task", task1)
        setattr(app.state, "ora_backup_freshness_task", task2)
    logger.info("✅ Ora backup freshness loops started")
    return True
