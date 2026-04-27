"""
Connectome Redis Client
Async Redis connection for caching and real-time signals.
"""

import redis.asyncio as aioredis
import json
import logging
from typing import Optional, Any
from core.config import settings

logger = logging.getLogger(__name__)

_redis: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    """Return the global Redis client, initializing if needed."""
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )
        logger.info("Redis client initialized")
    return _redis


async def close_redis():
    """Close the Redis connection."""
    global _redis
    if _redis:
        await _redis.aclose()
        _redis = None
        logger.info("Redis connection closed")


# ---------------------------------------------------------------------------
# Typed helpers
# ---------------------------------------------------------------------------

async def redis_get(key: str) -> Optional[Any]:
    """Get a JSON-decoded value from Redis."""
    r = await get_redis()
    val = await r.get(key)
    if val is None:
        return None
    try:
        return json.loads(val)
    except Exception:
        return val


async def redis_set(key: str, value: Any, ttl_seconds: Optional[int] = None):
    """Set a JSON-encoded value in Redis with optional TTL."""
    r = await get_redis()
    encoded = json.dumps(value)
    if ttl_seconds:
        await r.setex(key, ttl_seconds, encoded)
    else:
        await r.set(key, encoded)


async def redis_delete(key: str):
    r = await get_redis()
    await r.delete(key)


async def redis_publish(channel: str, message: Any):
    """Publish a message to a Redis pub/sub channel."""
    r = await get_redis()
    await r.publish(channel, json.dumps(message))


async def redis_incr(key: str, ttl_seconds: Optional[int] = None) -> int:
    """Increment a counter, optionally setting TTL on first creation."""
    r = await get_redis()
    val = await r.incr(key)
    if val == 1 and ttl_seconds:
        await r.expire(key, ttl_seconds)
    return val
