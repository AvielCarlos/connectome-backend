"""
SurfaceRegistry — tracks all Ora-spawned web surfaces.

Dual-write: PostgreSQL (source of truth) + Redis (hot cache with 24h TTL).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from core.database import execute, fetch, fetchrow
from core.redis_client import get_redis

logger = logging.getLogger(__name__)

_REDIS_PREFIX = "ora:surfaces"
_REDIS_TTL    = 86_400  # 24 hours


class SurfaceRegistry:
    """Track and retrieve Ora-spawned web surfaces."""

    # ─── Register ────────────────────────────────────────────────────────────

    async def register(
        self,
        surface_id: str,
        user_id: str,
        spec: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Persist a new surface in DB + Redis.
        Returns the stored surface metadata dict.
        """
        title      = spec.get("title", "My Surface")
        slug       = spec.get("slug", f"/{surface_id}")
        inferred   = spec.get("inferred_type", "custom")
        github_web = f"src/surfaces/surface_{surface_id}.tsx"
        github_api = f"api/routes/surfaces/surface_{surface_id}.py"

        await execute(
            """
            INSERT INTO ora_surfaces
                (id, user_id, surface_type, title, slug, spec,
                 github_path, api_path, status)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, 'active')
            ON CONFLICT (id) DO UPDATE
                SET title        = EXCLUDED.title,
                    slug         = EXCLUDED.slug,
                    spec         = EXCLUDED.spec,
                    updated_at   = NOW(),
                    status       = 'active'
            """,
            surface_id,
            UUID(user_id),
            inferred,
            title,
            slug,
            json.dumps(spec),
            github_web,
            github_api,
        )

        surface = {
            "id":           surface_id,
            "user_id":      user_id,
            "surface_type": inferred,
            "title":        title,
            "slug":         slug,
            "spec":         spec,
            "github_path":  github_web,
            "api_path":     github_api,
            "status":       "active",
            "view_count":   0,
            "created_at":   datetime.now(timezone.utc).isoformat(),
        }

        await self._cache_set(surface_id, surface)
        logger.info(f"SurfaceRegistry: registered {surface_id} for user {user_id[:8]}")
        return surface

    # ─── Read ─────────────────────────────────────────────────────────────────

    async def get_surface(self, surface_id: str) -> Optional[Dict[str, Any]]:
        """Fetch surface metadata (tries Redis first, falls back to DB)."""
        cached = await self._cache_get(surface_id)
        if cached:
            return cached

        row = await fetchrow(
            "SELECT * FROM ora_surfaces WHERE id = $1",
            surface_id,
        )
        if not row:
            return None

        surface = self._row_to_dict(row)
        await self._cache_set(surface_id, surface)
        return surface

    async def get_user_surfaces(self, user_id: str) -> List[Dict[str, Any]]:
        """Return all active surfaces for a user, newest first."""
        rows = await fetch(
            """
            SELECT * FROM ora_surfaces
            WHERE user_id = $1 AND status = 'active'
            ORDER BY created_at DESC
            """,
            UUID(user_id),
        )
        return [self._row_to_dict(r) for r in rows]

    # ─── Update ───────────────────────────────────────────────────────────────

    async def update_spec(self, surface_id: str, spec: Dict[str, Any]) -> None:
        """Overwrite the spec of an existing surface (used by update_surface)."""
        title    = spec.get("title", "My Surface")
        inferred = spec.get("inferred_type", "custom")

        await execute(
            """
            UPDATE ora_surfaces
            SET spec         = $1::jsonb,
                title        = $2,
                surface_type = $3,
                updated_at   = NOW()
            WHERE id = $4
            """,
            json.dumps(spec),
            title,
            inferred,
            surface_id,
        )
        await self._cache_del(surface_id)

    async def increment_view_count(self, surface_id: str) -> None:
        """Increment view counter (best-effort)."""
        try:
            await execute(
                "UPDATE ora_surfaces SET view_count = view_count + 1 WHERE id = $1",
                surface_id,
            )
            await self._cache_del(surface_id)
        except Exception as e:
            logger.debug(f"SurfaceRegistry: view_count increment failed: {e}")

    # ─── Retire ───────────────────────────────────────────────────────────────

    async def retire(self, surface_id: str) -> None:
        """Mark a surface as retired (soft-delete)."""
        await execute(
            "UPDATE ora_surfaces SET status = 'retired', updated_at = NOW() WHERE id = $1",
            surface_id,
        )
        await self._cache_del(surface_id)
        logger.info(f"SurfaceRegistry: retired {surface_id}")

    # ─── Redis helpers ────────────────────────────────────────────────────────

    async def _cache_set(self, surface_id: str, data: Dict[str, Any]) -> None:
        try:
            r = await get_redis()
            await r.set(
                f"{_REDIS_PREFIX}:{surface_id}",
                json.dumps(data, default=str),
                ex=_REDIS_TTL,
            )
        except Exception as e:
            logger.debug(f"SurfaceRegistry: Redis set failed: {e}")

    async def _cache_get(self, surface_id: str) -> Optional[Dict[str, Any]]:
        try:
            r = await get_redis()
            val = await r.get(f"{_REDIS_PREFIX}:{surface_id}")
            if val:
                return json.loads(val)
        except Exception as e:
            logger.debug(f"SurfaceRegistry: Redis get failed: {e}")
        return None

    async def _cache_del(self, surface_id: str) -> None:
        try:
            r = await get_redis()
            await r.delete(f"{_REDIS_PREFIX}:{surface_id}")
        except Exception:
            pass

    # ─── Serialization ────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: Any) -> Dict[str, Any]:
        d = dict(row)
        # Deserialize JSONB spec field
        spec = d.get("spec")
        if isinstance(spec, str):
            try:
                d["spec"] = json.loads(spec)
            except Exception:
                d["spec"] = {}
        # Serialize UUID and datetime fields
        if "user_id" in d and d["user_id"]:
            d["user_id"] = str(d["user_id"])
        for ts_key in ("created_at", "updated_at"):
            if d.get(ts_key) and hasattr(d[ts_key], "isoformat"):
                d[ts_key] = d[ts_key].isoformat()
        return d
