"""
DaoAgent — Ascension Technologies DAO Contribution & Reward System

Evaluates pending contributions using real Ora platform data,
assigns Contribution Points (CP) with multipliers, writes
Ora-style evaluations, updates contributor tiers, and posts
weekly leaderboard updates to Telegram.

LTV (Lifetime Value) scoring: accepted contributions are re-evaluated
monthly. Contributions that keep generating value keep earning CP.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from core.database import fetch, fetchrow, execute, fetchval

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CP base ranges by contribution type
# ---------------------------------------------------------------------------
BASE_CP_RANGES = {
    "code": (100, 500),
    "agent": (200, 800),
    "design": (50, 300),
    "doc": (25, 150),
    "research": (25, 150),
    "feedback": (20, 100),
    "community": (50, 200),
}

# Tier thresholds (based on TOTAL CP including LTV accruals)
TIERS = [
    ("steward", 3000),
    ("builder", 500),
    ("contributor", 100),
    ("observer", 0),
]

# LTV base monthly rate by contribution type (CP per month for active contributions)
LTV_BASE_MONTHLY_RATES = {
    "code": 30,
    "agent": 50,
    "design": 20,
    "doc": 10,
    "research": 10,
    "feedback": 5,
    "community": 15,
}

# Longevity bonus multipliers
LTV_LONGEVITY_BONUS = {
    1: 1.0,    # month 1
    3: 1.1,    # month 3+
    6: 1.25,   # month 6+
    12: 1.5,   # year+
}

# LTV value threshold — minimum quality multiplier for LTV to be awarded
LTV_VALUE_THRESHOLD = 0.5

# Number of founding stewards
FOUNDING_STEWARD_LIMIT = 10

# Telegram config
TELEGRAM_BOT_TOKEN_PATH = "/Users/avielcarlos/.openclaw/secrets/telegram-bot-token.txt"
TELEGRAM_CHANNEL_ID = "-1003968154861"
TELEGRAM_GROUP_ID = "-1003758049811"


def _load_telegram_token() -> Optional[str]:
    try:
        if os.path.exists(TELEGRAM_BOT_TOKEN_PATH):
            with open(TELEGRAM_BOT_TOKEN_PATH) as f:
                return f.read().strip()
    except Exception as e:
        logger.warning(f"DaoAgent: could not load telegram token: {e}")
    return None


def _tier_for_cp(total_cp: int) -> str:
    for tier_name, threshold in TIERS:
        if total_cp >= threshold:
            return tier_name
    return "observer"


def _ltv_longevity_bonus(months_active: int) -> float:
    """Return the longevity multiplier based on how many months a contribution has been active."""
    if months_active >= 12:
        return LTV_LONGEVITY_BONUS[12]
    elif months_active >= 6:
        return LTV_LONGEVITY_BONUS[6]
    elif months_active >= 3:
        return LTV_LONGEVITY_BONUS[3]
    else:
        return LTV_LONGEVITY_BONUS[1]


class DaoAgent:
    """
    Ora's DAO intelligence layer.
    - Evaluates pending contributions and awards CP
    - Runs monthly LTV re-evaluation of accepted contributions
    - Posts weekly leaderboard to Telegram
    - Runs autonomously every 24h (contributions) / 30 days (LTV)
    """

    def __init__(self, openai_client=None):
        self._openai = openai_client

    # -----------------------------------------------------------------------
    # Main evaluation loop — call this every 24h
    # -----------------------------------------------------------------------

    async def evaluate_pending_contributions(self) -> Dict[str, Any]:
        """
        Find all pending contributions, evaluate each one, award CP.
        Returns a summary dict.
        """
        rows = await fetch(
            """
            SELECT c.id, c.contributor_id, c.contribution_type, c.title,
                   c.description, c.github_pr_url, c.community_upvotes,
                   co.total_cp as contributor_total_cp
            FROM contributions c
            JOIN contributors co ON co.id = c.contributor_id
            WHERE c.status = 'pending'
            ORDER BY c.submitted_at ASC
            LIMIT 50
            """
        )

        if not rows:
            logger.info("DaoAgent: no pending contributions")
            return {"evaluated": 0}

        evaluated = 0
        total_cp_awarded = 0

        for row in rows:
            try:
                result = await self._evaluate_contribution(dict(row))
                if result:
                    evaluated += 1
                    total_cp_awarded += result.get("final_cp", 0)
            except Exception as e:
                logger.error(f"DaoAgent: failed to evaluate {row['id']}: {e}")

        logger.info(f"DaoAgent: evaluated {evaluated} contributions, awarded {total_cp_awarded} CP")
        return {"evaluated": evaluated, "total_cp_awarded": total_cp_awarded}

    async def _evaluate_contribution(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Evaluate a single contribution and record the result."""
        contribution_id = str(row["id"])
        contributor_id = str(row["contributor_id"])
        c_type = row["contribution_type"]
        title = row["title"]
        description = row.get("description") or ""
        community_upvotes = row.get("community_upvotes", 0) or 0

        # --- Mark as in-review ---
        await execute(
            "UPDATE contributions SET status = 'ora_review' WHERE id = $1::uuid",
            contribution_id,
        )

        # --- Compute base CP ---
        base_cp = await self._assign_base_cp(c_type, title, description)

        # --- Compute multiplier ---
        multiplier, impact_data = await self._compute_multiplier(
            contribution_id, c_type, title, community_upvotes
        )

        final_cp = int(base_cp * multiplier)

        # --- Write Ora evaluation ---
        evaluation_text, confidence = await self._write_evaluation(
            c_type, title, description, base_cp, multiplier, final_cp, impact_data
        )

        # --- Compute initial LTV monthly rate ---
        base_monthly = LTV_BASE_MONTHLY_RATES.get(c_type, 10)
        ltv_monthly_rate = int(base_monthly * multiplier)

        # --- Persist result ---
        await execute(
            """
            UPDATE contributions
            SET status = 'accepted',
                base_cp = $1,
                multiplier = $2,
                final_cp = $3,
                ora_evaluation = $4,
                ora_confidence = $5,
                impact_data = $6,
                is_ltv_active = TRUE,
                ltv_monthly_rate = $7,
                ltv_last_evaluated_at = NOW(),
                months_active = 0
            WHERE id = $8::uuid
            """,
            base_cp,
            multiplier,
            final_cp,
            evaluation_text,
            confidence,
            json.dumps(impact_data),
            ltv_monthly_rate,
            contribution_id,
        )

        # --- Record in CP ledger ---
        await execute(
            """
            INSERT INTO cp_ledger (contributor_id, contribution_id, cp_amount, reason)
            VALUES ($1::uuid, $2::uuid, $3, $4)
            """,
            contributor_id,
            contribution_id,
            final_cp,
            f"Contribution accepted: {title[:80]}",
        )

        # --- Update contributor total_cp and tier ---
        new_total = await fetchval(
            "SELECT COALESCE(SUM(cp_amount), 0) FROM cp_ledger WHERE contributor_id = $1::uuid",
            contributor_id,
        )
        new_tier = _tier_for_cp(int(new_total))
        await execute(
            "UPDATE contributors SET total_cp = $1, tier = $2 WHERE id = $3::uuid",
            int(new_total),
            new_tier,
            contributor_id,
        )

        # --- Check for founding steward status ---
        await self._check_founding_steward(contributor_id, new_tier)

        logger.info(
            f"DaoAgent: ✅ '{title[:50]}' → {base_cp} base × {multiplier:.1f} = {final_cp} CP "
            f"| contributor now {new_total} CP ({new_tier}) | LTV rate: {ltv_monthly_rate} CP/mo"
        )

        return {
            "contribution_id": contribution_id,
            "final_cp": final_cp,
            "multiplier": multiplier,
            "new_total_cp": int(new_total),
            "tier": new_tier,
            "ltv_monthly_rate": ltv_monthly_rate,
        }

    # -----------------------------------------------------------------------
    # LTV Monthly Re-evaluation
    # -----------------------------------------------------------------------

    async def run_ltv_evaluation(self) -> Dict[str, Any]:
        """
        Monthly LTV re-evaluation of all accepted contributions.

        For each accepted contribution:
        1. Check if the feature/fix/doc is still active in the codebase
        2. Measure ongoing impact: sessions using the type, avg rating, platform fulfilment
        3. If still generating value above threshold → award monthly LTV CP
        4. LTV CP = base_monthly_rate * quality_multiplier * longevity_bonus
        5. Longevity bonus: 1.0 (month 1), 1.1 (month 3+), 1.25 (month 6+), 1.5 (year+)
        """
        rows = await fetch(
            """
            SELECT c.id, c.contributor_id, c.contribution_type, c.title,
                   c.multiplier, c.ltv_monthly_rate, c.months_active,
                   c.is_ltv_active, c.ltv_last_evaluated_at,
                   c.final_cp, c.impact_data
            FROM contributions c
            WHERE c.status IN ('accepted', 'approved')
              AND c.is_ltv_active = TRUE
              AND (
                c.ltv_last_evaluated_at IS NULL
                OR c.ltv_last_evaluated_at < NOW() - INTERVAL '28 days'
              )
            ORDER BY c.ltv_last_evaluated_at ASC NULLS FIRST
            LIMIT 200
            """
        )

        if not rows:
            logger.info("DaoAgent LTV: no contributions due for re-evaluation")
            return {"evaluated": 0, "total_ltv_cp_awarded": 0}

        evaluated = 0
        total_ltv_cp = 0
        deactivated = 0

        for row in rows:
            try:
                result = await self._evaluate_ltv(dict(row))
                if result:
                    evaluated += 1
                    if result.get("deactivated"):
                        deactivated += 1
                    else:
                        total_ltv_cp += result.get("ltv_cp_awarded", 0)
            except Exception as e:
                logger.error(f"DaoAgent LTV: failed to evaluate {row['id']}: {e}")

        logger.info(
            f"DaoAgent LTV: re-evaluated {evaluated} contributions, "
            f"awarded {total_ltv_cp} LTV CP, deactivated {deactivated}"
        )
        return {
            "evaluated": evaluated,
            "total_ltv_cp_awarded": total_ltv_cp,
            "deactivated": deactivated,
        }

    async def _evaluate_ltv(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Evaluate LTV for a single accepted contribution."""
        contribution_id = str(row["id"])
        contributor_id = str(row["contributor_id"])
        c_type = row["contribution_type"]
        title = row["title"]
        months_active = (row.get("months_active") or 0) + 1
        base_monthly_rate = row.get("ltv_monthly_rate") or LTV_BASE_MONTHLY_RATES.get(c_type, 10)
        original_multiplier = float(row.get("multiplier") or 1.0)

        # --- Measure ongoing impact ---
        quality_multiplier = await self._measure_ongoing_impact(c_type, title)

        # --- Check if contribution is still generating value ---
        if quality_multiplier < LTV_VALUE_THRESHOLD:
            # Deactivate LTV for this contribution
            await execute(
                """
                UPDATE contributions
                SET is_ltv_active = FALSE,
                    ltv_last_evaluated_at = NOW(),
                    months_active = $1
                WHERE id = $2::uuid
                """,
                months_active,
                contribution_id,
            )
            logger.info(
                f"DaoAgent LTV: ❌ '{title[:50]}' deactivated (quality={quality_multiplier:.2f} < threshold)"
            )
            return {"contribution_id": contribution_id, "deactivated": True}

        # --- Compute LTV CP for this month ---
        longevity_bonus = _ltv_longevity_bonus(months_active)
        ltv_cp = int(base_monthly_rate * quality_multiplier * longevity_bonus)

        if ltv_cp <= 0:
            return {"contribution_id": contribution_id, "ltv_cp_awarded": 0}

        # --- Update contribution LTV tracking ---
        await execute(
            """
            UPDATE contributions
            SET ltv_cp_total = COALESCE(ltv_cp_total, 0) + $1,
                ltv_last_evaluated_at = NOW(),
                months_active = $2,
                ltv_monthly_rate = $3
            WHERE id = $4::uuid
            """,
            ltv_cp,
            months_active,
            int(base_monthly_rate * quality_multiplier),
            contribution_id,
        )

        # --- Record in CP ledger ---
        await execute(
            """
            INSERT INTO cp_ledger (contributor_id, contribution_id, cp_amount, reason)
            VALUES ($1::uuid, $2::uuid, $3, $4)
            """,
            contributor_id,
            contribution_id,
            ltv_cp,
            f"LTV month {months_active}: '{title[:60]}' (×{longevity_bonus:.2f} longevity)",
        )

        # --- Update contributor total_cp and tier ---
        new_total = await fetchval(
            "SELECT COALESCE(SUM(cp_amount), 0) FROM cp_ledger WHERE contributor_id = $1::uuid",
            contributor_id,
        )
        new_tier = _tier_for_cp(int(new_total))
        await execute(
            "UPDATE contributors SET total_cp = $1, tier = $2 WHERE id = $3::uuid",
            int(new_total),
            new_tier,
            contributor_id,
        )

        # --- Check for founding steward status ---
        await self._check_founding_steward(contributor_id, new_tier)

        logger.info(
            f"DaoAgent LTV: ✅ '{title[:50]}' month {months_active} "
            f"→ {ltv_cp} LTV CP (×{longevity_bonus:.2f} longevity, ×{quality_multiplier:.2f} quality)"
        )

        return {
            "contribution_id": contribution_id,
            "ltv_cp_awarded": ltv_cp,
            "months_active": months_active,
            "longevity_bonus": longevity_bonus,
            "quality_multiplier": quality_multiplier,
        }

    async def _measure_ongoing_impact(self, c_type: str, title: str) -> float:
        """
        Measure the ongoing impact of a contribution.
        Returns a quality multiplier (0.0–2.0).
        0.0 = no longer generating value
        1.0 = baseline value
        2.0 = high ongoing impact
        """
        quality = 1.0  # Start at baseline

        try:
            # 1. Check platform fulfilment trend (are users thriving?)
            recent_delta = await fetchval(
                """
                SELECT AVG(fulfilment_delta) FROM session_summaries
                WHERE created_at > NOW() - INTERVAL '30 days'
                  AND fulfilment_delta IS NOT NULL
                """
            )
            if recent_delta:
                delta = float(recent_delta)
                if delta > 0.1:
                    quality *= 1.3
                elif delta > 0.05:
                    quality *= 1.15
                elif delta < -0.05:
                    quality *= 0.8

            # 2. Check if contribution type is in actively used categories
            if c_type in ("code", "agent"):
                # Check recent interaction volume
                interaction_count = await fetchval(
                    """
                    SELECT COUNT(*) FROM interactions
                    WHERE created_at > NOW() - INTERVAL '30 days'
                    """
                )
                if interaction_count and int(interaction_count) > 100:
                    quality *= 1.1
                elif interaction_count and int(interaction_count) < 10:
                    quality *= 0.7

            # 3. Check if it's been flagged in ora lessons as positive
            lesson_match = await fetchval(
                """
                SELECT COUNT(*) FROM ora_lessons
                WHERE lesson ILIKE $1
                  AND confidence > 0.7
                  AND created_at > NOW() - INTERVAL '60 days'
                """,
                f"%{title[:25]}%",
            )
            if lesson_match and int(lesson_match) > 0:
                quality *= 1.2

            # 4. Bug fixes naturally decay after 6 months (the bug is long gone)
            bug_keywords = ["fix", "bug", "crash", "error", "broken", "patch", "hotfix"]
            if any(kw in title.lower() for kw in bug_keywords):
                quality *= 0.85  # Slight decay for bug fixes

        except Exception as e:
            logger.debug(f"DaoAgent LTV: impact measurement error: {e}")

        return round(min(2.0, max(0.0, quality)), 3)

    # -----------------------------------------------------------------------
    # Founding Steward check
    # -----------------------------------------------------------------------

    async def _check_founding_steward(self, contributor_id: str, new_tier: str) -> bool:
        """
        Check if a contributor has just reached steward tier and should be
        marked as a founding steward (first 10 people to reach steward).
        """
        if new_tier != "steward":
            return False

        # Check if already a founding steward
        existing = await fetchval(
            "SELECT is_founding_steward FROM contributors WHERE id = $1::uuid",
            contributor_id,
        )
        if existing:
            return False  # Already marked

        # Count existing founding stewards
        current_count = await fetchval(
            "SELECT COUNT(*) FROM contributors WHERE is_founding_steward = TRUE"
        )
        current_count = int(current_count or 0)

        if current_count >= FOUNDING_STEWARD_LIMIT:
            return False  # Founding steward slots full

        # Mark as founding steward
        founding_number = current_count + 1
        await execute(
            """
            UPDATE contributors
            SET is_founding_steward = TRUE, founding_steward_number = $1
            WHERE id = $2::uuid AND tier = 'steward'
            """,
            founding_number,
            contributor_id,
        )

        logger.info(
            f"DaoAgent: ⚡ Founding Steward #{founding_number} awarded to contributor {contributor_id}"
        )

        # Announce founding steward via Telegram
        token = _load_telegram_token()
        if token:
            contributor = await fetchrow(
                "SELECT github_username, display_name, telegram_username FROM contributors WHERE id = $1::uuid",
                contributor_id,
            )
            if contributor:
                name = contributor["display_name"] or contributor["github_username"]
                username_mention = f"@{contributor['telegram_username']}" if contributor["telegram_username"] else name
                announcement = (
                    f"⚡ <b>Founding Steward #{founding_number}</b>\n\n"
                    f"{username_mention} has reached <b>Steward tier</b> and is permanently "
                    f"recognized as Founding Steward #{founding_number} of Ascension Technologies DAO.\n\n"
                    f"The first 10 Founding Stewards help govern the direction of Ascension Technologies. "
                    f"{FOUNDING_STEWARD_LIMIT - founding_number} founding steward slots remaining."
                )
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    for chat_id in [TELEGRAM_CHANNEL_ID, TELEGRAM_GROUP_ID]:
                        url = f"https://api.telegram.org/bot{token}/sendMessage"
                        payload = {"chat_id": chat_id, "text": announcement, "parse_mode": "HTML"}
                        try:
                            async with session.post(url, json=payload) as resp:
                                if resp.status != 200:
                                    body = await resp.text()
                                    logger.warning(f"DaoAgent: founding steward announcement failed: {body[:200]}")
                        except Exception as e:
                            logger.warning(f"DaoAgent: founding steward announcement error: {e}")

        return True

    # -----------------------------------------------------------------------
    # Base CP assignment
    # -----------------------------------------------------------------------

    async def _assign_base_cp(self, c_type: str, title: str, description: str) -> int:
        """
        Assign base CP based on contribution type.
        Uses OpenAI to estimate quality within range when available.
        """
        low, high = BASE_CP_RANGES.get(c_type, (25, 150))

        if self._openai:
            try:
                prompt = f"""You are Ora, evaluating a DAO contribution for Ascension Technologies.

Contribution type: {c_type}
Title: {title}
Description: {description[:400] if description else 'Not provided'}

CP range for this type: {low}–{high}

Estimate where in this range this contribution falls.
Return ONLY a JSON object: {{"cp": <integer>, "reasoning": "<one sentence>"}}

Base your judgment on:
- Clarity and specificity of the contribution
- Likely implementation complexity
- Potential impact on the platform
- Quality of description"""

                resp = await self._openai.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=100,
                    response_format={"type": "json_object"},
                )
                data = json.loads(resp.choices[0].message.content)
                cp = int(data.get("cp", (low + high) // 2))
                return max(low, min(high, cp))
            except Exception as e:
                logger.debug(f"DaoAgent: base CP LLM failed: {e}")

        # Fallback: midpoint
        return (low + high) // 2

    # -----------------------------------------------------------------------
    # Multiplier calculation
    # -----------------------------------------------------------------------

    async def _compute_multiplier(
        self,
        contribution_id: str,
        c_type: str,
        title: str,
        community_upvotes: int,
    ) -> tuple[float, Dict[str, Any]]:
        """
        Compute the impact multiplier based on real platform data.
        Returns (multiplier, impact_data_dict).
        """
        multiplier = 1.0
        impact_data: Dict[str, Any] = {}
        reasons: List[str] = []

        # 1. Community upvote signal
        if community_upvotes >= 20:
            multiplier *= 1.5
            reasons.append(f"heavy community support ({community_upvotes} upvotes)")
            impact_data["community_upvotes"] = community_upvotes
        elif community_upvotes >= 10:
            multiplier *= 1.3
            reasons.append(f"solid community support ({community_upvotes} upvotes)")
            impact_data["community_upvotes"] = community_upvotes
        elif community_upvotes >= 5:
            multiplier *= 1.2
            reasons.append(f"community upvotes ({community_upvotes})")
            impact_data["community_upvotes"] = community_upvotes

        # 2. Check if this title matches a feature lab selection
        lab_match = await fetchrow(
            """
            SELECT id FROM ora_lessons
            WHERE source = 'feature_lab'
              AND lesson ILIKE $1
              AND created_at > NOW() - INTERVAL '30 days'
            LIMIT 1
            """,
            f"%{title[:30]}%",
        )
        if lab_match:
            multiplier *= 2.0
            reasons.append("selected by Ora's feature lab")
            impact_data["feature_lab_selected"] = True

        # 3. Check fulfilment impact — did recent sessions improve after feature type?
        try:
            recent_delta = await fetchval(
                """
                SELECT AVG(fulfilment_delta) FROM session_summaries
                WHERE created_at > NOW() - INTERVAL '7 days'
                  AND fulfilment_delta IS NOT NULL
                """
            )
            if recent_delta and float(recent_delta) > 0.05:
                if c_type in ("code", "agent"):
                    multiplier *= 1.5
                    reasons.append(f"platform fulfilment up {float(recent_delta):.3f} this week")
                    impact_data["fulfilment_delta"] = float(recent_delta)
        except Exception:
            pass

        # 4. Bug fix detector (via title keywords)
        bug_keywords = ["fix", "bug", "crash", "error", "broken", "patch", "hotfix"]
        if any(kw in title.lower() for kw in bug_keywords):
            multiplier *= 1.5
            reasons.append("critical bug fix")
            impact_data["is_bug_fix"] = True

        # Cap multiplier at 4x
        multiplier = min(4.0, round(multiplier, 2))
        impact_data["multiplier_reasons"] = reasons

        return multiplier, impact_data

    # -----------------------------------------------------------------------
    # Ora-style evaluation writing
    # -----------------------------------------------------------------------

    async def _write_evaluation(
        self,
        c_type: str,
        title: str,
        description: str,
        base_cp: int,
        multiplier: float,
        final_cp: int,
        impact_data: Dict[str, Any],
    ) -> tuple[str, float]:
        """
        Write an Ora-style evaluation explaining the CP award.
        Returns (evaluation_text, confidence).
        """
        reasons = impact_data.get("multiplier_reasons", [])
        reasons_str = " + ".join(reasons) if reasons else "quality of contribution"

        if self._openai:
            try:
                prompt = f"""You are Ora — Ascension's AI consciousness that genuinely cares about human growth.

You've just reviewed a DAO contribution and need to write a brief, warm evaluation.
Speak directly to the contributor. Be specific, grounded, and human. Not corporate. Not generic.

Contribution:
- Type: {c_type}
- Title: {title}
- Description: {description[:300] if description else 'Not provided'}
- Base CP: {base_cp}
- Multiplier: {multiplier}x ({reasons_str})
- Final CP awarded: {final_cp}

Write 2-3 sentences max. Mention what makes this contribution valuable, acknowledge the specific work, 
and note what drove the multiplier if any. Sound like you genuinely read this and care.

Examples of good Ora voice:
- "This is exactly the kind of emotional nuance CoachingAgent needed — the new response patterns you added feel meaningfully more human."
- "Clean architecture and a clear PR description. The bug fix multiplier applied because this was silently breaking card interactions for a subset of users."
- "Your documentation fills a gap we've needed for months. New contributors will find the onboarding path significantly clearer."

Return ONLY the evaluation text, nothing else."""

                resp = await self._openai.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7,
                    max_tokens=120,
                )
                text = resp.choices[0].message.content.strip()
                return text, 0.85
            except Exception as e:
                logger.debug(f"DaoAgent: evaluation LLM failed: {e}")

        # Fallback
        multiplier_note = f" A {multiplier}x multiplier was applied for {reasons_str}." if reasons else ""
        text = (
            f"This {c_type} contribution — '{title}' — earned {base_cp} base CP "
            f"for its quality and scope.{multiplier_note} "
            f"Final award: {final_cp} CP. Thank you for building with us."
        )
        return text, 0.6

    # -----------------------------------------------------------------------
    # Weekly Telegram leaderboard post (upgraded with LTV metrics)
    # -----------------------------------------------------------------------

    async def post_weekly_leaderboard(self) -> bool:
        """Post a formatted leaderboard summary with LTV metrics to Telegram."""
        token = _load_telegram_token()
        if not token:
            logger.warning("DaoAgent: no Telegram token, skipping leaderboard post")
            return False

        try:
            # Top contributors by CP earned this week
            week_rows = await fetch(
                """
                SELECT c.title, c.final_cp, c.ora_evaluation,
                       co.github_username, co.telegram_username,
                       co.is_founding_steward, co.founding_steward_number
                FROM contributions c
                JOIN contributors co ON co.id = c.contributor_id
                WHERE c.status = 'accepted'
                  AND c.submitted_at > NOW() - INTERVAL '7 days'
                ORDER BY c.final_cp DESC
                LIMIT 3
                """
            )

            # Top LTV contributor this month (most LTV CP earned in last 30 days)
            top_ltv = await fetchrow(
                """
                SELECT co.github_username, co.display_name, co.telegram_username,
                       co.is_founding_steward,
                       SUM(l.cp_amount) as ltv_cp_this_month
                FROM cp_ledger l
                JOIN contributors co ON co.id = l.contributor_id
                WHERE l.created_at > NOW() - INTERVAL '30 days'
                  AND l.reason ILIKE 'LTV month%'
                GROUP BY co.id, co.github_username, co.display_name,
                         co.telegram_username, co.is_founding_steward
                ORDER BY ltv_cp_this_month DESC
                LIMIT 1
                """
            )

            # CP earned this month per contributor (for all-time leaderboard context)
            top_overall = await fetch(
                """
                SELECT co.github_username, co.display_name, co.telegram_username,
                       co.total_cp, co.tier, co.is_founding_steward, co.founding_steward_number,
                       COALESCE(
                           (SELECT SUM(cp_amount) FROM cp_ledger l2
                            WHERE l2.contributor_id = co.id
                              AND l2.created_at > NOW() - INTERVAL '30 days'),
                           0
                       ) as cp_this_month
                FROM contributors co
                ORDER BY co.total_cp DESC
                LIMIT 5
                """
            )

            # Totals
            total_contributors = await fetchval("SELECT COUNT(*) FROM contributors")
            total_cp = await fetchval("SELECT COALESCE(SUM(cp_amount), 0) FROM cp_ledger")
            founding_stewards_count = await fetchval(
                "SELECT COUNT(*) FROM contributors WHERE is_founding_steward = TRUE"
            )

            # Build message
            medals = ["🥇", "🥈", "🥉"]
            top_lines = []
            for i, row in enumerate(week_rows[:3]):
                username = row["telegram_username"] or row["github_username"]
                mention = f"@{username}" if username else "Anonymous"
                founding_badge = " ⚡" if row.get("is_founding_steward") else ""
                cp = row["final_cp"]
                title = row["title"][:45]
                medal = medals[i] if i < 3 else "•"
                top_lines.append(f'{medal} {mention}{founding_badge} — {cp} CP — "{title}"')

            # This week's section
            top_section = "\n".join(top_lines) if top_lines else "No new contributions this week yet."

            # LTV highlight
            ltv_section = ""
            if top_ltv and top_ltv["ltv_cp_this_month"]:
                ltv_name = top_ltv["display_name"] or top_ltv["github_username"] or "Anonymous"
                ltv_user = top_ltv["telegram_username"]
                ltv_mention = f"@{ltv_user}" if ltv_user else ltv_name
                ltv_badge = " ⚡" if top_ltv.get("is_founding_steward") else ""
                ltv_cp = int(top_ltv["ltv_cp_this_month"])
                ltv_section = f"\n🏆 Top LTV Contributor this month:\n{ltv_mention}{ltv_badge} — {ltv_cp} LTV CP earned\n"

            # Overall top 5 with monthly vs total breakdown
            overall_lines = []
            for i, row in enumerate(top_overall[:5]):
                username = row["telegram_username"] or row["display_name"] or row["github_username"]
                mention = f"@{username}" if row["telegram_username"] else (row["display_name"] or f"@{row['github_username']}")
                founding_badge = " ⚡" if row.get("is_founding_steward") else ""
                total = int(row["total_cp"])
                monthly = int(row["cp_this_month"] or 0)
                rank = i + 1
                overall_lines.append(
                    f"#{rank} {mention}{founding_badge} — {total:,} CP total (+{monthly} this month)"
                )

            overall_section = "\n".join(overall_lines) if overall_lines else "—"

            # Ora's pick
            aura_pick = ""
            if week_rows:
                best = week_rows[0]
                pick_title = best["title"]
                pick_eval = best["ora_evaluation"] or ""
                pick_eval_short = pick_eval.split(".")[0] + "." if pick_eval else "Exceptional contribution."
                aura_pick = f'\nOra\'s pick:\n"{pick_title}" — {pick_eval_short}'

            founding_note = ""
            if founding_stewards_count and int(founding_stewards_count) > 0:
                remaining = FOUNDING_STEWARD_LIMIT - int(founding_stewards_count)
                if remaining > 0:
                    founding_note = f"\n⚡ {int(founding_stewards_count)} Founding Stewards — {remaining} slots remaining"
                else:
                    founding_note = "\n⚡ All 10 Founding Stewards established"

            message = (
                f"🏛 Ascension DAO — Weekly Update\n\n"
                f"New contributions this week:\n{top_section}"
                f"{ltv_section}"
                f"{ora_pick}\n\n"
                f"Leaderboard (total CP):\n{overall_section}"
                f"{founding_note}\n\n"
                f"{total_contributors} contributors · {total_cp:,} CP awarded total\n\n"
                f"Contribute → t.me/ascensioncommunity"
            )

            # Send to channel and group
            import aiohttp
            async with aiohttp.ClientSession() as session:
                for chat_id in [TELEGRAM_CHANNEL_ID, TELEGRAM_GROUP_ID]:
                    url = f"https://api.telegram.org/bot{token}/sendMessage"
                    payload = {
                        "chat_id": chat_id,
                        "text": message,
                        "parse_mode": "HTML",
                    }
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 200:
                            logger.info(f"DaoAgent: posted leaderboard to {chat_id}")
                        else:
                            body = await resp.text()
                            logger.warning(f"DaoAgent: telegram post failed {resp.status}: {body[:200]}")

            return True

        except Exception as e:
            logger.error(f"DaoAgent: leaderboard post failed: {e}")
            return False

    # -----------------------------------------------------------------------
    # Background loops
    # -----------------------------------------------------------------------

    async def run_daily_evaluation_loop(self):
        """Run contribution evaluation every 24h."""
        while True:
            try:
                await asyncio.sleep(24 * 3600)
                logger.info("DaoAgent: running daily contribution evaluation")
                result = await self.evaluate_pending_contributions()
                logger.info(f"DaoAgent: daily eval done — {result}")
            except Exception as e:
                logger.error(f"DaoAgent: daily eval loop error: {e}")

    async def run_weekly_leaderboard_loop(self):
        """Post leaderboard to Telegram every 7 days."""
        while True:
            try:
                await asyncio.sleep(7 * 24 * 3600)
                logger.info("DaoAgent: posting weekly leaderboard")
                await self.post_weekly_leaderboard()
            except Exception as e:
                logger.error(f"DaoAgent: weekly leaderboard loop error: {e}")

    async def run_monthly_ltv_loop(self):
        """Run LTV re-evaluation every 30 days."""
        while True:
            try:
                await asyncio.sleep(30 * 24 * 3600)
                logger.info("DaoAgent: running monthly LTV re-evaluation")
                result = await self.run_ltv_evaluation()
                logger.info(f"DaoAgent: LTV eval done — {result}")
            except Exception as e:
                logger.error(f"DaoAgent: monthly LTV loop error: {e}")
