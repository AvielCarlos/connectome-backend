"""
FeedbackExperimenter — Ora's Meta-Learning Agent
Autonomously designs, deploys, and evaluates new feedback collection methods.
Feedback mechanisms are themselves A/B tested — Ora learns which ways of asking
produce the most signal. Results are stored as lessons in ora_lessons, feeding
back into every future screen decision.
"""

import asyncio
import hashlib
import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from core.config import settings
from core.database import execute, fetch, fetchrow, fetchval

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feedback mechanism catalogue
# ---------------------------------------------------------------------------

FEEDBACK_MECHANISM_TYPES: Dict[str, str] = {
    "star_rating":        "Classic 1-5 stars — baseline",
    "emoji_reaction":     "Single tap: 😴🙂😊🤩 — lower friction than stars",
    "binary_swipe":       "Swipe right=loved it, left=not for me — zero UI",
    "voice_note":         "30-second voice memo — richest signal, highest friction",
    "one_word":           "Type one word that describes how this made you feel",
    "before_after":       "Rate your mood before and after — measures impact",
    "share_intent":       "Did you share this / want to? — action as signal",
    "goal_link":          "Did this connect to one of your goals? — relevance signal",
    "completion_pulse":   "Did you actually do the thing shown? — outcome signal",
    "micro_poll":         "Single yes/no question Ora generates per screen",
    "implicit_scroll":    "How far they scroll = engagement proxy (no UI needed)",
    "return_signal":      "Did they come back to this screen later? — delayed signal",
}

IMPLICIT_MECHANISMS = {"implicit_scroll", "return_signal"}

# Low-quality threshold: if avg normalized score < this after MIN_SIGNALS, auto-pause
AUTO_PAUSE_THRESHOLD = 1.5 / 4.0   # maps to ~0.375 in 0-1 space
AUTO_PAUSE_MIN_SIGNALS = 20
MAX_CONCURRENT_EXPERIMENTS = 3
SIGNIFICANCE_THRESHOLD = 0.05       # p < 0.05 to declare winner


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class FeedbackExperiment:
    id: str
    hypothesis: str
    mechanism_type: str
    control_mechanism: str
    screen_types: List[str]
    status: str
    sample_size_target: int
    duration_days: int
    started_at: datetime
    control_count: int = 0
    treatment_count: int = 0
    control_response_rate: float = 0.0
    treatment_response_rate: float = 0.0
    control_signal_quality: float = 0.0
    treatment_signal_quality: float = 0.0
    p_value: Optional[float] = None
    winner: Optional[str] = None
    summary: Optional[str] = None
    completed_at: Optional[datetime] = None


@dataclass
class ExperimentResult:
    experiment_id: str
    winner: str                # "control" | "treatment" | "inconclusive"
    p_value: float
    control_response_rate: float
    treatment_response_rate: float
    control_signal_quality: float
    treatment_signal_quality: float
    summary: str
    promoted: bool = False


# ---------------------------------------------------------------------------
# Main agent
# ---------------------------------------------------------------------------

class FeedbackExperimenter:
    """
    Ora's meta-learning agent.
    Autonomously designs new ways to collect feedback, deploys them as
    screen components, measures their effectiveness, and promotes winners.

    Feedback mechanisms are themselves A/B tested — Ora learns which
    ways of asking produce the most signal.
    """

    def __init__(self, openai_client=None):
        self._openai = openai_client
        self._running = False

    # -----------------------------------------------------------------------
    # 1. Design experiment
    # -----------------------------------------------------------------------

    async def design_experiment(self, hypothesis: str) -> FeedbackExperiment:
        """
        Design a proper A/B experiment from a hypothesis string.
        Uses LLM to extract mechanism type and screen types; falls back to mock.
        Stores in feedback_experiments table.
        """
        design = await self._design_via_llm(hypothesis)

        mechanism_type = design.get("mechanism_type", "emoji_reaction")
        control_mechanism = design.get("control_mechanism", "star_rating")
        screen_types = design.get("screen_types", ["discovery_card"])
        duration_days = max(3, min(14, int(design.get("duration_days", 7))))
        sample_size_target = max(100, int(design.get("sample_size_target", 100)))

        # Validate mechanism type
        if mechanism_type not in FEEDBACK_MECHANISM_TYPES:
            mechanism_type = "emoji_reaction"

        row = await fetchrow(
            """
            INSERT INTO feedback_experiments
                (hypothesis, mechanism_type, control_mechanism, screen_types,
                 duration_days, sample_size_target)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
            """,
            hypothesis,
            mechanism_type,
            control_mechanism,
            json.dumps(screen_types),
            duration_days,
            sample_size_target,
        )

        logger.info(
            f"FeedbackExperimenter: experiment designed | "
            f"id={str(row['id'])[:8]} mechanism={mechanism_type}"
        )

        return self._row_to_experiment(row)

    async def _design_via_llm(self, hypothesis: str) -> Dict[str, Any]:
        """Use LLM to extract experiment design from hypothesis. Falls back to mock."""
        if not self._openai or not settings.has_openai:
            return self._design_mock(hypothesis)

        prompt = f"""You are Ora's experiment designer. A hypothesis has been provided.
Extract the experiment design as JSON with these fields:
- mechanism_type: one of {list(FEEDBACK_MECHANISM_TYPES.keys())}
- control_mechanism: typically "star_rating"
- screen_types: list of screen type strings the experiment targets (e.g. ["discovery_card"])
- duration_days: int between 3 and 14
- sample_size_target: int, minimum 100 per variant

Hypothesis: "{hypothesis}"

Return ONLY valid JSON."""

        try:
            response = await self._openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=200,
                response_format={"type": "json_object"},
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.warning(f"FeedbackExperimenter: LLM design failed ({e}), using mock")
            return self._design_mock(hypothesis)

    @staticmethod
    def _design_mock(hypothesis: str) -> Dict[str, Any]:
        """Heuristic design extraction from hypothesis text."""
        hyp_lower = hypothesis.lower()
        mechanism_type = "emoji_reaction"
        for mtype in FEEDBACK_MECHANISM_TYPES:
            if mtype.replace("_", " ") in hyp_lower:
                mechanism_type = mtype
                break

        screen_types = ["discovery_card"]
        for stype in ["discovery_card", "coaching_card", "opportunity_card", "recommendation_card"]:
            if stype.replace("_", " ") in hyp_lower:
                screen_types = [stype]
                break

        return {
            "mechanism_type": mechanism_type,
            "control_mechanism": "star_rating",
            "screen_types": screen_types,
            "duration_days": 7,
            "sample_size_target": 100,
        }

    # -----------------------------------------------------------------------
    # 2. Generate hypothesis
    # -----------------------------------------------------------------------

    async def generate_hypothesis(self) -> str:
        """
        Ora generates her own experiment hypothesis by analyzing:
        - Current feedback response rates per mechanism
        - Screens with low signal
        - User segments that rarely give feedback
        Uses LLM; falls back to heuristic.
        """
        analytics = await self._gather_signal_analytics()
        hypothesis = await self._hypothesize_via_llm(analytics)
        logger.info(f"FeedbackExperimenter: generated hypothesis: {hypothesis[:80]}…")
        return hypothesis

    async def _gather_signal_analytics(self) -> Dict[str, Any]:
        """Aggregate signal stats for hypothesis generation."""
        try:
            # Response rates per mechanism from recent experiments
            rows = await fetch(
                """
                SELECT mechanism_type,
                       AVG(treatment_response_rate) as avg_response,
                       COUNT(*) as experiments
                FROM feedback_experiments
                WHERE status = 'completed'
                GROUP BY mechanism_type
                ORDER BY avg_response DESC
                """
            )
            mechanism_stats = [
                {
                    "mechanism": r["mechanism_type"],
                    "avg_response_rate": round(float(r["avg_response"] or 0), 3),
                    "experiments": int(r["experiments"]),
                }
                for r in rows
            ]

            # Interaction counts to assess overall feedback rate
            total_interactions = await fetchval("SELECT COUNT(*) FROM interactions")
            rated_interactions = await fetchval(
                "SELECT COUNT(*) FROM interactions WHERE rating IS NOT NULL"
            )
            overall_rate = (
                float(rated_interactions) / float(total_interactions)
                if total_interactions and total_interactions > 0
                else 0.0
            )

            return {
                "mechanism_stats": mechanism_stats,
                "overall_response_rate": round(overall_rate, 3),
                "total_interactions": int(total_interactions or 0),
                "least_used_mechanisms": [
                    m for m in FEEDBACK_MECHANISM_TYPES
                    if m not in [s["mechanism"] for s in mechanism_stats]
                    and m != "star_rating"
                ][:3],
            }
        except Exception as e:
            logger.warning(f"FeedbackExperimenter: analytics gather failed ({e})")
            return {
                "mechanism_stats": [],
                "overall_response_rate": 0.25,
                "total_interactions": 0,
                "least_used_mechanisms": ["emoji_reaction", "binary_swipe", "micro_poll"],
            }

    async def _hypothesize_via_llm(self, analytics: Dict[str, Any]) -> str:
        """Generate a hypothesis using LLM or heuristic fallback."""
        if not self._openai or not settings.has_openai:
            return self._hypothesize_mock(analytics)

        prompt = f"""You are Ora, an AI that improves human fulfilment.
Analyze these feedback analytics and propose ONE testable experiment hypothesis.

Analytics: {json.dumps(analytics, indent=2)}

Write a single clear hypothesis like:
"[mechanism_type] will achieve higher response rates than [control] on [screen_type] screens because [reason]"

Return ONLY the hypothesis string, no JSON."""

        try:
            response = await self._openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=100,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.warning(f"FeedbackExperimenter: hypothesis LLM failed ({e})")
            return self._hypothesize_mock(analytics)

    @staticmethod
    def _hypothesize_mock(analytics: Dict[str, Any]) -> str:
        """Heuristic hypothesis generation."""
        unused = analytics.get("least_used_mechanisms", ["emoji_reaction"])
        mechanism = unused[0] if unused else "emoji_reaction"
        overall = analytics.get("overall_response_rate", 0.25)

        if overall < 0.30:
            return (
                f"{mechanism} will achieve higher response rates than star_rating on "
                f"discovery_card screens due to lower interaction friction"
            )
        return (
            f"{mechanism} will produce higher signal quality than star_rating on "
            f"coaching_card screens because it captures emotional state more accurately"
        )

    # -----------------------------------------------------------------------
    # 3. Deploy experiment
    # -----------------------------------------------------------------------

    async def deploy_experiment(self, experiment: FeedbackExperiment):
        """
        Register the new feedback mechanism in the server-driven UI system.
        For implicit mechanisms, no UI component is added — just tracking metadata.
        """
        is_implicit = experiment.mechanism_type in IMPLICIT_MECHANISMS

        # Build the component spec that will be injected into screen specs
        if is_implicit:
            component_spec = {
                "type": "tracking_metadata",
                "tracking_type": experiment.mechanism_type,
                "experiment_id": experiment.id,
            }
        else:
            component_spec = self._build_component_spec(experiment)

        # Upsert the deployment record in ab_tests table (reuse existing infra)
        await execute(
            """
            INSERT INTO ab_tests (name, variants, status)
            VALUES ($1, $2, 'running')
            ON CONFLICT (name) DO UPDATE
              SET variants = EXCLUDED.variants, status = 'running'
            """,
            f"feedback_exp_{experiment.id[:8]}",
            json.dumps({
                "control": {
                    "mechanism_type": experiment.control_mechanism,
                    "component": {"type": f"feedback_{experiment.control_mechanism}"},
                },
                "treatment": {
                    "mechanism_type": experiment.mechanism_type,
                    "component": component_spec,
                },
                "screen_types": experiment.screen_types,
                "experiment_id": experiment.id,
            }),
        )

        logger.info(
            f"FeedbackExperimenter: experiment deployed | "
            f"id={experiment.id[:8]} type={'implicit' if is_implicit else 'ui'}"
        )

    @staticmethod
    def _build_component_spec(experiment: FeedbackExperiment) -> Dict[str, Any]:
        """Build the JSON component spec for a feedback mechanism."""
        mtype = experiment.mechanism_type
        base = {
            "experiment_id": experiment.id,
            "variant": "treatment",
            "position": "bottom_right",
            "always_visible": True,
        }

        if mtype == "emoji_reaction":
            return {**base, "type": "feedback_emoji", "options": ["😴", "🙂", "😊", "🤩"]}
        elif mtype == "binary_swipe":
            return {**base, "type": "feedback_binary"}
        elif mtype == "one_word":
            return {**base, "type": "feedback_one_word"}
        elif mtype == "micro_poll":
            return {**base, "type": "feedback_micro_poll", "question": "Did this connect to a goal?"}
        elif mtype == "completion_pulse":
            return {**base, "type": "feedback_completion_pulse", "delay_hours": 24}
        elif mtype == "before_after":
            return {**base, "type": "feedback_before_after"}
        elif mtype == "share_intent":
            return {**base, "type": "feedback_share_intent"}
        elif mtype == "goal_link":
            return {**base, "type": "feedback_goal_link"}
        elif mtype == "voice_note":
            return {**base, "type": "feedback_voice_note", "max_seconds": 30}
        else:
            return {**base, "type": f"feedback_{mtype}"}

    # -----------------------------------------------------------------------
    # 4. Collect experiment signal
    # -----------------------------------------------------------------------

    async def collect_experiment_signal(
        self,
        experiment_id: str,
        user_id: str,
        mechanism_type: str,
        raw_signal: Any,
        screen_spec_id: Optional[str] = None,
    ) -> float:
        """
        Normalize any signal type to 0.0-1.0 and store.
        Returns the normalized score.
        Also auto-pauses the experiment if quality drops below threshold.
        """
        normalized = self._normalize_signal(mechanism_type, raw_signal)

        # Determine variant from user assignment
        variant = self._assign_variant(user_id, experiment_id)

        # Store signal
        from uuid import UUID
        kwargs: Dict[str, Any] = {
            "experiment_id": uuid.UUID(experiment_id),
            "user_id": uuid.UUID(user_id),
            "variant": variant,
            "mechanism_type": mechanism_type,
            "raw_signal": json.dumps(raw_signal) if not isinstance(raw_signal, str) else raw_signal,
            "normalized_score": normalized,
        }
        if screen_spec_id:
            kwargs["screen_spec_id"] = uuid.UUID(screen_spec_id)
            await execute(
                """
                INSERT INTO experiment_signals
                    (experiment_id, user_id, screen_spec_id, variant,
                     mechanism_type, raw_signal, normalized_score)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                kwargs["experiment_id"], kwargs["user_id"], kwargs["screen_spec_id"],
                variant, mechanism_type,
                json.dumps(raw_signal) if not isinstance(raw_signal, str) else raw_signal,
                normalized,
            )
        else:
            await execute(
                """
                INSERT INTO experiment_signals
                    (experiment_id, user_id, variant,
                     mechanism_type, raw_signal, normalized_score)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                kwargs["experiment_id"], kwargs["user_id"],
                variant, mechanism_type,
                json.dumps(raw_signal) if not isinstance(raw_signal, str) else raw_signal,
                normalized,
            )

        # Update running stats on the experiment
        await self._update_experiment_stats(experiment_id, variant, normalized)

        # Check auto-pause
        await self._maybe_auto_pause(experiment_id)

        return normalized

    def _normalize_signal(self, mechanism_type: str, raw_signal: Any) -> float:
        """Normalize a raw signal to 0.0–1.0."""
        try:
            if mechanism_type == "star_rating":
                stars = float(raw_signal)
                return max(0.0, min(1.0, (stars - 1) / 4))
            elif mechanism_type == "emoji_reaction":
                idx = float(raw_signal)
                return max(0.0, min(1.0, idx / 3))
            elif mechanism_type == "binary_swipe":
                if isinstance(raw_signal, str):
                    return 1.0 if raw_signal.lower() == "right" else 0.0
                return float(raw_signal)
            elif mechanism_type == "voice_note":
                seconds = float(raw_signal) if not isinstance(raw_signal, dict) else float(raw_signal.get("length_seconds", 0))
                return max(0.0, min(1.0, seconds / 30))
            elif mechanism_type == "one_word":
                # LLM sentiment fallback to neutral
                return self._sentiment_score_mock(str(raw_signal))
            elif mechanism_type == "completion_pulse":
                val = str(raw_signal).lower()
                if val in ("yes", "1", "true"):
                    return 1.0
                elif val in ("not_yet", "not yet"):
                    return 0.5
                return 0.0
            elif mechanism_type == "implicit_scroll":
                pct = float(raw_signal) if not isinstance(raw_signal, dict) else float(raw_signal.get("scroll_pct", 0))
                return max(0.0, min(1.0, pct / 100))
            elif mechanism_type in ("before_after", "micro_poll", "share_intent", "goal_link", "return_signal"):
                return max(0.0, min(1.0, float(raw_signal)))
            else:
                return max(0.0, min(1.0, float(raw_signal)))
        except Exception:
            return 0.5  # neutral on error

    @staticmethod
    def _sentiment_score_mock(word: str) -> float:
        """Heuristic sentiment for one_word feedback."""
        positive = {"great", "amazing", "love", "wow", "good", "awesome", "perfect", "yes", "helpful", "insightful"}
        negative = {"bad", "boring", "meh", "no", "terrible", "awful", "useless", "skip", "tired", "waste"}
        word_lower = word.lower().strip()
        if word_lower in positive:
            return 0.9
        if word_lower in negative:
            return 0.1
        return 0.5

    @staticmethod
    def _assign_variant(user_id: str, experiment_id: str) -> str:
        """Deterministic variant assignment: hash(user_id + experiment_id) % 2."""
        key = f"{user_id}:{experiment_id}"
        hash_val = int(hashlib.md5(key.encode()).hexdigest(), 16)
        return "treatment" if hash_val % 2 == 0 else "control"

    async def _update_experiment_stats(
        self, experiment_id: str, variant: str, normalized_score: float
    ):
        """Incrementally update running counts and quality on the experiment row."""
        try:
            if variant == "treatment":
                await execute(
                    """
                    UPDATE feedback_experiments
                    SET treatment_count = treatment_count + 1,
                        treatment_signal_quality = (
                            treatment_signal_quality * treatment_count + $1
                        ) / GREATEST(treatment_count + 1, 1)
                    WHERE id = $2
                    """,
                    normalized_score,
                    uuid.UUID(experiment_id),
                )
            else:
                await execute(
                    """
                    UPDATE feedback_experiments
                    SET control_count = control_count + 1,
                        control_signal_quality = (
                            control_signal_quality * control_count + $1
                        ) / GREATEST(control_count + 1, 1)
                    WHERE id = $2
                    """,
                    normalized_score,
                    uuid.UUID(experiment_id),
                )
        except Exception as e:
            logger.warning(f"FeedbackExperimenter: stat update failed ({e})")

    async def _maybe_auto_pause(self, experiment_id: str):
        """
        Auto-pause an experiment if treatment quality is too low after
        AUTO_PAUSE_MIN_SIGNALS signals.
        """
        try:
            row = await fetchrow(
                "SELECT treatment_count, treatment_signal_quality, status FROM feedback_experiments WHERE id = $1",
                uuid.UUID(experiment_id),
            )
            if not row or row["status"] != "running":
                return
            if (
                row["treatment_count"] >= AUTO_PAUSE_MIN_SIGNALS
                and row["treatment_signal_quality"] < AUTO_PAUSE_THRESHOLD
            ):
                await execute(
                    "UPDATE feedback_experiments SET status = 'failed' WHERE id = $1",
                    uuid.UUID(experiment_id),
                )
                logger.warning(
                    f"FeedbackExperimenter: auto-paused experiment {experiment_id[:8]} "
                    f"(quality={row['treatment_signal_quality']:.3f} < {AUTO_PAUSE_THRESHOLD:.3f})"
                )
        except Exception as e:
            logger.warning(f"FeedbackExperimenter: auto-pause check failed ({e})")

    # -----------------------------------------------------------------------
    # 5. Evaluate experiment
    # -----------------------------------------------------------------------

    async def evaluate_experiment(self, experiment_id: str) -> ExperimentResult:
        """
        Evaluate a running experiment. Runs statistical analysis, determines winner,
        promotes winning mechanism, writes lesson to ora_lessons.
        """
        row = await fetchrow(
            "SELECT * FROM feedback_experiments WHERE id = $1",
            uuid.UUID(experiment_id),
        )
        if not row:
            raise ValueError(f"Experiment {experiment_id} not found")

        exp = self._row_to_experiment(row)

        # Compute response rates from actual signals
        ctrl_total, ctrl_responded = await self._get_variant_stats(experiment_id, "control")
        treat_total, treat_responded = await self._get_variant_stats(experiment_id, "treatment")

        ctrl_rate = ctrl_responded / ctrl_total if ctrl_total > 0 else 0.0
        treat_rate = treat_responded / treat_total if treat_total > 0 else 0.0

        # Signal quality
        ctrl_quality = float(row["control_signal_quality"] or 0.0)
        treat_quality = float(row["treatment_signal_quality"] or 0.0)

        # Statistical significance (z-test on proportions)
        p_value, significant = self._z_test_proportions(
            ctrl_responded, ctrl_total, treat_responded, treat_total
        )

        # Determine winner
        if not significant or (ctrl_total < 10 and treat_total < 10):
            winner = "inconclusive"
        elif treat_rate > ctrl_rate:
            winner = "treatment"
        else:
            winner = "control"

        # Generate summary
        summary = await self._generate_summary(
            exp, ctrl_rate, treat_rate, ctrl_quality, treat_quality, p_value, winner
        )

        promoted = False
        if winner == "treatment" and p_value < SIGNIFICANCE_THRESHOLD:
            await self._promote_mechanism(exp)
            promoted = True

        # Update experiment record
        now = datetime.now(timezone.utc)
        await execute(
            """
            UPDATE feedback_experiments
            SET status = 'completed',
                completed_at = $1,
                control_response_rate = $2,
                treatment_response_rate = $3,
                control_signal_quality = $4,
                treatment_signal_quality = $5,
                p_value = $6,
                winner = $7,
                summary = $8
            WHERE id = $9
            """,
            now,
            ctrl_rate,
            treat_rate,
            ctrl_quality,
            treat_quality,
            p_value,
            winner,
            summary,
            uuid.UUID(experiment_id),
        )

        # Write lesson to ora_lessons
        await self._write_lesson(exp, summary, winner, p_value, promoted)

        logger.info(
            f"FeedbackExperimenter: experiment {experiment_id[:8]} evaluated | "
            f"winner={winner} p={p_value:.3f} promoted={promoted}"
        )

        return ExperimentResult(
            experiment_id=experiment_id,
            winner=winner,
            p_value=p_value,
            control_response_rate=ctrl_rate,
            treatment_response_rate=treat_rate,
            control_signal_quality=ctrl_quality,
            treatment_signal_quality=treat_quality,
            summary=summary,
            promoted=promoted,
        )

    async def _get_variant_stats(
        self, experiment_id: str, variant: str
    ) -> Tuple[int, int]:
        """Returns (total_users_exposed, users_who_responded) for a variant."""
        # Total users assigned to this variant (approximate from experiment counts col)
        row = await fetchrow(
            f"SELECT {variant}_count FROM feedback_experiments WHERE id = $1",
            uuid.UUID(experiment_id),
        )
        total = int(row[f"{variant}_count"] or 0) if row else 0

        # Users who sent a signal
        responded = await fetchval(
            "SELECT COUNT(DISTINCT user_id) FROM experiment_signals WHERE experiment_id = $1 AND variant = $2",
            uuid.UUID(experiment_id),
            variant,
        )
        responded = int(responded or 0)
        return max(total, responded), responded

    @staticmethod
    def _z_test_proportions(
        ctrl_k: int, ctrl_n: int, treat_k: int, treat_n: int
    ) -> Tuple[float, bool]:
        """
        Simple two-proportion z-test.
        Returns (p_value, is_significant).
        Falls back to scipy if available, otherwise manual implementation.
        """
        if ctrl_n < 5 or treat_n < 5:
            return 1.0, False

        try:
            from scipy import stats as scipy_stats
            _, p_value = scipy_stats.proportions_ztest(
                [treat_k, ctrl_k], [treat_n, ctrl_n]
            )
            return float(p_value), float(p_value) < SIGNIFICANCE_THRESHOLD
        except ImportError:
            pass

        # Manual z-test
        p_ctrl = ctrl_k / ctrl_n
        p_treat = treat_k / treat_n
        p_pool = (ctrl_k + treat_k) / (ctrl_n + treat_n)

        if p_pool <= 0 or p_pool >= 1:
            return 1.0, False

        se = math.sqrt(p_pool * (1 - p_pool) * (1 / ctrl_n + 1 / treat_n))
        if se == 0:
            return 1.0, False

        z = (p_treat - p_ctrl) / se
        # Two-tailed p-value approximation (Abramowitz & Stegun)
        p_value = 2 * (1 - _norm_cdf(abs(z)))
        return p_value, p_value < SIGNIFICANCE_THRESHOLD

    async def _generate_summary(
        self,
        exp: FeedbackExperiment,
        ctrl_rate: float,
        treat_rate: float,
        ctrl_quality: float,
        treat_quality: float,
        p_value: float,
        winner: str,
    ) -> str:
        """Generate a plain-English summary of the experiment result."""
        screen_types_str = ", ".join(exp.screen_types) if exp.screen_types else "unknown"

        if winner == "treatment":
            ratio = treat_rate / ctrl_rate if ctrl_rate > 0 else float("inf")
            action = f"Promoting {exp.mechanism_type} as default for {screen_types_str} screens."
            return (
                f"{exp.mechanism_type} got {ratio:.1f}x more responses than "
                f"{exp.control_mechanism} on {screen_types_str} (p={p_value:.2f}). {action}"
            )
        elif winner == "control":
            return (
                f"{exp.control_mechanism} outperformed {exp.mechanism_type} on "
                f"{screen_types_str} (response rates: {ctrl_rate:.1%} vs {treat_rate:.1%}, "
                f"p={p_value:.2f}). Keeping {exp.control_mechanism} as default."
            )
        else:
            return (
                f"Experiment inconclusive: {exp.mechanism_type} vs {exp.control_mechanism} "
                f"on {screen_types_str} (p={p_value:.2f}, n={exp.control_count}/{exp.treatment_count}). "
                f"Need more data."
            )

    async def _promote_mechanism(self, exp: FeedbackExperiment):
        """Promote the winning mechanism as default for the experiment's screen types."""
        try:
            # Store the promotion as an A/B test result for future screen generation
            await execute(
                """
                UPDATE ab_tests
                SET results = jsonb_set(
                    COALESCE(results, '{}'::jsonb),
                    '{promoted_mechanism}',
                    $1::jsonb
                ),
                status = 'completed'
                WHERE name = $2
                """,
                json.dumps({
                    "mechanism": exp.mechanism_type,
                    "screen_types": exp.screen_types,
                    "promoted_at": datetime.now(timezone.utc).isoformat(),
                }),
                f"feedback_exp_{exp.id[:8]}",
            )
            logger.info(
                f"FeedbackExperimenter: promoted {exp.mechanism_type} for "
                f"{exp.screen_types}"
            )
        except Exception as e:
            logger.warning(f"FeedbackExperimenter: promote failed ({e})")

    async def _write_lesson(
        self,
        exp: FeedbackExperiment,
        summary: str,
        winner: str,
        p_value: float,
        promoted: bool,
    ):
        """Write experiment result as a lesson to ora_lessons."""
        confidence = max(0.5, min(0.99, 1.0 - p_value)) if p_value < 1.0 else 0.5
        applies_to = {
            "screen_types": exp.screen_types,
            "mechanism": exp.mechanism_type,
        }
        try:
            await execute(
                """
                INSERT INTO ora_lessons
                    (source, lesson, confidence, applied, applies_to)
                VALUES ($1, $2, $3, $4, $5)
                """,
                "feedback_experiment",
                summary,
                confidence,
                promoted,
                json.dumps(applies_to),
            )
        except Exception as e:
            logger.warning(f"FeedbackExperimenter: write lesson failed ({e})")

    # -----------------------------------------------------------------------
    # 6. Evaluation loop
    # -----------------------------------------------------------------------

    async def run_evaluation_loop(self):
        """
        Background task: runs every 6 hours.
        - Evaluates eligible experiments
        - Generates new hypothesis when needed
        - Designs + deploys up to MAX_CONCURRENT_EXPERIMENTS
        """
        self._running = True
        logger.info("FeedbackExperimenter: evaluation loop started")

        while self._running:
            try:
                await self._evaluation_tick()
            except Exception as e:
                logger.error(f"FeedbackExperimenter: loop error ({e})", exc_info=True)

            await asyncio.sleep(6 * 3600)  # 6 hours

    async def _evaluation_tick(self):
        """Single evaluation pass."""
        # Evaluate eligible running experiments
        eligible = await fetch(
            """
            SELECT id, started_at, duration_days, treatment_count, sample_size_target
            FROM feedback_experiments
            WHERE status = 'running'
            """
        )
        now = datetime.now(timezone.utc)
        for row in eligible:
            started = row["started_at"]
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed_days = (now - started).total_seconds() / 86400
            duration_done = elapsed_days >= row["duration_days"]
            sample_done = (row["treatment_count"] or 0) >= (row["sample_size_target"] or 100)

            if duration_done or sample_done:
                try:
                    await self.evaluate_experiment(str(row["id"]))
                except Exception as e:
                    logger.warning(f"FeedbackExperimenter: evaluate failed for {str(row['id'])[:8]}: {e}")

        # Check if we need a new hypothesis
        running_count = await fetchval(
            "SELECT COUNT(*) FROM feedback_experiments WHERE status = 'running'"
        )
        running_count = int(running_count or 0)

        if running_count >= MAX_CONCURRENT_EXPERIMENTS:
            return

        # Check if avg response rate is low enough to warrant a new experiment
        should_generate = await self._should_generate_new_experiment()
        if should_generate and running_count < MAX_CONCURRENT_EXPERIMENTS:
            try:
                hypothesis = await self.generate_hypothesis()
                experiment = await self.design_experiment(hypothesis)
                await self.deploy_experiment(experiment)
                logger.info(
                    f"FeedbackExperimenter: new experiment deployed | "
                    f"id={experiment.id[:8]}"
                )
            except Exception as e:
                logger.warning(f"FeedbackExperimenter: deploy new experiment failed ({e})")

    async def _should_generate_new_experiment(self) -> bool:
        """
        Returns True if: no experiment ran in last 7 days, OR avg response rate < 30%.
        """
        # Last experiment created
        last = await fetchval(
            "SELECT MAX(created_at) FROM feedback_experiments"
        )
        if last is None:
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        days_since = (datetime.now(timezone.utc) - last).total_seconds() / 86400
        if days_since >= 7:
            return True

        # Check overall response rate
        total = await fetchval("SELECT COUNT(*) FROM interactions") or 0
        rated = await fetchval("SELECT COUNT(*) FROM interactions WHERE rating IS NOT NULL") or 0
        rate = float(rated) / float(total) if total > 0 else 0.0
        return rate < 0.30

    # -----------------------------------------------------------------------
    # Public helper: get active experiment for a screen + user
    # -----------------------------------------------------------------------

    async def get_active_experiment_for_screen(
        self, screen_type: str, user_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Returns the active experiment component spec for a screen type + user,
        or None if no active experiment applies.
        Used by brain.get_screen() to inject experiment components.
        """
        try:
            rows = await fetch(
                """
                SELECT id, mechanism_type, control_mechanism, screen_types
                FROM feedback_experiments
                WHERE status = 'running'
                  AND screen_types ? $1
                LIMIT 1
                """,
                screen_type,
            )
            if not rows:
                return None

            row = rows[0]
            exp_id = str(row["id"])
            variant = self._assign_variant(user_id, exp_id)

            if variant == "treatment":
                comp = self._build_component_spec(FeedbackExperiment(
                    id=exp_id,
                    hypothesis="",
                    mechanism_type=row["mechanism_type"],
                    control_mechanism=row["control_mechanism"],
                    screen_types=row["screen_types"] or [],
                    status="running",
                    sample_size_target=100,
                    duration_days=7,
                    started_at=datetime.now(timezone.utc),
                ))
            else:
                comp = {
                    "type": f"feedback_{row['control_mechanism']}",
                    "experiment_id": exp_id,
                    "variant": "control",
                    "position": "bottom_right",
                    "always_visible": True,
                }
            return comp
        except Exception as e:
            logger.warning(f"FeedbackExperimenter: get_active_experiment failed ({e})")
            return None

    # -----------------------------------------------------------------------
    # Utility
    # -----------------------------------------------------------------------

    @staticmethod
    def _row_to_experiment(row) -> FeedbackExperiment:
        screen_types = row["screen_types"]
        if isinstance(screen_types, str):
            screen_types = json.loads(screen_types)
        elif screen_types is None:
            screen_types = []

        return FeedbackExperiment(
            id=str(row["id"]),
            hypothesis=row["hypothesis"] or "",
            mechanism_type=row["mechanism_type"] or "emoji_reaction",
            control_mechanism=row["control_mechanism"] or "star_rating",
            screen_types=screen_types,
            status=row["status"] or "running",
            sample_size_target=int(row["sample_size_target"] or 100),
            duration_days=int(row["duration_days"] or 7),
            started_at=row.get("started_at") or datetime.now(timezone.utc),
            control_count=int(row.get("control_count") or 0),
            treatment_count=int(row.get("treatment_count") or 0),
            control_response_rate=float(row.get("control_response_rate") or 0.0),
            treatment_response_rate=float(row.get("treatment_response_rate") or 0.0),
            control_signal_quality=float(row.get("control_signal_quality") or 0.0),
            treatment_signal_quality=float(row.get("treatment_signal_quality") or 0.0),
            p_value=row.get("p_value"),
            winner=row.get("winner"),
            summary=row.get("summary"),
            completed_at=row.get("completed_at"),
        )


# ---------------------------------------------------------------------------
# Math helper: Normal CDF approximation (for z-test without scipy)
# ---------------------------------------------------------------------------

def _norm_cdf(z: float) -> float:
    """Approximation of the standard normal CDF using Horner's method."""
    # Abramowitz & Stegun approximation (error < 7.5e-8)
    t = 1.0 / (1.0 + 0.2316419 * abs(z))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    approx = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * z * z) * poly
    return approx if z >= 0 else 1.0 - approx
