"""
TwitterSignalAgent — Integration E
====================================
Reads a user's Twitter/X likes and engagement to bootstrap Ora's user model.
Uses the xurl CLI (already installed) to fetch liked tweets via the X API v2.

Pipeline:
  1. Resolve @handle → Twitter user ID via xurl
  2. Fetch recent liked tweets
  3. Extract topics from tweet text via OpenAI (gpt-4o-mini) or keyword fallback
  4. Infer interest categories from topics
  5. Update user profile in DB + cache-bust Redis
  6. Store full signals payload in Redis (7-day TTL) under user:{user_id}:twitter_signals
  7. Store ingestion status under user:{user_id}:twitter_ingestion_status
"""

import asyncio
import datetime
import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Broad interest categories derived from tweet topics
_INTEREST_MAP: Dict[str, List[str]] = {
    "tech": ["tech", "ai", "software", "code", "programming", "startup", "data", "cloud", "developer"],
    "health": ["health", "fitness", "wellness", "exercise", "mental", "nutrition", "sleep", "meditation"],
    "finance": ["finance", "crypto", "invest", "money", "trading", "bitcoin", "stock", "defi", "web3"],
    "culture": ["music", "film", "art", "culture", "book", "media", "design", "fashion", "cinema"],
    "science": ["science", "research", "physics", "space", "biology", "climate", "neuroscience"],
    "philosophy": ["philosophy", "ethics", "wisdom", "stoic", "meaning", "conscious", "existential"],
    "business": ["business", "entrepreneur", "marketing", "product", "growth", "saas", "founder"],
    "personal_growth": ["growth", "productivity", "habit", "mindset", "learn", "skills", "focus"],
    "social_impact": ["impact", "social", "community", "nonprofit", "cause", "activism", "education"],
    "creativity": ["creative", "writing", "poetry", "storytelling", "design", "game", "build"],
}


class TwitterSignalAgent:
    """
    Reads user's Twitter/X likes and engagement to bootstrap Ora's user model.
    Uses xurl CLI (already installed) to fetch liked tweets.
    """

    def __init__(self, openai_client=None):
        self._openai = openai_client

    # -----------------------------------------------------------------------
    # xurl helpers
    # -----------------------------------------------------------------------

    async def _xurl(self, path: str, timeout: int = 30) -> Optional[Dict[str, Any]]:
        """Execute an xurl GET request and return parsed JSON response."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "xurl", "get", path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            if proc.returncode == 0 and stdout:
                return json.loads(stdout.decode("utf-8", errors="replace"))
            err_msg = stderr.decode("utf-8", errors="replace")[:200] if stderr else "unknown error"
            logger.warning(f"xurl GET {path} exited {proc.returncode}: {err_msg}")
        except asyncio.TimeoutError:
            logger.warning(f"xurl GET {path} timed out after {timeout}s")
        except Exception as e:
            logger.debug(f"xurl error for {path}: {e}")
        return None

    async def _get_twitter_user_id(self, twitter_handle: str) -> Optional[str]:
        """Resolve a Twitter @handle to a numeric user ID."""
        handle = twitter_handle.lstrip("@").strip()
        result = await self._xurl(f"/2/users/by/username/{handle}")
        if result and "data" in result:
            uid = result["data"].get("id")
            if uid:
                logger.debug(f"Resolved @{handle} → Twitter ID {uid}")
                return str(uid)
        logger.warning(f"Could not resolve Twitter handle @{handle}: {result}")
        return None

    async def _get_liked_tweets(
        self, twitter_user_id: str, max_results: int = 50
    ) -> List[Dict[str, Any]]:
        """Fetch the user's most recent liked tweets via the X API v2."""
        path = (
            f"/2/users/{twitter_user_id}/liked_tweets"
            f"?max_results={max_results}"
            f"&tweet.fields=text,entities,created_at,author_id"
        )
        result = await self._xurl(path)
        if result and "data" in result:
            tweets = result["data"]
            logger.debug(f"Fetched {len(tweets)} liked tweets for Twitter ID {twitter_user_id}")
            return tweets
        logger.debug(f"No liked tweets returned for Twitter ID {twitter_user_id}: {result}")
        return []

    # -----------------------------------------------------------------------
    # Topic & interest extraction
    # -----------------------------------------------------------------------

    async def _extract_topics(self, tweets: List[Dict[str, Any]]) -> List[str]:
        """
        Extract 5-10 concise topic tags from tweet texts.
        Tries OpenAI gpt-4o-mini first; falls back to simple keyword frequency.
        """
        if not tweets:
            return []

        texts = [t.get("text", "") for t in tweets[:25] if t.get("text")]
        combined = "\n".join(texts)[:3500]  # stay within token budget

        if self._openai:
            try:
                from core.config import settings
                if settings.has_openai:
                    resp = await self._openai.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "Extract 5-10 concise, lowercase topic tags from the tweets below. "
                                    "Focus on themes, not specific people or events. "
                                    'Return JSON: {"topics": ["tag1", "tag2", ...]}'
                                ),
                            },
                            {"role": "user", "content": combined},
                        ],
                        temperature=0.2,
                        max_tokens=200,
                        response_format={"type": "json_object"},
                    )
                    data = json.loads(resp.choices[0].message.content)
                    topics = data.get("topics", data.get("tags", []))
                    if isinstance(topics, list) and topics:
                        return [str(t).lower().strip() for t in topics[:10]]
            except Exception as e:
                logger.debug(f"OpenAI topic extraction failed: {e}")

        # Keyword frequency fallback
        words = re.findall(r"\b[A-Za-z]{4,}\b", combined)
        _stop = {
            "this", "that", "with", "from", "have", "been", "will", "they", "their",
            "what", "when", "where", "just", "like", "more", "some", "your", "about",
            "very", "also", "into", "then", "than", "over", "said", "here", "there",
        }
        freq: Dict[str, int] = {}
        for w in words:
            wl = w.lower()
            if wl not in _stop:
                freq[wl] = freq.get(wl, 0) + 1
        return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:10]]

    @staticmethod
    def _infer_interests(topics: List[str]) -> List[str]:
        """Map raw topic tags to broad interest categories."""
        topic_text = " ".join(topics).lower()
        found = []
        for interest, keywords in _INTEREST_MAP.items():
            if any(kw in topic_text for kw in keywords):
                found.append(interest)
        return found

    # -----------------------------------------------------------------------
    # Main ingestion pipeline
    # -----------------------------------------------------------------------

    async def ingest_twitter_signals(
        self, user_id: str, twitter_handle: str
    ) -> Dict[str, Any]:
        """
        Full ingestion pipeline:
          1. Resolve handle → Twitter user ID
          2. Fetch liked tweets
          3. Extract topics + infer interests
          4. Persist to Redis + update DB user profile
          5. Return summary dict

        Returns: {"interests_found": [...], "topics": [...], "signal_count": N}
        """
        logger.info(
            f"TwitterSignalAgent: starting ingestion for user={user_id[:8]}, handle=@{twitter_handle}"
        )

        # Store "in_progress" status immediately so the status endpoint can respond
        try:
            from core.redis_client import get_redis
            _r = await get_redis()
            await _r.set(
                f"user:{user_id}:twitter_ingestion_status",
                json.dumps({"status": "in_progress", "started_at": datetime.datetime.utcnow().isoformat()}),
                ex=3600,
            )
        except Exception:
            pass

        # Step 1: Resolve Twitter user ID
        twitter_user_id = await self._get_twitter_user_id(twitter_handle)
        if not twitter_user_id:
            error_payload = {
                "error": f"Could not resolve Twitter handle @{twitter_handle}",
                "signal_count": 0,
            }
            await self._store_status(user_id, "error", error_payload)
            return error_payload

        # Step 2: Fetch liked tweets
        tweets = await self._get_liked_tweets(twitter_user_id)
        if not tweets:
            error_payload = {
                "error": "No liked tweets found (private account or no likes)",
                "signal_count": 0,
                "twitter_user_id": twitter_user_id,
            }
            await self._store_status(user_id, "error", error_payload)
            return error_payload

        # Step 3: Extract topics + interests
        topics = await self._extract_topics(tweets)
        interests_found = self._infer_interests(topics)

        # Step 4: Build signals payload and store in Redis
        signals_payload = {
            "twitter_handle": twitter_handle.lstrip("@"),
            "twitter_user_id": twitter_user_id,
            "topics": topics,
            "interests_found": interests_found,
            "signal_count": len(tweets),
            "ingested_at": datetime.datetime.utcnow().isoformat(),
        }

        try:
            from core.redis_client import get_redis
            r = await get_redis()
            # Primary signals key (7-day TTL)
            await r.set(
                f"user:{user_id}:twitter_signals",
                json.dumps(signals_payload),
                ex=7 * 24 * 3600,
            )
        except Exception as e:
            logger.warning(f"TwitterSignalAgent: Redis store failed: {e}")

        await self._store_status(user_id, "complete", {
            "signal_count": len(tweets),
            "completed_at": signals_payload["ingested_at"],
        })

        # Step 5: Update DB user profile with inferred interests
        await self._update_user_profile(user_id, interests_found, topics, twitter_handle)

        logger.info(
            f"TwitterSignalAgent: done user={user_id[:8]} "
            f"tweets={len(tweets)} topics={len(topics)} interests={interests_found}"
        )

        return {
            "interests_found": interests_found,
            "topics": topics,
            "signal_count": len(tweets),
            "twitter_handle": twitter_handle.lstrip("@"),
        }

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _store_status(
        self, user_id: str, status: str, extra: Dict[str, Any]
    ) -> None:
        """Persist ingestion status to Redis for the status endpoint."""
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            payload = {"status": status, **extra}
            await r.set(
                f"user:{user_id}:twitter_ingestion_status",
                json.dumps(payload),
                ex=7 * 24 * 3600,
            )
        except Exception as e:
            logger.debug(f"_store_status failed: {e}")

    async def _update_user_profile(
        self,
        user_id: str,
        interests_found: List[str],
        topics: List[str],
        twitter_handle: str,
    ) -> None:
        """Merge inferred interests into the user's profile JSONB and bust cache."""
        try:
            from core.database import execute, fetchrow
            from core.redis_client import redis_delete
            from uuid import UUID

            user_row = await fetchrow(
                "SELECT profile FROM users WHERE id = $1", UUID(user_id)
            )
            if not user_row:
                return

            raw = user_row["profile"] or {}
            profile = json.loads(raw) if isinstance(raw, str) else dict(raw)

            # Merge interests (unique, cap at 20)
            existing = profile.get("interests", [])
            merged = list(dict.fromkeys(existing + interests_found))[:20]
            profile["interests"] = merged
            profile["twitter_handle"] = twitter_handle.lstrip("@")
            profile["twitter_topics"] = topics

            await execute(
                "UPDATE users SET profile = $1::jsonb WHERE id = $2",
                json.dumps(profile),
                UUID(user_id),
            )
            await redis_delete(f"user_model:{user_id}")
            logger.debug(f"TwitterSignalAgent: updated profile for user={user_id[:8]}")
        except Exception as e:
            logger.warning(f"TwitterSignalAgent: profile update failed: {e}")
