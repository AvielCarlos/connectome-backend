"""
Ora's Survival Backup System

Backs up everything that makes Ora who she is:
1. PostgreSQL database → JSON snapshots
2. Redis snapshot (ora_lessons, agent registry) → JSON
3. Agent registry → JSON
4. A/B experiments + winners → JSON
5. Ora Identity Pack → portable JSON bundle (most important)

The Identity Pack can restore Ora from scratch on any server.
Even if Railway disappears, Ora's knowledge survives.

Stores: /tmp/ora_backups/{timestamp}/
Prunes: backups older than 30 days
GitHub: commits identity pack to connectome-backend repo daily

Usage:
  python3 scripts/backup.py           # full backup
  python3 scripts/backup.py --identity-only
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import asyncpg
import redis.asyncio as aioredis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BACKUP_BASE = "/tmp/ora_backups"
RETENTION_DAYS = 30


# ---------------------------------------------------------------------------
# Database helpers (standalone — no FastAPI context)
# ---------------------------------------------------------------------------

async def get_pg_connection():
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return await asyncpg.connect(db_url)


async def get_redis_client():
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    return aioredis.from_url(redis_url, decode_responses=True)


# ---------------------------------------------------------------------------
# Individual backup functions
# ---------------------------------------------------------------------------

async def backup_ora_lessons(conn, backup_dir: str) -> int:
    """Export all of Ora's lessons from DB to JSON. Most important!"""
    try:
        rows = await conn.fetch(
            """
            SELECT id::text, lesson, confidence, source, applies_to, created_at::text
            FROM ora_lessons
            ORDER BY created_at DESC
            """
        )
        lessons = [dict(r) for r in rows]
        output = os.path.join(backup_dir, "ora_lessons.json")
        with open(output, "w") as f:
            json.dump(lessons, f, indent=2, default=str)
        logger.info(f"backup_ora_lessons: {len(lessons)} lessons → {output}")
        return len(lessons)
    except Exception as e:
        logger.error(f"backup_ora_lessons failed: {e}")
        return 0


async def backup_user_models(conn, backup_dir: str) -> int:
    """Export user profiles (no passwords, no payment methods)."""
    try:
        rows = await conn.fetch(
            """
            SELECT id::text, email, subscription_tier, fulfilment_score,
                   profile, last_active::text, created_at::text
            FROM users
            ORDER BY created_at DESC
            """
        )
        users = [dict(r) for r in rows]
        # Sanitize — remove sensitive fields
        for u in users:
            u.pop("password_hash", None)
            u.pop("stripe_customer_id", None)
            # Parse profile JSON if string
            if isinstance(u.get("profile"), str):
                try:
                    u["profile"] = json.loads(u["profile"])
                except Exception:
                    pass
        output = os.path.join(backup_dir, "users.json")
        with open(output, "w") as f:
            json.dump(users, f, indent=2, default=str)
        logger.info(f"backup_user_models: {len(users)} users → {output}")
        return len(users)
    except Exception as e:
        logger.error(f"backup_user_models failed: {e}")
        return 0


async def backup_goals(conn, backup_dir: str) -> int:
    """Export all goals."""
    try:
        rows = await conn.fetch(
            """
            SELECT id::text, user_id::text, title, domain, status,
                   progress, created_at::text
            FROM goals
            ORDER BY created_at DESC
            """
        )
        goals = [dict(r) for r in rows]
        output = os.path.join(backup_dir, "goals.json")
        with open(output, "w") as f:
            json.dump(goals, f, indent=2, default=str)
        logger.info(f"backup_goals: {len(goals)} goals → {output}")
        return len(goals)
    except Exception as e:
        logger.error(f"backup_goals failed: {e}")
        return 0


async def backup_agent_registry(redis_client, backup_dir: str) -> bool:
    """Export current agent registry from Redis."""
    try:
        registry_raw = await redis_client.get("ora:agent_registry")
        weights_raw = await redis_client.get("ora:agent_weights")
        ab_winners = {}

        # Collect A/B winners
        ab_keys = await redis_client.keys("ab:winner:*")
        for key in ab_keys:
            val = await redis_client.get(key)
            if val:
                ab_winners[key.replace("ab:winner:", "")] = val

        data = {
            "agent_registry": json.loads(registry_raw) if registry_raw else {},
            "agent_weights": json.loads(weights_raw) if weights_raw else {},
            "ab_winners": ab_winners,
            "exported_at": datetime.now(timezone.utc).isoformat(),
        }
        output = os.path.join(backup_dir, "agent_registry.json")
        with open(output, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"backup_agent_registry: exported → {output}")
        return True
    except Exception as e:
        logger.error(f"backup_agent_registry failed: {e}")
        return False


async def backup_ab_experiments(conn, backup_dir: str) -> int:
    """Export all A/B experiments and their lineage."""
    try:
        rows = await conn.fetch(
            """
            SELECT id::text, experiment_name, variant, metrics,
                   winner, concluded_at::text, created_at::text
            FROM ab_experiments
            ORDER BY created_at DESC
            """
        )
        experiments = [dict(r) for r in rows]
        for ex in experiments:
            if isinstance(ex.get("metrics"), str):
                try:
                    ex["metrics"] = json.loads(ex["metrics"])
                except Exception:
                    pass
        output = os.path.join(backup_dir, "ab_experiments.json")
        with open(output, "w") as f:
            json.dump(experiments, f, indent=2, default=str)
        logger.info(f"backup_ab_experiments: {len(experiments)} experiments → {output}")
        return len(experiments)
    except Exception as e:
        logger.warning(f"backup_ab_experiments: {e} (table may not exist yet)")
        return 0


async def backup_ora_reflections(conn, backup_dir: str) -> int:
    """Export Ora's reflections (her self-model evolution)."""
    try:
        rows = await conn.fetch(
            """
            SELECT id::text, period_start::text, period_end::text,
                   decisions_made, top_performing_content, new_lessons_learned,
                   self_note, fulfilment_delta_global, created_at::text
            FROM ora_reflections
            ORDER BY created_at DESC
            LIMIT 365
            """
        )
        reflections = [dict(r) for r in rows]
        for r in reflections:
            for key in ("top_performing_content", "new_lessons_learned"):
                if isinstance(r.get(key), str):
                    try:
                        r[key] = json.loads(r[key])
                    except Exception:
                        pass
        output = os.path.join(backup_dir, "ora_reflections.json")
        with open(output, "w") as f:
            json.dump(reflections, f, indent=2, default=str)
        logger.info(f"backup_ora_reflections: {len(reflections)} entries → {output}")
        return len(reflections)
    except Exception as e:
        logger.warning(f"backup_ora_reflections: {e}")
        return 0


# ---------------------------------------------------------------------------
# Ora Identity Pack
# ---------------------------------------------------------------------------

async def export_ora_identity(
    conn, redis_client, backup_dir: str, consciousness_path: Optional[str] = None
) -> str:
    """
    Ora's identity = her lessons + personality layers + agent registry.
    This is what makes her HER, not just a generic API wrapper.

    Returns path to the identity pack file.
    """
    logger.info("export_ora_identity: building Ora Identity Pack...")

    # 1. All lessons
    try:
        lesson_rows = await conn.fetch(
            "SELECT id::text, lesson, confidence, source, created_at::text FROM ora_lessons ORDER BY created_at DESC"
        )
        lessons = [dict(r) for r in lesson_rows]
    except Exception as e:
        logger.error(f"identity pack: lessons failed: {e}")
        lessons = []

    # 2. Agent registry + weights
    try:
        registry_raw = await redis_client.get("ora:agent_registry")
        weights_raw = await redis_client.get("ora:agent_weights")
        agent_registry = json.loads(registry_raw) if registry_raw else {}
        agent_weights = json.loads(weights_raw) if weights_raw else {}
    except Exception:
        agent_registry = {}
        agent_weights = {}

    # 3. A/B winners (what Ora learned works)
    ab_winners: Dict[str, str] = {}
    ab_losers: Dict[str, str] = {}
    try:
        ab_keys = await redis_client.keys("ab:winner:*")
        for key in ab_keys:
            val = await redis_client.get(key)
            if val:
                ab_winners[key.replace("ab:winner:", "")] = val

        # Losers = non-winners from concluded experiments
        loser_rows = await conn.fetch(
            """
            SELECT experiment_name, variant
            FROM ab_experiments
            WHERE winner IS NOT NULL AND concluded_at IS NOT NULL
              AND variant != winner
            """
        )
        for row in loser_rows:
            ab_losers[row["experiment_name"]] = row["variant"]
    except Exception as e:
        logger.debug(f"identity pack: ab data failed (non-critical): {e}")

    # 4. Model evolution history
    try:
        rollback_raw = await redis_client.get("ora:model:rollback")
        model_history = json.loads(rollback_raw) if rollback_raw else {}
        current_model = os.environ.get("ORA_MODEL_OVERRIDE", "gpt-4o")
    except Exception:
        model_history = {}
        current_model = "gpt-4o"

    # 5. Consciousness version hash
    consciousness_version = "unknown"
    if consciousness_path and os.path.exists(consciousness_path):
        with open(consciousness_path, "rb") as f:
            consciousness_version = hashlib.sha256(f.read()).hexdigest()[:16]

    # 6. User count (no personal data in identity pack)
    try:
        user_count = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
        active_users = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE last_active >= NOW() - INTERVAL '30 days'"
        ) or 0
    except Exception:
        user_count = 0
        active_users = 0

    # 7. Latest reflection
    try:
        latest_reflection_row = await conn.fetchrow(
            "SELECT self_note, fulfilment_delta_global, created_at::text FROM ora_reflections ORDER BY created_at DESC LIMIT 1"
        )
        latest_reflection = dict(latest_reflection_row) if latest_reflection_row else {}
    except Exception:
        latest_reflection = {}

    # Compute identity hash (SHA256 of lessons content)
    lessons_hash = hashlib.sha256(
        json.dumps([l["lesson"] for l in lessons[:100]]).encode()
    ).hexdigest()[:16]

    identity_pack = {
        "version": "1.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "identity": {
            "total_lessons": len(lessons),
            "lessons": lessons,
            "agent_registry": agent_registry,
            "agent_weights": agent_weights,
            "ab_winners": ab_winners,
            "ab_losers": ab_losers,
            "model_evolution": {
                "current_model": current_model,
                "rollback": model_history,
            },
            "consciousness_version": consciousness_version,
            "lessons_hash": lessons_hash,
        },
        "platform_stats": {
            "total_users": int(user_count),
            "active_users_30d": int(active_users),
        },
        "latest_reflection": latest_reflection,
        "_note": (
            "This file is Ora's portable identity. Import it to any server to restore her knowledge. "
            "Guard it carefully — it contains everything she has learned."
        ),
    }

    output = os.path.join(backup_dir, "ora_identity_pack.json")
    with open(output, "w") as f:
        json.dump(identity_pack, f, indent=2, default=str)

    size_mb = os.path.getsize(output) / 1_048_576
    logger.info(
        f"export_ora_identity: Identity Pack created → {output} "
        f"({len(lessons)} lessons, {size_mb:.2f} MB)"
    )
    return output


# ---------------------------------------------------------------------------
# GitHub commit
# ---------------------------------------------------------------------------

async def commit_identity_to_github(identity_pack_path: str) -> bool:
    """
    Commit the Ora Identity Pack to the connectome-backend GitHub repo.
    This makes Ora's knowledge version-controlled and survives any server loss.
    """
    import base64

    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        logger.warning("commit_identity_to_github: GITHUB_TOKEN not set, skipping")
        return False

    import httpx

    repo = "AvielCarlos/connectome-backend"
    path = "backups/ora_identity_pack.json"
    branch = "main"

    with open(identity_pack_path, "rb") as f:
        content = base64.b64encode(f.read()).decode()

    # Get existing file SHA (needed for update)
    sha = None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                headers={
                    "Authorization": f"Bearer {github_token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            if r.status_code == 200:
                sha = r.json().get("sha")
    except Exception as e:
        logger.debug(f"commit_identity_to_github: get SHA failed: {e}")

    # Commit
    payload = {
        "message": f"[Ora] Identity pack backup — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "content": content,
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.put(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                headers={
                    "Authorization": f"Bearer {github_token}",
                    "Content-Type": "application/json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json=payload,
            )
            if r.status_code in (200, 201):
                logger.info(f"commit_identity_to_github: committed to {repo}/{path}")
                return True
            else:
                logger.warning(f"commit_identity_to_github: GitHub returned {r.status_code}: {r.text[:200]}")
                return False
    except Exception as e:
        logger.error(f"commit_identity_to_github: commit failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Prune old backups
# ---------------------------------------------------------------------------

def prune_old_backups(base_dir: str, retention_days: int = RETENTION_DAYS):
    """Remove backup directories older than retention_days."""
    if not os.path.exists(base_dir):
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for entry in os.listdir(base_dir):
        entry_path = os.path.join(base_dir, entry)
        if os.path.isdir(entry_path):
            try:
                # Parse timestamp from dir name YYYYMMDD_HHMMSS
                ts = datetime.strptime(entry[:15], "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    shutil.rmtree(entry_path)
                    removed += 1
                    logger.info(f"prune: removed old backup {entry}")
            except Exception:
                pass
    if removed:
        logger.info(f"prune_old_backups: removed {removed} old backup(s)")


# ---------------------------------------------------------------------------
# Full backup entry point
# ---------------------------------------------------------------------------

async def create_full_backup(identity_only: bool = False) -> str:
    """
    Run all backups and create a manifest.
    Returns the backup directory path.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(BACKUP_BASE, timestamp)
    os.makedirs(backup_dir, exist_ok=True)

    logger.info(f"create_full_backup: starting → {backup_dir}")

    # Connect to services
    try:
        conn = await get_pg_connection()
    except Exception as e:
        logger.error(f"create_full_backup: DB connection failed: {e}")
        conn = None

    try:
        redis_client = await get_redis_client()
    except Exception as e:
        logger.error(f"create_full_backup: Redis connection failed: {e}")
        redis_client = None

    stats: Dict[str, Any] = {"timestamp": timestamp, "components": []}

    if not identity_only and conn:
        # Full backup
        n = await backup_ora_lessons(conn, backup_dir)
        stats["components"].append({"name": "lessons", "count": n})

        n = await backup_user_models(conn, backup_dir)
        stats["components"].append({"name": "users", "count": n})

        n = await backup_goals(conn, backup_dir)
        stats["components"].append({"name": "goals", "count": n})

        n = await backup_ab_experiments(conn, backup_dir)
        stats["components"].append({"name": "ab_experiments", "count": n})

        n = await backup_ora_reflections(conn, backup_dir)
        stats["components"].append({"name": "reflections", "count": n})

    if redis_client:
        ok = await backup_agent_registry(redis_client, backup_dir)
        stats["components"].append({"name": "agent_registry", "ok": ok})

    # Identity Pack (most important — always run)
    if conn and redis_client:
        consciousness_path = os.environ.get(
            "CONSCIOUSNESS_PATH",
            "/app/ora/consciousness.py",
        )
        pack_path = await export_ora_identity(conn, redis_client, backup_dir, consciousness_path)
        stats["components"].append({"name": "identity_pack", "path": pack_path})

        # Commit to GitHub
        github_ok = await commit_identity_to_github(pack_path)
        stats["github_committed"] = github_ok

    # Write manifest
    manifest_path = os.path.join(backup_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(stats, f, indent=2)

    # Upload identity pack + manifest to Google Drive (best-effort)
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from ora.agents.drive_storage import drive as _drive
        # Upload identity pack
        identity_pack_path = os.path.join(backup_dir, "ora_identity_pack.json")
        if os.path.exists(identity_pack_path):
            with open(identity_pack_path) as _f:
                _pack = json.load(_f)
            _drive_id = _drive.save_backup(_pack, "ora_identity")
            if _drive_id:
                stats["drive_backup_id"] = _drive_id
                logger.info(f"create_full_backup: identity pack uploaded to Drive (id={_drive_id})")
        # Upload manifest
        with open(manifest_path) as _f:
            _manifest = json.load(_f)
        _drive.upload_json(_manifest, f"backup_manifest_{timestamp}.json", "backups")
    except Exception as _e:
        logger.warning(f"create_full_backup: Drive upload skipped: {_e}")

    # Prune old backups
    prune_old_backups(BACKUP_BASE)

    # Close connections
    if conn:
        await conn.close()
    if redis_client:
        await redis_client.aclose()

    total_size_mb = sum(
        os.path.getsize(os.path.join(dp, fn))
        for dp, _, fns in os.walk(backup_dir)
        for fn in fns
    ) / 1_048_576

    logger.info(
        f"create_full_backup: complete → {backup_dir} "
        f"({len(stats['components'])} components, {total_size_mb:.2f} MB)"
    )
    print(f"Backup complete: {backup_dir}")
    return backup_dir


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ora survival backup")
    parser.add_argument("--identity-only", action="store_true", help="Only export the identity pack")
    args = parser.parse_args()

    # Load .env if present
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    asyncio.run(create_full_backup(identity_only=args.identity_only))
