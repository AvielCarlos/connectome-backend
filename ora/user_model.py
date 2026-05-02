"""
Ora User Model
Loads, caches, and updates per-user models including embedding.
The embedding encodes preference signals and is updated on each feedback.
"""

import json
import logging
import numpy as np
from typing import Optional, List, Dict, Any
from uuid import UUID

from core.database import fetchrow, execute, fetch
from core.redis_client import redis_get, redis_set, redis_delete

logger = logging.getLogger(__name__)

USER_CACHE_TTL = 300  # 5 minutes


class UserModel:
    """
    Encapsulates everything Ora knows about a user.
    Loaded fresh from DB, cached in Redis.
    """

    def __init__(self, row: Dict[str, Any]):
        self.id: str = str(row["id"])
        self.subscription_tier: str = row.get("subscription_tier", "free")
        self.fulfilment_score: float = row.get("fulfilment_score") or 0.0
        raw_profile = row.get("profile") or {}
        if isinstance(raw_profile, str):
            import json
            try:
                raw_profile = json.loads(raw_profile)
            except Exception:
                raw_profile = {}
        self.profile: Dict[str, Any] = raw_profile
        self.embedding: Optional[List[float]] = self._parse_embedding(
            row.get("embedding")
        )
        self.now_embedding: Optional[List[float]] = self._parse_embedding(
            row.get("now_embedding")
        )
        self.later_embedding: Optional[List[float]] = self._parse_embedding(
            row.get("later_embedding")
        )
        self.goals: List[Dict[str, Any]] = []
        self.recent_interactions: List[Dict[str, Any]] = []
        # Domain weights: how much the user engages with each domain
        self.domain_weights: Dict[str, float] = raw_profile.get(
            "domain_weights", {"iVive": 0.33, "Eviva": 0.33, "Aventi": 0.33}
        )

    @staticmethod
    def _parse_embedding(raw) -> Optional[List[float]]:
        if raw is None:
            return None
        if isinstance(raw, str):
            # pgvector returns '[1,2,3]' format
            raw = raw.strip("[]")
            return [float(x) for x in raw.split(",") if x.strip()]
        if isinstance(raw, list):
            return [float(x) for x in raw]
        return None

    def to_context_dict(self) -> Dict[str, Any]:
        """Compact user context for Ora's agent prompts."""
        return {
            "user_id": self.id,
            "subscription_tier": self.subscription_tier,
            "fulfilment_score": round(self.fulfilment_score, 3),
            "interests": self.profile.get("interests", []),
            "display_name": self.profile.get("display_name", ""),
            "location": self.profile.get("location", ""),
            "active_goals": [
                {
                    "id": g["id"],
                    "title": g["title"],
                    "progress": g["progress"],
                    "domain": g.get("domain", "iVive"),
                }
                for g in self.goals
                if g.get("status") == "active"
            ],
            "recent_ratings": [
                i["rating"]
                for i in self.recent_interactions[-10:]
                if i.get("rating")
            ],
            "domain_weights": self.domain_weights,
        }


async def load_user_model(user_id: str) -> Optional[UserModel]:
    """Load user model from cache or DB, with goals and recent interactions."""
    cache_key = f"user_model:{user_id}"
    cached = await redis_get(cache_key)
    if cached:
        # Reconstruct from cache dict
        model = UserModel(cached)
        model.goals = cached.get("goals", [])
        model.recent_interactions = cached.get("recent_interactions", [])
        return model

    row = await fetchrow(
        "SELECT * FROM users WHERE id = $1", UUID(user_id)
    )
    if not row:
        return None

    model = UserModel(dict(row))

    # Load active goals
    goal_rows = await fetch(
        "SELECT id, title, description, status, steps, progress FROM goals "
        "WHERE user_id = $1 AND status = 'active' ORDER BY created_at DESC LIMIT 10",
        UUID(user_id),
    )
    model.goals = [dict(g) for g in goal_rows]

    # Load last 20 interactions
    interaction_rows = await fetch(
        "SELECT rating, exit_point, completed, time_on_screen_ms, "
        "s.agent_type FROM interactions i "
        "LEFT JOIN screen_specs s ON s.id = i.screen_spec_id "
        "WHERE i.user_id = $1 ORDER BY i.created_at DESC LIMIT 20",
        UUID(user_id),
    )
    model.recent_interactions = [dict(r) for r in interaction_rows]

    # Cache it
    cache_payload = {
        "id": model.id,
        "subscription_tier": model.subscription_tier,
        "fulfilment_score": model.fulfilment_score,
        "profile": model.profile,
        "embedding": model.embedding,
        "now_embedding": model.now_embedding,
        "later_embedding": model.later_embedding,
        "goals": model.goals,
        "recent_interactions": model.recent_interactions,
        "domain_weights": model.domain_weights,
    }
    await redis_set(cache_key, cache_payload, ttl_seconds=USER_CACHE_TTL)

    return model


async def update_user_embedding(user_id: str, rating: int, agent_type: str):
    """
    Nudge the user's preference embedding based on a rating.
    High rating → reinforce this agent_type direction.
    Low rating → push away.
    """
    model = await load_user_model(user_id)
    if not model:
        return

    # Simple preference signal: map agent types to embedding dimensions
    AGENT_DIMS = {
        "DiscoveryAgent": 0,
        "CoachingAgent": 1,
        "RecommendationAgent": 2,
        "UIGeneratorAgent": 3,
    }

    current = np.array(model.embedding) if model.embedding else np.zeros(1536)
    dim = AGENT_DIMS.get(agent_type, 0)

    # Normalized rating (-1 to 1)
    signal = (rating - 3.0) / 2.0  # maps 1→-1, 3→0, 5→1

    # Update the relevant dimension with small learning rate
    lr = 0.05
    current[dim] = float(np.clip(current[dim] + lr * signal, -1, 1))

    # Normalize the vector
    norm = np.linalg.norm(current)
    if norm > 0:
        current = current / norm

    embedding_str = "[" + ",".join(f"{v:.6f}" for v in current.tolist()) + "]"

    await execute(
        "UPDATE users SET embedding = $1::vector WHERE id = $2",
        embedding_str,
        UUID(user_id),
    )

    # Update fulfilment score (rolling EMA)
    new_score = model.fulfilment_score * 0.95 + (rating / 5.0) * 0.05
    await execute(
        "UPDATE users SET fulfilment_score = $1, last_active = NOW() WHERE id = $2",
        new_score,
        UUID(user_id),
    )

    # Invalidate cache
    await redis_delete(f"user_model:{user_id}")
    logger.debug(f"Updated embedding for user {user_id}, rating={rating}")


async def update_user_embedding_from_cards(user_id: str, openai_client=None) -> None:
    """
    Integration A: Update the user's embedding by averaging embeddings of recently
    high-rated (>=4) screen_specs. Called fire-and-forget at the end of get_screen().
    Uses actual semantic content similarity instead of synthetic dimension nudging.
    """
    try:
        rows = await fetch(
            """
            SELECT ss.embedding
            FROM interactions i
            JOIN screen_specs ss ON ss.id = i.screen_spec_id
            WHERE i.user_id = $1
              AND i.rating >= 4
              AND ss.embedding IS NOT NULL
            ORDER BY i.created_at DESC
            LIMIT 20
            """,
            UUID(user_id),
        )
        if not rows:
            return

        embeddings = []
        for row in rows:
            raw = row["embedding"]
            if raw:
                parsed = UserModel._parse_embedding(raw)
                if parsed and len(parsed) == 1536:
                    embeddings.append(np.array(parsed, dtype=np.float32))

        if not embeddings:
            return

        avg_embedding = np.mean(embeddings, axis=0)
        norm = np.linalg.norm(avg_embedding)
        if norm > 0:
            avg_embedding = avg_embedding / norm

        embedding_str = "[" + ",".join(f"{v:.6f}" for v in avg_embedding.tolist()) + "]"

        await execute(
            "UPDATE users SET embedding = $1::vector WHERE id = $2",
            embedding_str,
            UUID(user_id),
        )
        await redis_delete(f"user_model:{user_id}")
        logger.debug(
            f"update_user_embedding_from_cards: user={user_id[:8]} averaged {len(embeddings)} card embeddings"
        )
    except Exception as e:
        logger.debug(f"update_user_embedding_from_cards failed: {e}")


async def update_user_embedding_from_context(
    user_id: str,
    context_answers: dict,
    vector_mode: str = "now",
) -> None:
    """
    Re-embed a mode-specific user vector from capability/context answers.

    now_embedding   = current-state/actionability vector (energy, time, resources)
    later_embedding = future-planning/aspiration vector (interests, larger goals)

    The global users.embedding is kept in sync with Now for legacy ranking paths.
    """
    try:
        from core.config import settings
        api_key = getattr(settings, "OPENAI_API_KEY", "")
        if not api_key:
            return

        vector_mode = "later" if vector_mode in ("later", "future") else "now"
        model = await load_user_model(user_id)
        profile = model.profile if model else {}

        lines = [f"User {vector_mode.upper()} vector for personalised feed recommendations:"]
        if vector_mode == "later":
            lines.append("Hard temporal contract: Later/Future vector is only for scheduled or bookable opportunities — events, classes, programs, trips, appointments, reservations, tickets, RSVPs, registration, and future planning. Exclude generic actions that can be done right now.")
        else:
            lines.append("Hard temporal contract: Now vector is only for immediate current-state actions that can begin or complete right now/today. Exclude scheduled events, bookings, classes, trips, reservations, tickets, RSVPs, and future opportunities.")

        prompt_key = "later_vector_prompt" if vector_mode == "later" else "now_vector_prompt"
        manual_prompt = context_answers.get(prompt_key) or profile.get(prompt_key)
        if manual_prompt:
            lines.append(f"User manual {vector_mode} guidance: {manual_prompt}")

        capacity = context_answers.get("today_capacity") or profile.get("today_capacity")
        if capacity and vector_mode == "now":
            lines.append(f"Available time today: {capacity}")

        energy = context_answers.get("current_energy_state") or profile.get("current_energy_state")
        if energy and vector_mode == "now":
            lines.append(f"Energy level right now: {energy}")

        resources = context_answers.get("available_resources_today") or profile.get("available_resources_today")
        if resources and vector_mode == "now":
            if isinstance(resources, list):
                lines.append(f"Available resources now: {', '.join(resources)}")
            else:
                lines.append(f"Available resources now: {resources}")

        interests = context_answers.get("interests") or profile.get("interests", [])
        later_interests = context_answers.get("later_interests") or profile.get("later_interests", [])
        chosen_interests = later_interests if vector_mode == "later" and later_interests else interests
        if chosen_interests:
            if isinstance(chosen_interests, list):
                lines.append(f"Interests: {', '.join(chosen_interests[:10])}")
            else:
                lines.append(f"Interests: {chosen_interests}")

        value_scores = profile.get("value_weights") or profile.get("value_scores") or profile.get("value_compass") or {}
        if value_scores:
            top = sorted(value_scores.items(), key=lambda x: -x[1])[:4]
            lines.append(f"Top values: {', '.join(f'{k} ({v}/10)' for k, v in top)}")

        active_goals = context_answers.get("active_goals") or []
        if model and model.goals:
            active_goals = [g["title"] for g in model.goals if g.get("status") == "active"][:5]
        if active_goals:
            prefix = "Active immediate goals" if vector_mode == "now" else "Future/later path goals"
            lines.append(f"{prefix}: {', '.join(active_goals)}")

        present_state_fields = [
            ("active_goal_title", "Goal Aura is optimising for now"),
            ("goal_current_stage", "Current stage toward that goal"),
            ("goal_biggest_gap", "Biggest present-state gap/blocker"),
            ("current_constraint", "Current constraint"),
            ("social_bandwidth", "Social bandwidth"),
            ("mobility_now", "Mobility right now"),
            ("desired_next_step_style", "Desired next-step style"),
        ]
        for key, label in present_state_fields:
            value = context_answers.get(key) or profile.get(key)
            if value:
                if isinstance(value, list):
                    value = ", ".join(str(v) for v in value)
                lines.append(f"{label}: {value}")

        location = profile.get("location") or profile.get("city")
        if location:
            lines.append(f"Location: {location}")

        embed_text = "\n".join(lines)

        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": "text-embedding-3-small", "input": embed_text[:8000]},
            )
            r.raise_for_status()
            embedding = r.json()["data"][0]["embedding"]

        existing_mode = model.later_embedding if (model and vector_mode == "later") else (model.now_embedding if model else None)
        existing_global = model.embedding if model else None
        existing = existing_mode if existing_mode and len(existing_mode) == 1536 else existing_global
        if existing and len(existing) == 1536:
            arr = np.array(embedding, dtype=np.float32)
            old = np.array(existing, dtype=np.float32)
            blended = 0.8 * arr + 0.2 * old
            norm = np.linalg.norm(blended)
            if norm > 0:
                blended = blended / norm
            embedding = blended.tolist()

        embedding_str = "[" + ",".join(f"{v:.6f}" for v in embedding) + "]"
        if vector_mode == "now":
            await execute(
                "UPDATE users SET embedding = $1::vector, now_embedding = $1::vector WHERE id = $2",
                embedding_str,
                UUID(user_id),
            )
        else:
            await execute(
                "UPDATE users SET later_embedding = $1::vector WHERE id = $2",
                embedding_str,
                UUID(user_id),
            )
        await redis_delete(f"user_model:{user_id}")
        logger.info(f"update_user_embedding_from_context: user={user_id[:8]} mode={vector_mode} re-embedded")

    except Exception as e:
        logger.debug(f"update_user_embedding_from_context failed: {e}")


async def update_domain_weights(
    user_id: str,
    domain: str,
    rating: int,
) -> None:
    """
    Shift domain weights based on a rating for content in a given domain.
    High ratings boost the domain; low ratings reduce it.
    Weights are stored in user profile JSONB and kept normalized (sum = 1.0).
    """
    if domain not in ("iVive", "Eviva", "Aventi"):
        return

    model = await load_user_model(user_id)
    if not model:
        return

    weights = dict(model.domain_weights)
    # Normalized rating signal: -0.05 to +0.05 per interaction
    signal = (rating - 3.0) / 2.0 * 0.05
    weights[domain] = max(0.05, weights.get(domain, 0.33) + signal)

    # Normalize so weights sum to 1.0
    total = sum(weights.values())
    weights = {k: v / total for k, v in weights.items()}

    # Persist in user profile JSONB
    await execute(
        """
        UPDATE users
        SET profile = jsonb_set(
            COALESCE(profile, '{}'),
            '{domain_weights}',
            $1::jsonb
        )
        WHERE id = $2
        """,
        json.dumps(weights),
        UUID(user_id),
    )
    await redis_delete(f"user_model:{user_id}")
    logger.debug(f"Domain weights updated for user {user_id}: {weights}")


async def update_aura_memory(user_id: str, session_summary: dict, openai_client=None) -> str:
    """
    Update Ora's running narrative memory for a user.
    Stored in users.profile['ora_memory'] — max 500 chars.
    Called after each session summary.
    """
    user_row = await fetchrow(
        "SELECT profile FROM users WHERE id = $1", UUID(user_id)
    )
    if not user_row:
        return ""

    _rp = user_row["profile"] or {}
    profile = json.loads(_rp) if isinstance(_rp, str) else (_rp or {})
    existing_memory = profile.get("ora_memory", "")

    # Build new memory via LLM or structured fallback
    new_memory = await _build_aura_memory(
        user_id=user_id,
        existing_memory=existing_memory,
        session_summary=session_summary,
        openai_client=openai_client,
    )

    # Truncate to 500 chars
    new_memory = new_memory[:500]

    await execute(
        """
        UPDATE users
        SET profile = jsonb_set(
            COALESCE(profile, '{}'),
            '{ora_memory}',
            $1::jsonb
        )
        WHERE id = $2
        """,
        json.dumps(new_memory),
        UUID(user_id),
    )
    await redis_delete(f"user_model:{user_id}")
    logger.debug(f"ora_memory updated for user {user_id[:8]}")
    return new_memory


async def _build_aura_memory(
    user_id: str,
    existing_memory: str,
    session_summary: dict,
    openai_client=None,
) -> str:
    """Use LLM to update the ora_memory narrative, or fall back to structured prose."""
    if openai_client:
        try:
            from core.config import settings
            if not settings.has_openai:
                raise ValueError("no key")

            prompt = f"""You are Ora. Update your internal memory about this user.

Existing memory (may be empty): {existing_memory or '(none yet)'}

New session data:
- Screens shown: {session_summary.get('screens_shown', 0)}
- Highly rated: {session_summary.get('highly_rated', 0)}
- Early exits: {session_summary.get('early_exits', 0)}
- Emerging interests: {session_summary.get('emerging_interests', [])}
- Topics to avoid: {session_summary.get('avoid_topics', [])}
- Session note: {session_summary.get('ora_note', '')}
- Fulfilment delta: {session_summary.get('fulfilment_delta', 0):+.3f}

Write a single paragraph (max 500 chars) that represents your updated understanding of this user.
Be specific, honest, and useful for future decisions. No preamble."""

            response = await openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5,
                max_tokens=150,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.warning(f"ora_memory LLM failed: {e}")

    # Structured fallback
    interests = session_summary.get("emerging_interests", [])
    avoid = session_summary.get("avoid_topics", [])
    note = session_summary.get("ora_note", "")

    parts = []
    if existing_memory:
        parts.append(existing_memory.rstrip("."))
    if interests:
        parts.append(f"Engages with {', '.join(interests[:2])}")
    if avoid:
        parts.append(f"Disengages from {', '.join(avoid[:2])}")
    if note:
        parts.append(note[:120])

    return ". ".join(parts)[:500]


async def get_daily_screen_count(user_id: str) -> int:
    """How many screens has this user seen today?"""
    from core.redis_client import redis_get
    cache_key = f"screens_today:{user_id}"
    val = await redis_get(cache_key)
    return int(val) if val is not None else 0


async def increment_daily_screen_count(user_id: str) -> int:
    """Increment and return today's screen count."""
    from core.redis_client import redis_incr
    import datetime
    cache_key = f"screens_today:{user_id}"
    # TTL = seconds until midnight UTC
    now = datetime.datetime.utcnow()
    midnight = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    ttl = int((midnight - now).total_seconds()) + 60
    count = await redis_incr(cache_key, ttl_seconds=ttl)
    return count
