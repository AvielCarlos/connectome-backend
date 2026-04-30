"""
FeedbackAnalystAgent
Processes real-time feedback signals and generates insight summaries.
Called by the brain after each feedback submission.

Also handles:
  - Exit intent classification (Feature 1)
  - Improvement loop trigger (Feature 3)
"""

import json
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
from uuid import UUID

from core.database import fetch, fetchval, fetchrow, execute
from core.redis_client import redis_publish, get_redis

logger = logging.getLogger(__name__)


class FeedbackAnalystAgent:
    """
    Analyzes feedback patterns and emits real-time signals.
    Does NOT generate screens — produces insights consumed by the brain.
    """

    AGENT_NAME = "FeedbackAnalystAgent"

    def __init__(self, openai_client=None, ui_generator=None):
        self.openai = openai_client
        self.ui_generator = ui_generator

    async def process_feedback(
        self,
        user_id: str,
        screen_spec_id: str,
        rating: int,
        time_on_screen_ms: int,
        agent_type: str,
        completed: bool,
    ) -> Dict[str, Any]:
        """
        Process a feedback event and return insight summary.
        Also publishes a Redis signal for any real-time consumers.
        """
        insight = await self._build_insight(
            user_id, screen_spec_id, rating, time_on_screen_ms, agent_type, completed
        )

        # Publish to Redis for real-time consumers (analytics dashboard, etc.)
        await redis_publish(
            "ora:feedback",
            {
                "user_id": user_id,
                "screen_spec_id": screen_spec_id,
                "rating": rating,
                "agent_type": agent_type,
                "completed": completed,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )

        return insight

    async def _build_insight(
        self,
        user_id: str,
        screen_spec_id: str,
        rating: int,
        time_on_screen_ms: int,
        agent_type: str,
        completed: bool,
    ) -> Dict[str, Any]:
        """Build a compact insight dict from the feedback event."""
        # Compute fulfilment delta from rating
        fulfilment_delta = (rating - 3.0) / 10.0  # small signal

        # Classify the signal
        if rating >= 4:
            signal_type = "positive"
            action = "boost"
        elif rating <= 2:
            signal_type = "negative"
            action = "deprioritize"
        else:
            signal_type = "neutral"
            action = "maintain"

        # Compute engagement score (time-normalized)
        engagement_score = 0.5
        if time_on_screen_ms:
            # >30s = highly engaged, <5s = skimmed
            engagement_score = min(1.0, time_on_screen_ms / 30000)

        return {
            "signal_type": signal_type,
            "action": action,
            "fulfilment_delta": fulfilment_delta,
            "engagement_score": engagement_score,
            "completed": completed,
            "agent_type": agent_type,
            "should_generate_variation": rating >= 4 and completed,
        }

    # -----------------------------------------------------------------------
    # Feature 1: Exit Intent Classification
    # -----------------------------------------------------------------------

    async def classify_exit_intent(
        self,
        user_id: str,
        interaction_id: str,
        screen_spec_id: str,
        exit_point: Optional[str],
        time_on_screen_ms: int,
    ) -> Dict[str, Any]:
        """
        Classify why a user exited a screen. Stores in exit_classifications table
        and feeds the signal back to the user model.

        Anti-hallucination safeguards applied:
          1. Evidence thresholds — confidence capped based on interaction history count
          2. Consistency validation — checked against session summaries + prior exits
        """
        # ----------------------------------------------------------------
        # Safeguard 1: Count total interactions for this user (evidence threshold)
        # ----------------------------------------------------------------
        total_interactions: int = await fetchval(
            "SELECT COUNT(*) FROM interactions WHERE user_id = $1",
            UUID(user_id),
        ) or 0

        # Fetch last 5 interactions as user history
        history_rows = await fetch(
            """
            SELECT i.rating, i.exit_point, i.completed, i.time_on_screen_ms,
                   s.agent_type, s.spec
            FROM interactions i
            LEFT JOIN screen_specs s ON s.id = i.screen_spec_id
            WHERE i.user_id = $1
            ORDER BY i.created_at DESC LIMIT 5
            """,
            UUID(user_id),
        )
        user_history = [dict(r) for r in history_rows]

        # Fetch the screen spec that was exited
        spec_row = await fetchrow(
            "SELECT spec, agent_type FROM screen_specs WHERE id = $1",
            UUID(screen_spec_id),
        )
        screen_spec = dict(spec_row) if spec_row else {}
        agent_type = screen_spec.get("agent_type", "unknown")

        # Build classification via LLM or mock
        from core.config import settings
        if self.openai and settings.has_openai:
            classification = await self._classify_with_llm(
                user_history, screen_spec, exit_point, time_on_screen_ms
            )
        else:
            classification = self._classify_mock(time_on_screen_ms)

        # ----------------------------------------------------------------
        # Apply evidence threshold caps (Safeguard 1)
        # ----------------------------------------------------------------
        if total_interactions < 5:
            classification["confidence"] = min(classification["confidence"], 0.3)
            classification["category"] = "insufficient_data"
            classification["reason"] = "Not enough user history to classify reliably"
        elif total_interactions < 15:
            classification["confidence"] = min(classification["confidence"], 0.6)
            if "(early inference" not in classification["reason"]:
                classification["reason"] = (
                    classification["reason"] + " (early inference — treat as tentative)"
                )
        # else: 15+ interactions — full confidence allowed

        # ----------------------------------------------------------------
        # Safeguard 2: Consistency validation
        # ----------------------------------------------------------------
        classification = await self.validate_classification(
            user_id=user_id,
            classification=classification,
        )

        # Store in exit_classifications (with new safeguard columns)
        await execute(
            """
            INSERT INTO exit_classifications
                (interaction_id, user_id, screen_spec_id, reason, category,
                 confidence, suggested_improvement,
                 data_points_at_classification, consistency_flagged, consistency_note)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            UUID(interaction_id),
            UUID(user_id),
            UUID(screen_spec_id),
            classification["reason"],
            classification["category"],
            classification["confidence"],
            classification["suggested_improvement"],
            total_interactions,
            classification.get("consistency_flagged", False),
            classification.get("consistency_note"),
        )

        # Feed back to user embedding: map category to a rating signal
        CATEGORY_RATING = {
            "content_mismatch": 1,
            "offer_failed": 1,
            "attention_lost": 2,
            "timing": 2,
            "unknown": None,
            "insufficient_data": None,
        }
        feedback_rating = CATEGORY_RATING.get(classification["category"])
        if feedback_rating and agent_type:
            from ora.user_model import update_user_embedding
            await update_user_embedding(user_id, feedback_rating, agent_type)

        # Trigger improvement loop check (fire-and-forget, don't block)
        try:
            await self.trigger_improvement_loop(
                screen_spec_id=screen_spec_id,
                suggested_improvement=classification["suggested_improvement"],
            )
        except Exception as e:
            logger.warning(f"Improvement loop trigger failed: {e}")

        logger.debug(
            f"Exit classified: user={user_id[:8]} spec={screen_spec_id[:8]} "
            f"category={classification['category']} conf={classification['confidence']:.2f} "
            f"data_points={total_interactions} consistency_flagged={classification.get('consistency_flagged', False)}"
        )
        return classification

    # -----------------------------------------------------------------------
    # Safeguard 2: Consistency Validation
    # -----------------------------------------------------------------------

    async def validate_classification(
        self,
        user_id: str,
        classification: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Cross-check a freshly generated classification against the user's
        session history and recent exit patterns.

        If the classification contradicts known user preferences (avoid_topics)
        or recent exit patterns, confidence is downgraded and the record is
        flagged for review.

        Returns the (potentially modified) classification dict.
        """
        # Load last 3 session summaries
        summary_rows = await fetch(
            """
            SELECT emerging_interests, avoid_topics
            FROM session_summaries
            WHERE user_id = $1
            ORDER BY created_at DESC LIMIT 3
            """,
            UUID(user_id),
        )

        if not summary_rows:
            # Not enough history to validate — skip
            classification.setdefault("consistency_flagged", False)
            classification.setdefault("consistency_note", None)
            return classification

        # Build context lists
        emerging_interests: List[str] = []
        avoid_topics: List[str] = []
        for row in summary_rows:
            raw_interests = row["emerging_interests"]
            raw_avoid = row["avoid_topics"]
            if isinstance(raw_interests, str):
                import json as _json
                raw_interests = _json.loads(raw_interests)
            if isinstance(raw_avoid, str):
                import json as _json
                raw_avoid = _json.loads(raw_avoid)
            emerging_interests.extend(raw_interests or [])
            avoid_topics.extend(raw_avoid or [])

        # Deduplicate
        emerging_interests = list(dict.fromkeys(emerging_interests))
        avoid_topics = list(dict.fromkeys(avoid_topics))

        # Load last 10 exit classifications for pattern context
        exit_rows = await fetch(
            """
            SELECT category, reason
            FROM exit_classifications
            WHERE user_id = $1
            ORDER BY created_at DESC LIMIT 10
            """,
            UUID(user_id),
        )
        recent_exits = [{"category": r["category"], "reason": r["reason"]} for r in exit_rows]

        # ---------------------------------------------------------------
        # Consistency check: LLM path or mock
        # ---------------------------------------------------------------
        from core.config import settings
        if self.openai and settings.has_openai:
            result = await self._consistency_check_llm(
                classification=classification,
                emerging_interests=emerging_interests,
                avoid_topics=avoid_topics,
                recent_exits=recent_exits,
            )
        else:
            result = self._consistency_check_mock(
                classification=classification,
                avoid_topics=avoid_topics,
            )

        # Apply result
        if not result.get("consistent", True):
            note = result.get("note", "Consistency check flagged this classification")
            classification["confidence"] = max(
                0.0, classification["confidence"] - 0.3
            )
            classification["reason"] = classification["reason"] + f" [FLAGGED: {note}]"
            classification["consistency_flagged"] = True
            classification["consistency_note"] = note
            logger.info(
                f"Consistency flag for user={user_id[:8]}: {note} "
                f"(confidence now {classification['confidence']:.2f})"
            )
        else:
            classification["consistency_flagged"] = False
            classification["consistency_note"] = result.get("note")

        return classification

    async def _consistency_check_llm(
        self,
        classification: Dict[str, Any],
        emerging_interests: List[str],
        avoid_topics: List[str],
        recent_exits: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """LLM-based consistency check."""
        prompt = (
            f"Given this user's known interests {emerging_interests} and topics to avoid "
            f"{avoid_topics}, and their recent exit patterns {json.dumps(recent_exits[:5])}, "
            f"does this new classification make sense: '{classification.get('reason')}'? "
            f"Answer JSON: {{\"consistent\": true/false, \"adjusted_confidence\": float, \"note\": string}}"
        )
        try:
            response = await self.openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=100,
                response_format={"type": "json_object"},
            )
            data = json.loads(response.choices[0].message.content)
            return {
                "consistent": bool(data.get("consistent", True)),
                "note": data.get("note", ""),
            }
        except Exception as e:
            logger.warning(f"LLM consistency check failed: {e}")
            return {"consistent": True, "note": ""}

    @staticmethod
    def _consistency_check_mock(
        classification: Dict[str, Any],
        avoid_topics: List[str],
    ) -> Dict[str, Any]:
        """
        Mock consistency check: flag if the classification's category or reason
        string-matches any known avoid_topic.
        """
        category = (classification.get("category") or "").lower()
        reason = (classification.get("reason") or "").lower()

        for topic in avoid_topics:
            topic_lower = topic.lower()
            if topic_lower and (topic_lower in category or topic_lower in reason):
                return {
                    "consistent": False,
                    "note": f"Classification references avoided topic '{topic}'",
                }
        return {"consistent": True, "note": None}

    async def _classify_with_llm(
        self,
        user_history: List[Dict[str, Any]],
        screen_spec: Dict[str, Any],
        exit_point: Optional[str],
        time_on_screen_ms: int,
    ) -> Dict[str, Any]:
        """Use GPT-4o to classify exit intent."""
        history_summary = [
            {
                "rating": r.get("rating"),
                "exit_point": r.get("exit_point"),
                "completed": r.get("completed"),
                "time_ms": r.get("time_on_screen_ms"),
                "agent": r.get("agent_type"),
            }
            for r in user_history
        ]
        spec_summary = {
            "agent_type": screen_spec.get("agent_type"),
            "screen_type": (screen_spec.get("spec") or {}).get("type") if isinstance(screen_spec.get("spec"), dict) else None,
        }

        prompt = f"""You are Ora analyzing a user exit signal.

User's last 5 interactions: {json.dumps(history_summary)}
Screen they exited: {json.dumps(spec_summary)}
Exit point: {exit_point}
Time on screen (ms): {time_on_screen_ms}

Classify why the user left. Return ONLY valid JSON:
{{
  "reason": "concise explanation based on their history and the screen",
  "category": "content_mismatch" | "timing" | "offer_failed" | "attention_lost" | "unknown",
  "confidence": 0.0-1.0,
  "suggested_improvement": "specific suggestion to improve this screen for this user type"
}}"""

        try:
            response = await self.openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=200,
                response_format={"type": "json_object"},
            )
            data = json.loads(response.choices[0].message.content)
            # Validate category
            valid_cats = {"content_mismatch", "timing", "offer_failed", "attention_lost", "unknown"}
            if data.get("category") not in valid_cats:
                data["category"] = "unknown"
            return data
        except Exception as e:
            logger.warning(f"LLM exit classification failed: {e}")
            return self._classify_mock(time_on_screen_ms)

    @staticmethod
    def _classify_mock(time_on_screen_ms: int) -> Dict[str, Any]:
        """Intelligent mock classification based on time-on-screen."""
        if time_on_screen_ms < 3000:
            return {
                "reason": "User left too quickly to engage",
                "category": "timing",
                "confidence": 0.85,
                "suggested_improvement": "Open with a stronger hook in the first 2 seconds",
            }
        if time_on_screen_ms < 10000:
            return {
                "reason": "User lost interest mid-screen",
                "category": "attention_lost",
                "confidence": 0.70,
                "suggested_improvement": "Shorten content or add interactive elements earlier",
            }
        return {
            "reason": "Insufficient signal to determine exit reason",
            "category": "unknown",
            "confidence": 0.30,
            "suggested_improvement": "Gather more interaction data for this screen type",
        }

    # -----------------------------------------------------------------------
    # Feature 3: Improvement Loop
    # -----------------------------------------------------------------------

    async def trigger_improvement_loop(
        self,
        screen_spec_id: str,
        suggested_improvement: str,
    ) -> Optional[Dict[str, Any]]:
        """
        After every 10 new exit classifications for a screen, check if an
        improvement A/B test should be created. Triggers when >3 exits are
        categorized as content_mismatch or offer_failed.
        """
        r = await get_redis()
        redis_key = f"exit_count:{screen_spec_id}"

        # Increment and check the count
        count = await r.incr(redis_key)
        if count == 1:
            await r.expire(redis_key, 86400 * 7)  # expire in 7 days

        # Only run the loop check every 10 exits
        if count % 10 != 0:
            return None

        # Query DB for problematic exit categories on this screen
        # Safeguard 1: Only act on classifications with sufficient evidence
        rows = await fetch(
            """
            SELECT category, suggested_improvement
            FROM exit_classifications
            WHERE screen_spec_id = $1
              AND category IN ('content_mismatch', 'offer_failed')
              AND confidence >= 0.5
              AND data_points_at_classification >= 5
            ORDER BY created_at DESC LIMIT 20
            """,
            UUID(screen_spec_id),
        )

        if len(rows) <= 3:
            logger.debug(
                f"Improvement loop: spec={screen_spec_id[:8]} only {len(rows)} problematic exits — skipping"
            )
            return None

        # Gather improvement suggestions from recent exits
        suggestions = [r["suggested_improvement"] for r in rows if r["suggested_improvement"]]
        combined_hint = "; ".join(suggestions[:3]) if suggestions else suggested_improvement

        logger.info(
            f"Improvement loop triggered for spec={screen_spec_id[:8]}: "
            f"{len(rows)} problem exits. Hint: {combined_hint[:80]}"
        )

        if not self.ui_generator:
            logger.warning("Improvement loop: no ui_generator available — skipping generation")
            return None

        # Fetch original spec to get screen_type
        original_row = await fetchrow(
            "SELECT spec, agent_type FROM screen_specs WHERE id = $1",
            UUID(screen_spec_id),
        )
        if not original_row:
            return None

        original_spec = original_row["spec"] or {}
        screen_type = original_spec.get("type", "summary") if isinstance(original_spec, dict) else "summary"

        # Generate improved screen spec with hint injected into user_context
        improvement_context = {
            "user_id": "improvement_loop",
            "subscription_tier": "free",
            "fulfilment_score": 0.5,
            "interests": [],
            "display_name": "",
            "active_goals": [],
            "recent_ratings": [],
            "improvement_hint": combined_hint,  # picked up by UIGeneratorAgent prompt
        }

        try:
            new_spec = await self.ui_generator.generate_screen(
                improvement_context,
                screen_type=screen_type,
                variant="improved",
            )
        except Exception as e:
            logger.warning(f"Improvement loop: UI generation failed: {e}")
            return None

        # Store the new spec
        new_spec_row = await fetchrow(
            """
            INSERT INTO screen_specs (spec, agent_type)
            VALUES ($1, $2)
            RETURNING id
            """,
            json.dumps(new_spec),
            original_row["agent_type"],
        )
        new_spec_id = str(new_spec_row["id"])

        # Create A/B test between original and improved
        test_name = f"improvement_{screen_spec_id[:12]}"
        from ora.ab_testing import get_or_create_test
        await get_or_create_test(
            name=test_name,
            variants=["original", "improved"],
        )
        # Record the improved spec id in AB test metadata
        await execute(
            """
            UPDATE ab_tests
            SET variants = variants || $1::jsonb,
                status = 'running'
            WHERE name = $2
            """,
            json.dumps({"improved_spec_id": new_spec_id, "original_spec_id": screen_spec_id}),
            test_name,
        )

        logger.info(
            f"Improvement A/B test '{test_name}' created: "
            f"original={screen_spec_id[:8]} improved={new_spec_id[:8]}"
        )
        return {
            "test_name": test_name,
            "original_spec_id": screen_spec_id,
            "improved_spec_id": new_spec_id,
        }

    # -----------------------------------------------------------------------
    # Calibration Tracking
    # -----------------------------------------------------------------------

    async def get_calibration_stats(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Compute calibration metrics comparing Ora's exit classifications against
        ground-truth labels provided by users.

        Optionally scoped to a single user; if user_id is None, computes globally.
        """
        # Scope filter for queries
        user_filter = "AND ec.user_id = $1" if user_id else ""
        params = [UUID(user_id)] if user_id else []

        # Total classifications
        total_classifications: int = await fetchval(
            f"SELECT COUNT(*) FROM exit_classifications ec WHERE 1=1 {user_filter}",
            *params,
        ) or 0

        # Total ground-truth labels
        gt_filter = "AND gt.user_id = $1" if user_id else ""
        ground_truth_count: int = await fetchval(
            f"SELECT COUNT(*) FROM ground_truth_labels gt WHERE 1=1 {gt_filter}",
            *params,
        ) or 0

        if ground_truth_count == 0:
            return {
                "total_classifications": total_classifications,
                "ground_truth_labels": 0,
                "accuracy_rate": None,
                "most_common_miscategory": None,
                "confidence_calibration": "insufficient_data",
            }

        # Map user answers to category names (mirrors ground_truth.py mapping)
        ANSWER_TO_CATEGORY = {
            "too_long": "attention_lost",
            "not_interesting": "content_mismatch",
            "wrong_topic": "content_mismatch",
            "just_browsing": "unknown",
            "other": "unknown",
        }

        # Fetch all ground truth comparisons
        gt_rows = await fetch(
            f"""
            SELECT gt.user_answer, ec.category, ec.confidence
            FROM ground_truth_labels gt
            JOIN exit_classifications ec ON ec.id = gt.exit_classification_id
            WHERE 1=1 {gt_filter}
            """,
            *params,
        )

        if not gt_rows:
            return {
                "total_classifications": total_classifications,
                "ground_truth_labels": ground_truth_count,
                "accuracy_rate": 0.0,
                "most_common_miscategory": None,
                "confidence_calibration": "insufficient_data",
            }

        correct = []
        incorrect = []
        miscategories: Dict[str, int] = {}

        for row in gt_rows:
            user_answer = row["user_answer"] or "other"
            aura_category = row["category"] or "unknown"
            expected_category = ANSWER_TO_CATEGORY.get(user_answer, "unknown")
            confidence = float(row["confidence"] or 0.0)

            if expected_category == aura_category:
                correct.append(confidence)
            else:
                incorrect.append(confidence)
                key = f"{ora_category} → actually {user_answer}"
                miscategories[key] = miscategories.get(key, 0) + 1

        total_labeled = len(correct) + len(incorrect)
        accuracy_rate = len(correct) / total_labeled if total_labeled > 0 else 0.0

        most_common_miscategory = (
            max(miscategories, key=lambda k: miscategories[k])
            if miscategories else None
        )

        # Confidence calibration
        avg_wrong_conf = (
            sum(incorrect) / len(incorrect) if incorrect else 0.0
        )
        avg_correct_conf = (
            sum(correct) / len(correct) if correct else 1.0
        )
        if avg_wrong_conf > 0.6:
            calibration = "overconfident"
        elif avg_correct_conf < 0.5:
            calibration = "underconfident"
        else:
            calibration = "well_calibrated"

        return {
            "total_classifications": total_classifications,
            "ground_truth_labels": ground_truth_count,
            "accuracy_rate": round(accuracy_rate, 4),
            "most_common_miscategory": most_common_miscategory,
            "confidence_calibration": calibration,
        }

    async def get_user_trend(self, user_id: str) -> Dict[str, Any]:
        """Compute recent trend metrics for a user."""
        from uuid import UUID

        rows = await fetch(
            """
            SELECT i.rating, i.completed, i.time_on_screen_ms, s.agent_type
            FROM interactions i
            LEFT JOIN screen_specs s ON s.id = i.screen_spec_id
            WHERE i.user_id = $1 AND i.created_at > NOW() - INTERVAL '7 days'
            ORDER BY i.created_at DESC
            """,
            UUID(user_id),
        )

        if not rows:
            return {"trend": "new_user", "avg_rating": 0, "engagement": 0}

        ratings = [r["rating"] for r in rows if r["rating"]]
        completions = [r["completed"] for r in rows]
        times = [r["time_on_screen_ms"] for r in rows if r["time_on_screen_ms"]]

        avg_rating = sum(ratings) / len(ratings) if ratings else 0
        completion_rate = sum(completions) / len(completions) if completions else 0
        avg_time_ms = sum(times) / len(times) if times else 0

        # Determine trend direction
        if len(ratings) >= 5:
            recent = ratings[:5]
            older = ratings[5:] if len(ratings) > 5 else recent
            recent_avg = sum(recent) / len(recent)
            older_avg = sum(older) / len(older)
            if recent_avg > older_avg + 0.3:
                trend = "improving"
            elif recent_avg < older_avg - 0.3:
                trend = "declining"
            else:
                trend = "stable"
        else:
            trend = "early"

        # Agent preference
        agent_ratings: Dict[str, list] = {}
        for r in rows:
            at = r.get("agent_type", "unknown")
            if r["rating"]:
                agent_ratings.setdefault(at, []).append(r["rating"])

        preferred_agent = max(
            agent_ratings,
            key=lambda a: sum(agent_ratings[a]) / len(agent_ratings[a]),
            default=None,
        ) if agent_ratings else None

        return {
            "trend": trend,
            "avg_rating": round(avg_rating, 2),
            "completion_rate": round(completion_rate, 2),
            "avg_time_ms": int(avg_time_ms),
            "sample_size": len(rows),
            "preferred_agent": preferred_agent,
        }
