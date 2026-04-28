"""
A/B Testing API Routes
Handles UI surface event recording and variant queries.
Comprehensive experiment registry covering every page and component in iDo.
"""

import hashlib
import json
import logging
import math
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.middleware import get_current_user_id
from core.redis_client import get_redis
from ora.ab_testing import record_ui_event, get_winning_variant, get_ui_variant

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ab", tags=["ab_testing"])


# ─── Centralized Experiment Registry ──────────────────────────────────────────

EXPERIMENT_REGISTRY = {
    # ─── LANDING / HOME ───
    "home_hero_headline": {
        "variants": {
            "A": "The AI that knows what you want",
            "B": "Your goals. Ora's intelligence.",
            "C": "What do you actually want to do today?",
            "D": "iDo. Two letters. One life.",
        },
        "metric": "signup_rate",
        "page": "landing",
    },
    "home_cta_text": {
        "variants": {
            "A": "Get Started Free",
            "B": "Try iDo Free",
            "C": "Meet Ora",
            "D": "Start for Free",
        },
        "metric": "signup_click_rate",
        "page": "landing",
    },
    "home_entry_state": {
        "variants": {
            "know": "I know what I want → Goals",
            "explore": "Show me what to do → Feed",
        },
        "metric": "session_depth",
        "page": "home",
    },
    "home_intake_depth": {
        "variants": {
            "deep": "3 intake questions",
            "one_q": "1 open question",
            "instant": "straight to feed",
        },
        "metric": "feed_rating_avg",
        "page": "home",
    },
    # ─── ONBOARDING ───
    "onboarding_length": {
        "variants": {
            "A": "3 steps",
            "B": "5 steps",
            "C": "1 step (name only)",
            "D": "skip (go straight to app)",
        },
        "metric": "day7_retention",
        "page": "onboarding",
    },
    "onboarding_goal_framing": {
        "variants": {
            "A": "What do you want to achieve?",
            "B": "What's one thing you want to do?",
            "C": "I want to...",
            "D": "What matters most to you right now?",
        },
        "metric": "goal_set_rate",
        "page": "onboarding",
    },
    "onboarding_social_proof": {
        "variants": {
            "A": "shown — X people using iDo",
            "B": "hidden",
        },
        "metric": "completion_rate",
        "page": "onboarding",
    },
    # ─── FEED ───
    "feed_card_layout": {
        "variants": {
            "A": "full screen card",
            "B": "card with visible next card behind",
            "C": "list of 3 cards",
        },
        "metric": "cards_per_session",
        "page": "feed",
    },
    "feed_rating_ui": {
        "variants": {
            "A": "thumbs up / skip buttons",
            "B": "swipe gesture hint",
            "C": "5-star rating",
            "D": "heart / x buttons",
        },
        "metric": "rating_completion_rate",
        "page": "feed",
    },
    "feed_empty_state": {
        "variants": {
            "A": "Talk to Ora to get more cards",
            "B": "You've seen everything for now — come back tomorrow",
            "C": "Add a goal to get personalized cards",
        },
        "metric": "return_rate",
        "page": "feed",
    },
    "feed_goal_banner": {
        "variants": {
            "A": "shown — working toward [goal]",
            "B": "hidden",
        },
        "metric": "feed_rating_avg",
        "page": "feed",
    },
    # ─── ORA CHAT ───
    "ora_greeting": {
        "variants": {
            "A": "Hey! I'm Ora. What do you want to work toward?",
            "B": "◈ What's on your mind?",
            "C": "Hey [name]. What are we working on today?",
            "D": "I've been thinking about your goals. Want to pick up where we left off?",
        },
        "metric": "conversation_length",
        "page": "ora",
    },
    "ora_response_length": {
        "variants": {
            "A": "concise — 1-3 sentences",
            "B": "standard — 3-5 sentences",
            "C": "rich — with bullet points when helpful",
        },
        "metric": "conversation_rating",
        "page": "ora",
    },
    "ora_proactive_suggestions": {
        "variants": {
            "A": "shown — Ora suggests next steps",
            "B": "hidden — pure Q&A",
        },
        "metric": "goal_set_rate",
        "page": "ora",
    },
    # ─── GOALS ───
    "goals_input_placeholder": {
        "variants": {
            "A": "I want to…",
            "B": "What do you want?",
            "C": "My goal is to…",
            "D": "Tell Ora what you want",
        },
        "metric": "goal_creation_rate",
        "page": "goals",
    },
    "goals_breakdown_timing": {
        "variants": {
            "A": "immediate — show breakdown as soon as goal set",
            "B": "delayed — show in feed as cards",
        },
        "metric": "goal_completion_rate",
        "page": "goals",
    },
    # ─── UPGRADE / PAYWALL ───
    "upgrade_headline": {
        "variants": {
            "A": "Unlock the full Ora",
            "B": "Go deeper with Explorer",
            "C": "You're using iDo like a power user",
            "D": "Explorer — built for people serious about their goals",
        },
        "metric": "upgrade_conversion_rate",
        "page": "upgrade",
    },
    "upgrade_price_display": {
        "variants": {
            "A": "$12.99/month",
            "B": "$0.43/day",
            "C": "Less than a coffee/month",
        },
        "metric": "upgrade_conversion_rate",
        "page": "upgrade",
    },
    "upgrade_cta_button": {
        "variants": {
            "A": "Upgrade to Explorer",
            "B": "Unlock Explorer — $12.99/mo",
            "C": "Try Explorer Free for 7 Days",
            "D": "Get Explorer",
        },
        "metric": "upgrade_click_rate",
        "page": "upgrade",
    },
    # ─── NAVIGATION ───
    "navbar_labels": {
        "variants": {
            "A": "Feed | Ora ◈ | Goals | DAO | Profile",
            "B": "Discover | Chat | Goals | Earn | Me",
            "C": "Home | Ora | Goals | DAO | You",
        },
        "metric": "nav_click_distribution",
        "page": "navbar",
    },
    # ─── REGISTRATION / LOGIN ───
    "register_headline": {
        "variants": {
            "A": "Create your account",
            "B": "Meet Ora",
            "C": "Start your journey",
            "D": "Join iDo",
        },
        "metric": "registration_completion_rate",
        "page": "auth",
    },
    "login_after_register_destination": {
        "variants": {
            "A": "home",
            "B": "onboarding",
            "C": "feed",
        },
        "metric": "day1_retention",
        "page": "auth",
    },
    # ─── DAO PAGE ───
    "dao_task_display": {
        "variants": {
            "A": "grid cards with CP reward prominent",
            "B": "list with difficulty badge prominent",
            "C": "kanban-style (Available / In Progress / Done)",
        },
        "metric": "task_claim_rate",
        "page": "dao",
    },
    "dao_first_cta": {
        "variants": {
            "A": "Claim a Task →",
            "B": "Earn your first CP →",
            "C": "Start Contributing →",
        },
        "metric": "first_contribution_rate",
        "page": "dao",
    },
    # ─── NOTIFICATIONS / TIMING ───
    "re_engagement_timing": {
        "variants": {
            "A": "after 3 days inactive",
            "B": "after 7 days inactive",
            "C": "daily if no session",
        },
        "metric": "reactivation_rate",
        "page": "system",
    },
    "upgrade_nudge_timing": {
        "variants": {
            "A": "after hitting daily limit",
            "B": "after 5th session",
            "C": "after first goal completion",
        },
        "metric": "upgrade_conversion_rate",
        "page": "system",
    },
}

# ─── Metric definitions ────────────────────────────────────────────────────────

AB_METRICS = {
    "signup_rate": "new user registrations / landing page views",
    "signup_click_rate": "CTA clicks / page views",
    "session_depth": "cards viewed or messages sent in session",
    "feed_rating_avg": "average card rating",
    "cards_per_session": "cards rated per session",
    "rating_completion_rate": "cards rated / cards shown",
    "conversation_length": "messages in Ora conversation",
    "goal_set_rate": "users who set a goal / users who saw goals page",
    "goal_creation_rate": "goals created / page visits",
    "day1_retention": "users who return within 24h",
    "day7_retention": "users who return within 7d",
    "upgrade_conversion_rate": "upgrades / users who saw paywall",
    "upgrade_click_rate": "upgrade CTA clicks / impressions",
    "task_claim_rate": "tasks claimed / users who saw DAO page",
    "first_contribution_rate": "first-time contributors / DAO page views",
    "reactivation_rate": "re-engagements / inactive users emailed",
    "nav_click_distribution": "clicks per nav item (diversity metric)",
    "registration_completion_rate": "completions / starts",
    "return_rate": "users who return after empty state",
    "goal_completion_rate": "goals marked complete / goals set",
    "conversation_rating": "post-conversation satisfaction (inferred from follow-up behavior)",
    "completion_rate": "onboarding completions / starts",
}


# ─── Helper: deterministic assignment ─────────────────────────────────────────

def _assign_variant(user_id: str, experiment: str, variants: list) -> str:
    """Deterministic, stable assignment: hash(user_id + experiment) % n_variants."""
    seed = f"{user_id}:{experiment}"
    hash_val = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    return variants[hash_val % len(variants)]


# ─── Helper: compute statistical confidence (two-proportion z-test) ───────────

def _compute_confidence(n1: int, c1: int, n2: int, c2: int) -> float:
    """Return 0-1 confidence that variant 2 outperforms variant 1."""
    if n1 < 30 or n2 < 30:
        return 0.0
    p1 = c1 / n1 if n1 > 0 else 0
    p2 = c2 / n2 if n2 > 0 else 0
    p_pool = (c1 + c2) / (n1 + n2) if (n1 + n2) > 0 else 0
    denom = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2)) if p_pool > 0 else 0
    if denom == 0:
        return 0.0
    z = (p2 - p1) / denom
    # Approximate CDF using erf
    confidence = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    return round(confidence, 4)


# ─── Old UI Surface endpoints (preserved) ─────────────────────────────────────

class UIEventBody(BaseModel):
    surface: str
    variant: str
    event_type: str
    value: float = 1.0


@router.post("/ui-event")
async def record_ui_event_endpoint(
    body: UIEventBody,
    user_id: str = Depends(get_current_user_id),
):
    """Record a UI interaction event for a surface/variant pair."""
    try:
        await record_ui_event(
            surface=body.surface,
            variant=body.variant,
            event_type=body.event_type,
            value=body.value,
        )
    except Exception as e:
        logger.warning(f"record_ui_event failed: {e}")
    return {"ok": True}


@router.get("/variant/{surface}")
async def get_variant_endpoint(
    surface: str,
    user_id: str = Depends(get_current_user_id),
):
    """Get the current A/B variant for a UI surface."""
    from ora.agents.ui_ab_testing import UI_TESTS

    cfg = UI_TESTS.get(surface)
    if not cfg:
        return {"variant": None}

    try:
        variant = await get_ui_variant(
            user_id=user_id,
            surface=surface,
            variants=cfg["variants"],
            weights=cfg.get("weights"),
        )
        return {"variant": variant}
    except Exception as e:
        logger.warning(f"get_variant failed for surface={surface}: {e}")
        return {"variant": None}


# ─── Old landing A/B (preserved) ─────────────────────────────────────────────

VARIANTS = ["A", "B", "C", "D"]
EVENT_LIST_MAX = 1000
VARIANT_TTL = 7 * 24 * 3600  # 7 days
ASSIGNMENT_TTL = 24 * 3600   # 24h for bulk assignments


class AssignBody(BaseModel):
    experiment_id: str


class EventBody(BaseModel):
    experiment_id: str
    variant: str
    event_type: str
    value: float = 1.0


@router.post("/assign")
async def assign_variant(
    body: AssignBody,
    user_id: str = Depends(get_current_user_id),
):
    """Assign (or retrieve cached) variant for a user+experiment."""
    redis = await get_redis()
    cache_key = f"ab:variant:{user_id}:{body.experiment_id}"
    try:
        cached = await redis.get(cache_key)
        if cached:
            return {"variant": cached.decode() if isinstance(cached, bytes) else cached}
    except Exception as e:
        logger.warning(f"Redis get failed for {cache_key}: {e}")

    try:
        hash_val = int(user_id[-8:], 16)
    except (ValueError, TypeError):
        hash_val = hash(user_id)
    variant = VARIANTS[abs(hash_val) % 4]

    try:
        await redis.setex(cache_key, VARIANT_TTL, variant)
    except Exception as e:
        logger.warning(f"Redis set failed for {cache_key}: {e}")

    return {"variant": variant}


@router.post("/event")
async def track_event(
    body: EventBody,
    user_id: str = Depends(get_current_user_id),
):
    """Append an engagement event. Supports both legacy experiment_id and new 'experiment' field."""
    redis = await get_redis()
    # Support new-style body fields too (experiment, event_type, value)
    exp_id = getattr(body, "experiment_id", None) or body.experiment_id
    list_key = f"ab:events:{exp_id}:{body.variant}"
    entry = json.dumps({
        "user_id": user_id,
        "event_type": body.event_type,
        "value": body.value,
    })
    try:
        await redis.rpush(list_key, entry)
        await redis.ltrim(list_key, -EVENT_LIST_MAX, -1)
    except Exception as e:
        logger.warning(f"Redis event append failed for {list_key}: {e}")
    return {"ok": True}


@router.get("/winner/{experiment_id}")
async def get_experiment_winner(experiment_id: str):
    """Return the server-side winner for an experiment."""
    redis = await get_redis()
    try:
        winner_raw = await redis.get(f"ab:winner:{experiment_id}")
        winner = winner_raw if isinstance(winner_raw, str) else (
            winner_raw.decode() if winner_raw else None
        )
        if winner and winner not in VARIANTS:
            winner = None
        return {"winner": winner}
    except Exception as e:
        logger.warning(f"get_experiment_winner failed for {experiment_id}: {e}")
        return {"winner": None}


@router.get("/results/{experiment_id}")
async def get_results(
    experiment_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Aggregate per-variant event counts by event_type (legacy endpoint)."""
    redis = await get_redis()
    results: dict = {}
    for variant in VARIANTS:
        list_key = f"ab:events:{experiment_id}:{variant}"
        try:
            raw_events = await redis.lrange(list_key, 0, -1)
        except Exception as e:
            logger.warning(f"Redis lrange failed for {list_key}: {e}")
            raw_events = []
        counts: dict = {}
        for raw in raw_events:
            try:
                ev = json.loads(raw)
                et = ev.get("event_type", "unknown")
                counts[et] = counts.get(et, 0) + 1
            except Exception:
                pass
        results[variant] = counts
    return {"experiment_id": experiment_id, "results": results}


@router.post("/set-winner/{experiment_id}")
async def set_winner(experiment_id: str, body: dict, user_id: str = Depends(get_current_user_id)):
    """Force a specific variant as the winner (admin only)."""
    r = await get_redis()
    winner = body.get("winner", "A")
    if r:
        await r.set(f"ab:winner:{experiment_id}", winner, ex=7 * 86400)
    return {"ok": True, "winner": winner}


@router.delete("/winner/{experiment_id}")
async def clear_winner(experiment_id: str, user_id: str = Depends(get_current_user_id)):
    """Clear forced winner — revert to random assignment."""
    r = await get_redis()
    if r:
        await r.delete(f"ab:winner:{experiment_id}")
    return {"ok": True}


# ─── NEW: Comprehensive experiment endpoints ───────────────────────────────────

@router.get("/experiments")
async def list_experiments():
    """Return all experiment definitions + current winner for each."""
    redis = await get_redis()
    output = {}
    for exp_name, exp_cfg in EXPERIMENT_REGISTRY.items():
        winner = None
        try:
            raw = await redis.get(f"ab:winner:{exp_name}")
            winner = raw.decode() if isinstance(raw, bytes) else raw
        except Exception:
            pass
        output[exp_name] = {
            **exp_cfg,
            "winner": winner,
            "metric_description": AB_METRICS.get(exp_cfg["metric"], ""),
        }
    return {"experiments": output, "count": len(output)}


@router.get("/assignment/{user_id}")
async def get_all_assignments(
    user_id: str,
    current_user: str = Depends(get_current_user_id),
):
    """
    Return all variant assignments for a user across ALL experiments.
    Winners override user-specific assignments.
    Cached in Redis for 24h.
    """
    redis = await get_redis()
    cache_key = f"ab:assignments:{user_id}"

    try:
        cached = await redis.get(cache_key)
        if cached:
            data = json.loads(cached.decode() if isinstance(cached, bytes) else cached)
            # Check for winner overrides (always fresh)
            for exp_name in EXPERIMENT_REGISTRY:
                winner_raw = await redis.get(f"ab:winner:{exp_name}")
                if winner_raw:
                    winner = winner_raw.decode() if isinstance(winner_raw, bytes) else winner_raw
                    data[exp_name] = winner
            return data
    except Exception as e:
        logger.warning(f"Redis cache read failed for {cache_key}: {e}")

    # Compute fresh assignments
    assignments: dict = {}
    for exp_name, exp_cfg in EXPERIMENT_REGISTRY.items():
        variants = list(exp_cfg["variants"].keys())
        # Check for declared winner first
        try:
            winner_raw = await redis.get(f"ab:winner:{exp_name}")
            if winner_raw:
                winner = winner_raw.decode() if isinstance(winner_raw, bytes) else winner_raw
                assignments[exp_name] = winner
                continue
        except Exception:
            pass
        # Deterministic assignment
        assignments[exp_name] = _assign_variant(user_id, exp_name, variants)

    # Cache for 24h
    try:
        await redis.setex(cache_key, ASSIGNMENT_TTL, json.dumps(assignments))
    except Exception as e:
        logger.warning(f"Redis cache write failed for {cache_key}: {e}")

    return assignments


@router.get("/results")
async def get_experiment_results(
    user_id: str = Depends(get_current_user_id),
):
    """
    Admin endpoint. For each experiment:
    - Per-variant: exposure count, conversion count, conversion rate, statistical confidence
    - Winner (if confidence > 95%)
    Returns all experiments ranked by statistical confidence.
    """
    redis = await get_redis()
    results = []

    for exp_name, exp_cfg in EXPERIMENT_REGISTRY.items():
        variants = list(exp_cfg["variants"].keys())
        winner_raw = await redis.get(f"ab:winner:{exp_name}") if redis else None
        declared_winner = (winner_raw.decode() if isinstance(winner_raw, bytes) else winner_raw) if winner_raw else None

        variant_stats = {}
        for variant in variants:
            list_key = f"ab:events:{exp_name}:{variant}"
            try:
                raw_events = await redis.lrange(list_key, 0, -1)
            except Exception:
                raw_events = []

            exposures = 0
            conversions = 0
            value_sum = 0.0
            for raw in raw_events:
                try:
                    ev = json.loads(raw)
                    et = ev.get("event_type", "")
                    val = float(ev.get("value", 1))
                    if et == "exposure":
                        exposures += 1
                    elif et in ("conversion", "click", "signup", "upgrade", "goal_set",
                                "goal_created", "claim", "registration_complete"):
                        conversions += 1
                        value_sum += val
                except Exception:
                    pass

            conv_rate = round(conversions / exposures, 4) if exposures > 0 else 0.0
            variant_stats[variant] = {
                "exposures": exposures,
                "conversions": conversions,
                "conversion_rate": conv_rate,
                "value_sum": round(value_sum, 2),
            }

        # Find best variant and compute confidence vs control (first variant)
        control_key = variants[0]
        ctrl = variant_stats[control_key]
        best_variant = control_key
        best_confidence = 0.0

        for variant in variants[1:]:
            vs = variant_stats[variant]
            conf = _compute_confidence(
                ctrl["exposures"], ctrl["conversions"],
                vs["exposures"], vs["conversions"],
            )
            variant_stats[variant]["confidence_vs_control"] = conf
            if conf > best_confidence:
                best_confidence = conf
                best_variant = variant
        variant_stats[control_key]["confidence_vs_control"] = None  # control has no comparison

        auto_winner = best_variant if best_confidence >= 0.95 else None

        results.append({
            "experiment": exp_name,
            "page": exp_cfg["page"],
            "metric": exp_cfg["metric"],
            "variants": variant_stats,
            "declared_winner": declared_winner,
            "auto_winner": auto_winner,
            "best_confidence": best_confidence,
            "recommendation": (
                f"Apply '{best_variant}' — {best_confidence*100:.1f}% confidence"
                if best_confidence >= 0.95
                else ("Insufficient data" if best_confidence < 0.5 else f"Trending toward '{best_variant}' ({best_confidence*100:.1f}%)")
            ),
        })

    # Sort by best_confidence descending
    results.sort(key=lambda x: x["best_confidence"], reverse=True)
    return {"results": results, "total": len(results)}


@router.post("/winner")
async def set_experiment_winner(
    body: dict,
    user_id: str = Depends(get_current_user_id),
):
    """
    Declare a winner for an experiment. After this, ALL users get the winner variant.
    Clears the user assignments cache so next load picks up the winner.
    """
    experiment = body.get("experiment")
    winner = body.get("winner")

    if not experiment or not winner:
        raise HTTPException(status_code=400, detail="experiment and winner are required")

    if experiment not in EXPERIMENT_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown experiment: {experiment}")

    variants = list(EXPERIMENT_REGISTRY[experiment]["variants"].keys())
    if winner not in variants:
        raise HTTPException(status_code=400, detail=f"Invalid winner '{winner}'. Valid variants: {variants}")

    redis = await get_redis()
    try:
        await redis.set(f"ab:winner:{experiment}", winner, ex=90 * 86400)  # 90 days
        # Clear all cached assignment blobs so they are recomputed with the winner
        # (pattern-based delete — best-effort)
        async for key in redis.scan_iter("ab:assignments:*"):
            await redis.delete(key)
    except Exception as e:
        logger.warning(f"set_experiment_winner Redis error: {e}")

    logger.info(f"Experiment winner set: {experiment} → {winner} (by user {user_id[:8]})")
    return {"ok": True, "experiment": experiment, "winner": winner}


# ─── Evolutionary A/B Experiments (DB-backed) ─────────────────────────────────

from ora.agents.experiment_generator import ExperimentGeneratorAgent
from core.database import fetchrow as db_fetchrow, fetch as db_fetch, execute as db_execute


@router.get("/experiments")
async def list_experiments():
    """All experiments from DB (evolutionary registry)."""
    try:
        rows = await db_fetch("SELECT * FROM ab_experiments ORDER BY created_at DESC LIMIT 100")
        return {"experiments": [dict(r) for r in rows]}
    except Exception as e:
        # Fall back gracefully if DB not ready
        return {"experiments": [], "error": str(e)}


@router.post("/experiments")
async def create_experiment_endpoint(
    body: dict,
    user_id: str = Depends(get_current_user_id),
):
    """Dynamically create a new experiment (admin only)."""
    user = await db_fetchrow("SELECT is_admin FROM users WHERE id = $1", str(user_id))
    if not user or not user.get('is_admin'):
        raise HTTPException(403, "Admin required")

    generator = ExperimentGeneratorAgent()
    exp_id = await generator._create_experiment(
        name=body.get("name"),
        page=body.get("page", "global"),
        metric=body.get("metric", "engagement_rate"),
        variants=body.get("variants", {}),
        source="manual"
    )
    return {"experiment_id": exp_id}


@router.get("/experiments/{experiment_id}/evolution")
async def get_evolution_tree(experiment_id: str):
    """Show experiment lineage — walk the parent chain."""
    chain = []
    current_id = experiment_id
    for _ in range(10):  # max 10 generations
        exp = await db_fetchrow(
            "SELECT id, name, winner, generation, parent_experiment FROM ab_experiments WHERE id = $1",
            current_id
        )
        if not exp:
            break
        chain.append(dict(exp))
        if not exp['parent_experiment']:
            break
        current_id = exp['parent_experiment']
    return {"evolution_chain": list(reversed(chain))}


@router.get("/insights")
async def get_learned_insights(user_id: str = Depends(get_current_user_id)):
    """Cross-experiment learned patterns (admin only)."""
    user = await db_fetchrow("SELECT is_admin FROM users WHERE id = $1", str(user_id))
    if not user or not user.get('is_admin'):
        raise HTTPException(403, "Admin required")

    concluded = await db_fetch("""
        SELECT name, winner, variants, metric, generation
        FROM ab_experiments WHERE status = 'concluded' AND winner IS NOT NULL
        ORDER BY concluded_at DESC LIMIT 50
    """)

    active = await db_fetch("SELECT COUNT(*) as cnt FROM ab_experiments WHERE status = 'active'")
    generations = await db_fetch("SELECT MAX(generation) as max_gen FROM ab_experiments")

    return {
        "total_concluded": len(concluded),
        "active_experiments": active[0]['cnt'] if active else 0,
        "max_generation_reached": generations[0]['max_gen'] if generations else 0,
        "recent_winners": [
            {"name": r['name'], "winner": r['winner'], "metric": r['metric']}
            for r in concluded[:10]
        ]
    }
