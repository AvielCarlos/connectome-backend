"""
Ora A/B Testing Engine
Assigns users to test variants and tracks results per variant.
"""

import hashlib
import logging
import random
from typing import Optional, Dict, Any, List
from uuid import UUID

from core.database import fetchrow, execute, fetch

logger = logging.getLogger(__name__)


def _assign_variant(user_id: str, test_name: str, variants: list) -> str:
    """
    Deterministic variant assignment: hash(user_id + test_name) mod n_variants.
    Same user always gets the same variant for a given test.
    """
    seed = f"{user_id}:{test_name}"
    hash_val = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    return variants[hash_val % len(variants)]


async def get_or_create_test(name: str, variants: list) -> Dict[str, Any]:
    """Fetch an A/B test by name, creating it if it doesn't exist."""
    row = await fetchrow("SELECT * FROM ab_tests WHERE name = $1", name)
    if row:
        return dict(row)

    # Create test with empty results
    import json
    variant_weights = {v: 0 for v in variants}
    results = {v: {"impressions": 0, "ratings": [], "avg_rating": 0.0} for v in variants}

    row = await fetchrow(
        """
        INSERT INTO ab_tests (name, variants, results, status)
        VALUES ($1, $2, $3, 'running')
        RETURNING *
        """,
        name,
        json.dumps(variant_weights),
        json.dumps(results),
    )
    return dict(row)


async def assign_user_variant(user_id: str, test_name: str, variants: list) -> str:
    """Get (or deterministically assign) a user's variant for a test."""
    test = await get_or_create_test(test_name, variants)
    variant = _assign_variant(user_id, test_name, variants)
    logger.debug(f"User {user_id} assigned to variant '{variant}' for test '{test_name}'")
    return variant


async def record_test_result(
    test_name: str, variant: str, rating: float, completed: bool
):
    """Update A/B test results with a new data point."""
    import json

    row = await fetchrow("SELECT * FROM ab_tests WHERE name = $1", test_name)
    if not row:
        return

    results: dict = dict(row["results"]) if row["results"] else {}
    if variant not in results:
        results[variant] = {"impressions": 0, "ratings": [], "avg_rating": 0.0}

    results[variant]["impressions"] += 1
    results[variant]["ratings"].append(rating)
    # Keep last 1000 ratings only
    results[variant]["ratings"] = results[variant]["ratings"][-1000:]
    ratings = results[variant]["ratings"]
    results[variant]["avg_rating"] = sum(ratings) / len(ratings) if ratings else 0.0

    await execute(
        "UPDATE ab_tests SET results = $1 WHERE name = $2",
        json.dumps(results),
        test_name,
    )


# ---------------------------------------------------------------------------
# UI Surface A/B testing — extended API
# ---------------------------------------------------------------------------


async def get_ui_variant(
    user_id: str,
    surface: str,
    variants: List[str],
    weights: Optional[List[float]] = None,
) -> str:
    """
    Assign a user to a UI variant for a given surface.
    Uses deterministic hashing (same user always gets same variant per surface)
    unless a winning variant has already been declared.
    """
    winner = await get_winning_variant(surface)
    if winner and winner in variants:
        return winner

    if weights:
        total = sum(weights)
        norm_weights = [w / total for w in weights]
        seed = f"{user_id}:{surface}:weighted"
        hash_val = int(hashlib.md5(seed.encode()).hexdigest(), 16) / (16 ** 32)
        cumulative = 0.0
        for variant, w in zip(variants, norm_weights):
            cumulative += w
            if hash_val <= cumulative:
                return variant
        return variants[-1]
    else:
        seed = f"{user_id}:{surface}"
        hash_val = int(hashlib.md5(seed.encode()).hexdigest(), 16)
        return variants[hash_val % len(variants)]


async def record_ui_event(
    surface: str,
    variant: str,
    event_type: str,
    value: float = 1.0,
) -> None:
    """
    Record a UI interaction event for a surface/variant pair.
    """
    import json
    try:
        await execute(
            """
            INSERT INTO ui_ab_events (surface, variant, event_type, value)
            VALUES ($1, $2, $3, $4)
            """,
            surface,
            variant,
            event_type,
            value,
        )
    except Exception as e:
        logger.debug(f"record_ui_event table miss, falling back: {e}")
        test_name = f"ui_surface_{surface}"
        try:
            await record_test_result(test_name, variant, value, event_type == "completion")
        except Exception as _fe:
            logger.debug(f"record_ui_event fallback also failed: {_fe}")


async def get_winning_variant(surface: str) -> Optional[str]:
    """
    Return the current winning variant for a surface, or None if no winner yet.
    A winner is declared when one variant has >= 100 impressions AND
    its avg_rating is > 0.05 above the next best variant.
    """
    import json
    test_name = f"ui_surface_{surface}"
    try:
        row = await fetchrow("SELECT * FROM ab_tests WHERE name = $1", test_name)
        if not row or not row["results"]:
            return None

        results: dict = dict(row["results"])
        candidates = [
            (v, data)
            for v, data in results.items()
            if isinstance(data, dict) and data.get("impressions", 0) >= 100
        ]
        if not candidates:
            return None

        candidates.sort(key=lambda x: x[1].get("avg_rating", 0.0), reverse=True)
        if len(candidates) >= 2:
            best_rating = candidates[0][1].get("avg_rating", 0.0)
            second_rating = candidates[1][1].get("avg_rating", 0.0)
            if best_rating - second_rating > 0.05:
                return candidates[0][0]
        elif len(candidates) == 1:
            return candidates[0][0]
        return None
    except Exception as e:
        logger.debug(f"get_winning_variant failed: {e}")
        return None
