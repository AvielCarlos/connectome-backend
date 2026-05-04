"""
RecommendationEngine — TikTok/YouTube Shorts-style Vector Recommendation

Two-tower recommendation using pgvector (already installed).

Two pools:
1. EXPLORE pool (20%): new/unproven cards — tested across users
2. EXPLOIT pool (80%): proven quality, matched via vector similarity

Card lifecycle:
  New card → explore pool (shown to 50-100 users)
  → if avg_rating > 3.5 → exploit pool
  → if avg_rating > 4.5 across 500+ users → viral pool (shown to anyone)

User matching:
  User embedding = EMA weighted average of cards rated ≥4
  At recommendation time: cosine similarity search against card_popularity
  Filter: already seen, too recent (same card twice in 7 days)
  Inject: localized cards, viral cards, goal flow cards

TODO: Add OPENAI_API_KEY to Railway env to enable full 1536-dim vector search.
      Without it, the system uses a deterministic hash-based fallback that
      still enables tag-based filtering and quality-based ranking.
"""

import hashlib
import json
import logging
import math
import random
import struct
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from core.config import settings
from core.database import fetch, fetchrow, execute, fetchval

logger = logging.getLogger(__name__)

# Embedding dimensions
EMBEDDING_DIM = 1536

# Card lifecycle thresholds
EXPLORE_GRADUATION_RATINGS = 50       # min ratings to leave explore pool
EXPLORE_GRADUATION_MIN_SCORE = 3.5    # min avg rating to graduate
VIRAL_MIN_RATINGS = 100               # ratings to check viral status
VIRAL_MIN_SCORE = 4.5                 # avg rating for viral status

# Feed mixing ratios
EXPLORE_RATIO = 0.20    # 20% from explore pool
EXPLOIT_RATIO = 0.80    # 80% from exploit pool

# EMA alpha for user embedding updates
EMA_ALPHA = 0.10        # new = alpha * card_emb + (1-alpha) * old_emb
GOAL_BLEND_RATIO = 0.30  # 30% goal embedding, 70% behavioral
IOO_BLEND_RATIO = 0.25   # 25% IOO graph fingerprint, 75% behavioral/goal blend


def _hash_text_to_embedding(text: str, dim: int = EMBEDDING_DIM) -> List[float]:
    """
    Deterministic text → float vector using SHA-256 hash seeds.
    NOT semantically meaningful, but enables consistent filtering/ranking
    until an OpenAI key is added.

    TODO: Replace with OpenAI text-embedding-3-small when OPENAI_API_KEY is set.
    """
    embedding = []
    seed = text.encode("utf-8")
    i = 0
    while len(embedding) < dim:
        h = hashlib.sha256(seed + i.to_bytes(4, "little")).digest()
        # Unpack 8 floats per hash block
        for j in range(0, len(h) - 3, 4):
            raw = struct.unpack_from("I", h, j)[0]
            # Map to [-1.0, 1.0]
            val = (raw / 0xFFFFFFFF) * 2.0 - 1.0
            embedding.append(val)
            if len(embedding) >= dim:
                break
        i += 1

    # L2 normalize
    norm = math.sqrt(sum(v * v for v in embedding))
    if norm > 0:
        embedding = [v / norm for v in embedding]

    return embedding[:dim]


def _embedding_to_pgvector(emb: List[float]) -> str:
    """Format a Python list as a pgvector literal string."""
    return "[" + ",".join(f"{v:.6f}" for v in emb) + "]"


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _ema_blend(old: List[float], new: List[float], alpha: float = EMA_ALPHA) -> List[float]:
    """Exponential moving average blend of two embeddings."""
    blended = [alpha * n + (1 - alpha) * o for o, n in zip(old, new)]
    # Re-normalize
    norm = math.sqrt(sum(v * v for v in blended))
    if norm > 0:
        blended = [v / norm for v in blended]
    return blended


def _weighted_blend(
    emb_a: List[float],
    weight_a: float,
    emb_b: List[float],
    weight_b: float,
) -> List[float]:
    """Weighted blend of two embeddings, normalized."""
    blended = [weight_a * a + weight_b * b for a, b in zip(emb_a, emb_b)]
    norm = math.sqrt(sum(v * v for v in blended))
    if norm > 0:
        blended = [v / norm for v in blended]
    return blended


class RecommendationEngine:
    """
    TikTok/YouTube Shorts-style recommendation engine.
    Uses pgvector for user-card similarity matching.
    """

    def __init__(self, openai_client=None):
        self.openai = openai_client

    # -----------------------------------------------------------------------
    # Embedding generation
    # -----------------------------------------------------------------------

    async def embed_card(self, card_content: str) -> List[float]:
        """
        Generate 1536-dim embedding for a card.
        Uses OpenAI text-embedding-3-small if API key present,
        otherwise falls back to deterministic hash embedding.
        """
        api_key = settings.OPENAI_API_KEY if hasattr(settings, "OPENAI_API_KEY") else ""

        if api_key:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=10.0) as client:
                    r = await client.post(
                        "https://api.openai.com/v1/embeddings",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={
                            "model": "text-embedding-3-small",
                            "input": card_content[:8000],  # token limit safety
                        },
                    )
                    r.raise_for_status()
                    return r.json()["data"][0]["embedding"]
            except Exception as e:
                logger.warning(f"RecommendationEngine.embed_card OpenAI failed: {e} — using hash fallback")

        # Deterministic hash fallback
        # TODO: Add OPENAI_API_KEY to Railway env for semantic embeddings
        return _hash_text_to_embedding(card_content)

    async def embed_text(self, text: str) -> List[float]:
        """Alias for embed_card — embed any text string."""
        return await self.embed_card(text)

    # -----------------------------------------------------------------------
    # Card embedding and registration
    # -----------------------------------------------------------------------

    async def embed_new_card(self, card_id: str, screen_spec: Dict[str, Any]) -> bool:
        """
        Called when a new card is generated.
        Embeds the card content and inserts into card_popularity (explore pool).
        """
        try:
            # Extract text content from screen spec
            content_parts = []
            spec_type = screen_spec.get("type", "")
            if spec_type:
                content_parts.append(spec_type)

            for comp in screen_spec.get("components", []):
                if isinstance(comp, dict):
                    for field in ("text", "title", "body", "label", "subtitle"):
                        val = comp.get(field)
                        if val:
                            content_parts.append(str(val))

            meta = screen_spec.get("metadata", {})
            if meta.get("agent"):
                content_parts.append(meta["agent"])
            if meta.get("topic"):
                content_parts.append(meta["topic"])

            card_content = " ".join(content_parts)[:2000]
            embedding = await self.embed_card(card_content)
            emb_str = _embedding_to_pgvector(embedding)

            # Determine tags
            archetype = screen_spec.get("archetype") or meta.get("archetype") or ""
            goal_tags = [archetype] if archetype else []

            await execute(
                """
                INSERT INTO card_popularity
                    (card_id, total_views, total_ratings, avg_rating,
                     high_rating_count, share_eligible, is_viral,
                     goal_tags, archetype, embedding, created_at, updated_at)
                VALUES ($1, 1, 0, 0, 0, FALSE, FALSE, $2, $3, $4::vector, NOW(), NOW())
                ON CONFLICT (card_id) DO UPDATE
                    SET total_views = card_popularity.total_views + 1,
                        updated_at = NOW()
                """,
                card_id,
                goal_tags or [],
                archetype or None,
                emb_str,
            )
            return True
        except Exception as e:
            logger.warning(f"RecommendationEngine.embed_new_card failed for {card_id}: {e}")
            return False

    # -----------------------------------------------------------------------
    # User embedding updates
    # -----------------------------------------------------------------------

    async def update_user_embedding(
        self, user_id: str, rated_card_id: str, rating: float
    ) -> bool:
        """
        After user rates a card ≥4:
        1. Fetch the card's embedding
        2. EMA update: new = 0.9 * old + 0.1 * card_embedding
        3. If user has goals: blend with goal embeddings (30% goal, 70% behavior)
        4. Store in user_interest_vectors
        """
        if rating < 4:
            return False

        try:
            # Fetch card embedding
            card_row = await fetchrow(
                "SELECT embedding FROM card_popularity WHERE card_id = $1",
                rated_card_id,
            )
            if not card_row or not card_row["embedding"]:
                return False

            raw_emb = card_row["embedding"]
            if isinstance(raw_emb, str):
                card_emb = json.loads(raw_emb.replace("[", "[").replace("]", "]"))
            elif isinstance(raw_emb, list):
                card_emb = raw_emb
            else:
                return False

            # Fetch current user embedding
            uiv_row = await fetchrow(
                "SELECT embedding, goal_embedding, total_cards_rated FROM user_interest_vectors WHERE user_id = $1::uuid",
                user_id,
            )

            if uiv_row and uiv_row["embedding"]:
                raw_user_emb = uiv_row["embedding"]
                if isinstance(raw_user_emb, str):
                    user_emb = json.loads(raw_user_emb.replace("[", "[").replace("]", "]"))
                else:
                    user_emb = list(raw_user_emb)
                new_behavior_emb = _ema_blend(user_emb, card_emb, EMA_ALPHA)
                total_rated = (uiv_row["total_cards_rated"] or 0) + 1
            else:
                new_behavior_emb = card_emb
                total_rated = 1

            # Blend with goal embedding if available
            goal_emb_raw = uiv_row["goal_embedding"] if uiv_row else None
            if goal_emb_raw:
                if isinstance(goal_emb_raw, str):
                    goal_emb = json.loads(goal_emb_raw)
                else:
                    goal_emb = list(goal_emb_raw)
                combined_emb = _weighted_blend(
                    goal_emb, GOAL_BLEND_RATIO,
                    new_behavior_emb, 1.0 - GOAL_BLEND_RATIO
                )
            else:
                combined_emb = new_behavior_emb

            emb_str = _embedding_to_pgvector(new_behavior_emb)
            combined_str = _embedding_to_pgvector(combined_emb)

            await execute(
                """
                INSERT INTO user_interest_vectors
                    (user_id, embedding, combined_embedding, total_cards_rated, last_updated)
                VALUES ($1::uuid, $2::vector, $3::vector, $4, NOW())
                ON CONFLICT (user_id) DO UPDATE
                    SET embedding = $2::vector,
                        combined_embedding = $3::vector,
                        total_cards_rated = $4,
                        last_updated = NOW()
                """,
                user_id, emb_str, combined_str, total_rated,
            )
            return True
        except Exception as e:
            logger.warning(f"RecommendationEngine.update_user_embedding failed: {e}")
            return False

    async def update_user_goal_embedding(self, user_id: str) -> bool:
        """
        Recompute goal embedding from user's active goals.
        Called when a goal is added or updated.
        """
        try:
            goals = await fetch(
                "SELECT title, description FROM goals WHERE user_id = $1::uuid AND status = 'active'",
                user_id,
            )
            if not goals:
                return False

            goal_texts = []
            for g in goals:
                parts = []
                if g["title"]:
                    parts.append(g["title"])
                if g["description"]:
                    parts.append(g["description"])
                goal_texts.append(" ".join(parts))

            combined_goal_text = " | ".join(goal_texts)
            goal_emb = await self.embed_text(combined_goal_text)
            goal_emb_str = _embedding_to_pgvector(goal_emb)

            # Fetch current behavior embedding
            uiv_row = await fetchrow(
                "SELECT embedding FROM user_interest_vectors WHERE user_id = $1::uuid",
                user_id,
            )

            if uiv_row and uiv_row["embedding"]:
                raw_b = uiv_row["embedding"]
                behavior_emb = json.loads(raw_b) if isinstance(raw_b, str) else list(raw_b)
                combined = _weighted_blend(
                    goal_emb, GOAL_BLEND_RATIO,
                    behavior_emb, 1.0 - GOAL_BLEND_RATIO
                )
            else:
                combined = goal_emb

            combined_str = _embedding_to_pgvector(combined)

            await execute(
                """
                INSERT INTO user_interest_vectors
                    (user_id, goal_embedding, combined_embedding, last_updated)
                VALUES ($1::uuid, $2::vector, $3::vector, NOW())
                ON CONFLICT (user_id) DO UPDATE
                    SET goal_embedding = $2::vector,
                        combined_embedding = $3::vector,
                        last_updated = NOW()
                """,
                user_id, goal_emb_str, combined_str,
            )
            return True
        except Exception as e:
            logger.warning(f"RecommendationEngine.update_user_goal_embedding: {e}")
            return False

    # -----------------------------------------------------------------------
    # Card popularity tracking
    # -----------------------------------------------------------------------

    async def update_card_popularity(
        self, card_id: str, rating: float, user_id: str
    ) -> bool:
        """
        After every rating:
        1. Update avg_rating, total_ratings, high_rating_count
        2. Check viral threshold (avg > 4.5, ratings > 100)
        3. Check explore graduation (ratings > 50, avg > 3.5)
        """
        try:
            row = await fetchrow(
                "SELECT avg_rating, total_ratings, high_rating_count FROM card_popularity WHERE card_id = $1",
                card_id,
            )

            if row:
                old_avg = row["avg_rating"] or 0.0
                total = row["total_ratings"] or 0
                high = row["high_rating_count"] or 0

                new_total = total + 1
                new_avg = (old_avg * total + rating) / new_total
                new_high = high + (1 if rating >= 4 else 0)

                is_viral = new_avg >= VIRAL_MIN_SCORE and new_total >= VIRAL_MIN_RATINGS
                share_eligible = new_avg >= EXPLORE_GRADUATION_MIN_SCORE and new_total >= EXPLORE_GRADUATION_RATINGS

                await execute(
                    """
                    UPDATE card_popularity
                    SET avg_rating = $1,
                        total_ratings = $2,
                        high_rating_count = $3,
                        is_viral = $4,
                        share_eligible = $5,
                        updated_at = NOW()
                    WHERE card_id = $6
                    """,
                    new_avg, new_total, new_high,
                    is_viral, share_eligible,
                    card_id,
                )

                if is_viral:
                    logger.info(
                        f"RecommendationEngine: card {card_id[:8]} went VIRAL "
                        f"(avg={new_avg:.2f}, ratings={new_total})"
                    )
                elif share_eligible:
                    logger.debug(
                        f"RecommendationEngine: card {card_id[:8]} graduated to exploit pool "
                        f"(avg={new_avg:.2f}, ratings={new_total})"
                    )
            else:
                # Card not tracked yet — insert it
                is_high = rating >= 4
                await execute(
                    """
                    INSERT INTO card_popularity
                        (card_id, total_ratings, avg_rating, high_rating_count,
                         share_eligible, is_viral, created_at, updated_at)
                    VALUES ($1, 1, $2, $3, FALSE, FALSE, NOW(), NOW())
                    ON CONFLICT (card_id) DO NOTHING
                    """,
                    card_id, rating, 1 if is_high else 0,
                )
            return True
        except Exception as e:
            logger.warning(f"RecommendationEngine.update_card_popularity failed: {e}")
            return False

    # -----------------------------------------------------------------------
    # Main recommendation function
    # -----------------------------------------------------------------------

    async def get_recommended_cards(
        self,
        user_id: str,
        count: int = 10,
        location: str = None,
    ) -> List[str]:
        """
        Main recommendation function — returns list of card_ids.

        Algorithm:
        1. Fetch user's combined_embedding
        2. Pull the user's IOO vector profile and blend it in
        3. Vector similarity search against card_popularity
        4. Re-rank by similarity, quality, recency, and IOO alignment
        5. Insert explore cards (20%)
        6. Return final ranked list
        """
        try:
            # Fetch user embedding
            uiv_row = await fetchrow(
                "SELECT combined_embedding, total_cards_rated FROM user_interest_vectors WHERE user_id = $1::uuid",
                user_id,
            )

            ioo_vector: List[float] = []
            try:
                from aura.agents.ioo_graph_agent import get_graph_agent
                ioo_vector = await get_graph_agent().build_user_ioo_vector(user_id)
            except Exception as ioe:
                logger.debug(f"RecommendationEngine: IOO profile unavailable: {ioe}")

            # Get recently seen cards (last 7 days)
            recent_seen = await fetch(
                """
                SELECT DISTINCT screen_spec_id::text as card_id
                FROM interactions
                WHERE user_id = $1::uuid
                  AND created_at > NOW() - INTERVAL '7 days'
                  AND screen_spec_id IS NOT NULL
                """,
                user_id,
            )
            seen_ids = {str(r["card_id"]) for r in recent_seen}

            exploit_count = max(1, int(count * EXPLOIT_RATIO))
            explore_count = max(1, count - exploit_count)

            exploit_cards = []
            explore_cards = []

            if uiv_row and uiv_row["combined_embedding"] and uiv_row["total_cards_rated"] > 5:
                # Vector similarity search for exploit pool. Blend Aura's
                # behavioral/goal vector with the IOO graph fingerprint so feed
                # cards preferentially align with where the user is in their
                # achievement graph.
                raw_emb = uiv_row["combined_embedding"]
                behavior_emb = json.loads(raw_emb) if isinstance(raw_emb, str) else list(raw_emb)
                if ioo_vector:
                    combined_for_feed = _weighted_blend(
                        ioo_vector, IOO_BLEND_RATIO,
                        behavior_emb, 1.0 - IOO_BLEND_RATIO,
                    )
                else:
                    combined_for_feed = behavior_emb
                emb_str = _embedding_to_pgvector(combined_for_feed)

                location_filter = ""
                location_params = []
                if location:
                    location_filter = "AND (location_tags @> ARRAY[$3] OR location_tags = '{}'::text[])"
                    location_params = [location.lower()]

                query = f"""
                    SELECT card_id,
                           1 - (embedding <=> $2::vector) as similarity,
                           avg_rating, total_ratings,
                           EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0 as age_days
                    FROM card_popularity
                    WHERE share_eligible = TRUE
                      AND embedding IS NOT NULL
                      {location_filter}
                    ORDER BY embedding <=> $2::vector
                    LIMIT $1
                """
                params = [exploit_count * 3, emb_str] + location_params

                try:
                    candidates = await fetch(query, *params)
                except Exception as ve:
                    logger.debug(f"Vector search failed (no data yet?): {ve}")
                    candidates = []

                # Re-rank: similarity*0.6 + avg_rating/5*0.3 + recency_bonus*0.1
                scored = []
                for row in candidates:
                    card_id = row["card_id"]
                    if card_id in seen_ids:
                        continue
                    sim = float(row["similarity"] or 0)
                    avg_r = float(row["avg_rating"] or 0)
                    age = float(row["age_days"] or 30)
                    recency = max(0, 1.0 - age / 30.0)  # 1.0 for brand new, 0.0 for 30+ days
                    ioo_bonus = 0.05 if ioo_vector else 0.0
                    score = sim * 0.6 + (avg_r / 5.0) * 0.3 + recency * 0.1 + ioo_bonus
                    scored.append((card_id, score))

                scored.sort(key=lambda x: x[1], reverse=True)
                exploit_cards = [c[0] for c in scored[:exploit_count]]
            else:
                # Cold start: return highest-rated cards
                top_cards = await fetch(
                    """
                    SELECT card_id FROM card_popularity
                    WHERE share_eligible = TRUE
                    ORDER BY avg_rating DESC, total_ratings DESC
                    LIMIT $1
                    """,
                    exploit_count,
                )
                exploit_cards = [r["card_id"] for r in top_cards if r["card_id"] not in seen_ids]

            # Explore pool: random new/unproven cards
            try:
                explore_rows = await fetch(
                    """
                    SELECT card_id FROM card_popularity
                    WHERE share_eligible = FALSE
                      AND total_ratings < $1
                    ORDER BY RANDOM()
                    LIMIT $2
                    """,
                    EXPLORE_GRADUATION_RATINGS,
                    explore_count * 2,
                )
                explore_candidates = [r["card_id"] for r in explore_rows if r["card_id"] not in seen_ids]
                explore_cards = explore_candidates[:explore_count]
            except Exception:
                explore_cards = []

            # Mix: 80% exploit + 20% explore
            result = []
            exploit_iter = iter(exploit_cards)
            explore_iter = iter(explore_cards)
            explore_positions = set(
                random.randint(0, count - 1)
                for _ in range(max(1, int(count * EXPLORE_RATIO)))
            )

            for i in range(count):
                if i in explore_positions:
                    card = next(explore_iter, None)
                    if card:
                        result.append(card)
                        continue
                card = next(exploit_iter, None)
                if card:
                    result.append(card)

            return result

        except Exception as e:
            logger.warning(f"RecommendationEngine.get_recommended_cards failed: {e}")
            return []

    # -----------------------------------------------------------------------
    # Collaborative filtering
    # -----------------------------------------------------------------------

    async def find_similar_users(
        self, user_id: str, limit: int = 10
    ) -> List[str]:
        """
        Find users with similar interest vectors (collaborative filtering).
        Cards that similar users rated highly = good candidates for this user.
        """
        try:
            rows = await fetch(
                """
                SELECT user_id::text FROM user_interest_vectors
                WHERE user_id != $1::uuid
                  AND combined_embedding IS NOT NULL
                ORDER BY combined_embedding <=> (
                    SELECT combined_embedding FROM user_interest_vectors
                    WHERE user_id = $1::uuid
                )
                LIMIT $2
                """,
                user_id, limit,
            )
            return [str(r["user_id"]) for r in rows]
        except Exception as e:
            logger.debug(f"RecommendationEngine.find_similar_users: {e}")
            return []

    async def get_cards_from_similar_users(
        self, user_id: str, count: int = 5
    ) -> List[str]:
        """Get highly-rated cards from similar users (collaborative filtering)."""
        try:
            similar_users = await self.find_similar_users(user_id, limit=5)
            if not similar_users:
                return []

            recent_seen = await fetch(
                """
                SELECT DISTINCT screen_spec_id::text as card_id
                FROM interactions
                WHERE user_id = $1::uuid AND created_at > NOW() - INTERVAL '7 days'
                """,
                user_id,
            )
            seen_ids = {str(r["card_id"]) for r in recent_seen}

            rows = await fetch(
                """
                SELECT screen_spec_id::text as card_id, AVG(rating) as avg_r
                FROM interactions
                WHERE user_id = ANY($1::uuid[])
                  AND rating >= 4
                  AND screen_spec_id IS NOT NULL
                GROUP BY screen_spec_id
                ORDER BY avg_r DESC
                LIMIT $2
                """,
                [uuid.UUID(uid) for uid in similar_users],
                count * 2,
            )
            return [str(r["card_id"]) for r in rows if str(r["card_id"]) not in seen_ids][:count]
        except Exception as e:
            logger.debug(f"RecommendationEngine.get_cards_from_similar_users: {e}")
            return []


# ---------------------------------------------------------------------------
# CardLibraryAgent — Universal experience card library
# ---------------------------------------------------------------------------

class CardLibraryAgent:
    """
    Manages the universal card library.

    Universal experiences that work for thousands of users,
    personalized with local context:
    - "Try skydiving in [LOCATION]" — localized with real dropzone
    - "Watch a live concert this week" — localized with local events
    - "Spend a day with no phone" — universal, no localization needed

    Cards are pre-generated, embedded, and shared across users.
    """

    UNIVERSAL_EXPERIENCES = [
        "skydiving",
        "hiking local trails",
        "live music",
        "cooking a new cuisine",
        "cold water swimming",
        "learn to surf",
        "silent retreat",
        "volunteer work",
        "overnight camping",
        "speak to a stranger",
        "take a class in something new",
        "watch a sunrise alone",
    ]

    def __init__(self, openai_client=None):
        self.openai = openai_client
        self._rec_engine = RecommendationEngine(openai_client)

    async def generate_localized_card(
        self, experience: str, location: str
    ) -> Optional[Dict[str, Any]]:
        """
        Generate a card for a universal experience, localized to user's area.
        Uses web search to find real local opportunities.
        """
        from aura.agents.goal_flow_agent import GoalFlowAgent
        gfa = GoalFlowAgent(self.openai)
        opps = await gfa.find_local_opportunities(experience, location)

        card_id = str(uuid.uuid4())

        if self.openai and opps:
            try:
                opp = opps[0]
                prompt = (
                    f"Generate a card for the experience: {experience}. "
                    f"Location: {location}. "
                    f"Local option found: {opp.get('title', '')} at {opp.get('url', '')}. "
                    "Create a compelling 2-sentence description and CTA. "
                    "Return JSON with: title, body, cta"
                )
                resp = await self.openai.chat.completions.create(
                    model="gpt-4o-mini",
                    max_tokens=150,
                    response_format={"type": "json_object"},
                    messages=[{"role": "user", "content": prompt}],
                )
                data = json.loads(resp.choices[0].message.content)
                title = data.get("title", f"Try {experience} in {location}")
                body = data.get("body", f"Discover {experience} options near {location}.")
                cta = data.get("cta", "Explore options")
            except Exception:
                title = f"Try {experience.title()} in {location}"
                body = f"Discover {experience} options near you."
                cta = "Find options"
        else:
            title = f"Try {experience.title()}"
            body = f"Have you ever tried {experience}? Here's how to start near {location or 'you'}."
            cta = "Find options"

        card = {
            "screen_id": card_id,
            "type": "universal_experience_card",
            "layout": "scroll",
            "components": [
                {"type": "hero_text", "text": title, "style": "bold"},
                {"type": "body_text", "text": body},
                {"type": "cta_button", "label": cta, "action": "open_url",
                 "url": opps[0].get("url", "") if opps else ""},
            ],
            "feedback_overlay": {
                "type": "star_rating",
                "position": "bottom_right",
                "always_visible": True,
            },
            "metadata": {
                "agent": "CardLibraryAgent",
                "experience": experience,
                "location": location,
                "is_universal": True,
            },
        }

        # Register in card_popularity
        await self._rec_engine.embed_new_card(card_id, card)

        return card

    async def seed_universal_cards(self, major_cities: Optional[List[str]] = None) -> int:
        """
        Weekly: ensure all universal experiences have cards in the library.
        For each experience + major city combination.
        Returns number of cards created.
        """
        cities = major_cities or ["Toronto", "New York", "London", "Sydney", "Berlin"]
        created = 0

        for experience in self.UNIVERSAL_EXPERIENCES:
            for city in cities:
                try:
                    card = await self.generate_localized_card(experience, city)
                    if card:
                        created += 1
                        logger.info(f"CardLibrary: seeded card for {experience} in {city}")
                except Exception as e:
                    logger.warning(f"CardLibrary: failed {experience}/{city}: {e}")

        return created


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_rec_engine: Optional[RecommendationEngine] = None
_card_library: Optional[CardLibraryAgent] = None


def get_recommendation_engine(openai_client=None) -> RecommendationEngine:
    global _rec_engine
    if _rec_engine is None:
        _rec_engine = RecommendationEngine(openai_client)
    return _rec_engine


def get_card_library_agent(openai_client=None) -> CardLibraryAgent:
    global _card_library
    if _card_library is None:
        _card_library = CardLibraryAgent(openai_client)
    return _card_library
