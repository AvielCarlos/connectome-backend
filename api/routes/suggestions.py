"""
Suggestions Routes

GET  /api/suggestions         — list recent community suggestions
POST /api/suggestions         — submit a new suggestion (content + category)
POST /api/suggestions/{id}/vote — upvote a suggestion
"""

import json
import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.middleware import get_current_user_id
from core.database import fetch, fetchrow, execute, fetchval

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/suggestions", tags=["suggestions"])



INTEGRATION_AWARD_CP_DEFAULT = 200
INTEGRATION_AWARDABLE_STATUSES = {"adopted", "implemented"}
AUTO_QUEUE_CATEGORIES = {"Bug", "Malfunction", "Bad Card/Node", "Confusing", "Idea", "Design"}


class SuggestionCreate(BaseModel):
    content: Optional[str] = None
    title: Optional[str] = None   # legacy field alias
    body: Optional[str] = None    # legacy field alias
    category: Optional[str] = "general"

    def get_content(self) -> str:
        return self.content or self.body or self.title or ""


class SuggestionAutomationRun(BaseModel):
    import_app_feedback: bool = True
    auto_queue_low_risk: bool = True
    award_implemented: bool = True
    limit: int = 50


class SuggestionIntegrationUpdate(BaseModel):
    status: str  # accepted | adopted | implemented | rejected | pending
    reference_url: Optional[str] = None
    note: Optional[str] = None
    cp_award: Optional[int] = None


async def _ensure_suggestion_automation_schema() -> None:
    """Idempotent columns for app-feedback -> suggestion -> CP integration."""
    await execute("ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS content TEXT")
    await execute("ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS body TEXT")
    await execute("ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'manual'")
    await execute("ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS source_id TEXT")
    await execute("ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS integration_status TEXT DEFAULT 'pending'")
    await execute("ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS integration_reference TEXT")
    await execute("ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS triage_metadata JSONB DEFAULT '{}'::jsonb")
    await execute("ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS adopted_cp_awarded INTEGER DEFAULT 0")
    await execute("ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS adopted_at TIMESTAMPTZ")
    await execute("CREATE INDEX IF NOT EXISTS idx_user_suggestions_integration_status ON user_suggestions(integration_status)")
    await execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_user_suggestions_source_unique
        ON user_suggestions(source, source_id)
        WHERE source IS NOT NULL AND source_id IS NOT NULL
        """
    )


async def _require_suggestion_admin(user_id: str) -> None:
    caller_row = await fetchrow("SELECT email, profile FROM users WHERE id = $1", UUID(user_id))
    caller = dict(caller_row) if caller_row else None
    profile = caller.get("profile") if caller else {}
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except Exception:
            profile = {}
    is_admin = bool(
        caller
        and (
            (isinstance(profile, dict) and profile.get("is_admin"))
            or (caller.get("email") or "").lower() == "carlosandromeda8@gmail.com"
        )
    )
    if not is_admin:
        raise HTTPException(status_code=403, detail="Only admins can run suggestion integration automation")


def _feedback_to_suggestion_kind(category: str, message: str) -> str:
    if category in {"Bug", "Malfunction"}:
        return "bug_fix_candidate"
    if category in {"Bad Card/Node", "Confusing"}:
        return "ioo_graph_correction"
    if category == "Design":
        return "design_improvement"
    if category == "Idea":
        return "product_suggestion"
    if category == "Praise":
        return "positive_signal"
    return "general_feedback"


def _short_title(category: str, message: str) -> str:
    cleaned = " ".join((message or "").split())
    if len(cleaned) > 82:
        cleaned = cleaned[:79].rstrip() + "…"
    return f"{category}: {cleaned or 'feedback'}"


async def _award_suggestion_integration_cp(row: Any, *, amount: int, reason: str, reference: Optional[str]) -> Optional[dict]:
    row_data = dict(row) if not isinstance(row, dict) else row
    user_id_raw = row_data["user_id"]
    suggestion_id = str(row_data["id"])
    try:
        user_uuid = UUID(str(user_id_raw))
    except Exception:
        logger.warning("Skipping suggestion CP award for non-UUID user_id=%s suggestion=%s", user_id_raw, suggestion_id)
        return None

    reference_id = f"suggestion:{suggestion_id}:integration"
    duplicate = await fetchval(
        "SELECT COUNT(*) FROM cp_transactions WHERE user_id = $1 AND reference_id = $2 AND amount > 0",
        user_uuid,
        reference_id,
    )
    if int(duplicate or 0) > 0:
        return {"suggestion_id": suggestion_id, "cp_awarded": 0, "skipped": "already_awarded"}

    await execute(
        """
        INSERT INTO user_cp_balance (user_id, cp_balance, total_cp_earned, last_updated)
        VALUES ($1, $2, $2, NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            cp_balance = user_cp_balance.cp_balance + $2,
            total_cp_earned = user_cp_balance.total_cp_earned + $2,
            last_updated = NOW()
        """,
        user_uuid,
        amount,
    )
    await execute(
        """
        INSERT INTO cp_transactions (user_id, amount, reason, reference_id, created_at)
        VALUES ($1, $2, $3, $4, NOW())
        """,
        user_uuid,
        amount,
        f"suggestion_integration: {reason}" + (f" | {reference}" if reference else ""),
        reference_id,
    )
    await execute(
        """
        UPDATE user_suggestions
        SET adopted_cp_awarded = COALESCE(adopted_cp_awarded, 0) + $1,
            adopted_at = COALESCE(adopted_at, NOW()),
            updated_at = NOW()
        WHERE id = $2::uuid
        """,
        amount,
        suggestion_id,
    )
    return {"suggestion_id": suggestion_id, "cp_awarded": amount, "reference_id": reference_id}


@router.get("")
async def list_suggestions(limit: int = 20) -> List[Dict[str, Any]]:
    """Return recent community suggestions ordered by vote count."""
    try:
        await _ensure_suggestion_automation_schema()
        rows = await fetch(
            """
            SELECT id, COALESCE(content, body, title) AS content, category, status, vote_count, cp_earned,
                   integration_status, integration_reference, adopted_cp_awarded, created_at
            FROM user_suggestions
            ORDER BY vote_count DESC, created_at DESC
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"Suggestions list failed: {e}")
        return []


@router.post("")
async def create_suggestion(
    payload: SuggestionCreate,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Submit a new community suggestion. Awards 10 CP per submission."""
    content = payload.get_content()
    if not content:
        raise HTTPException(status_code=422, detail="content is required")
    try:
        await _ensure_suggestion_automation_schema()
        row = await fetchrow(
            """
            INSERT INTO user_suggestions (user_id, title, content, body, category, status, vote_count, cp_earned, source, integration_status)
            VALUES ($1, $2, $2, $2, $3, 'pending', 0, 10, 'manual', 'pending')
            RETURNING id, content, category, status, cp_earned, integration_status, created_at
            """,
            str(UUID(user_id)),
            content,
            payload.category or "general",
        )
        if not row:
            raise HTTPException(status_code=500, detail="Could not create suggestion")

        # Credit CP to user_cp_balance
        cp_earned = int(row["cp_earned"] or 10)
        suggestion_id = str(row["id"])
        try:
            await execute(
                """
                INSERT INTO user_cp_balance (user_id, cp_balance, total_cp_earned, last_updated)
                VALUES ($1, $2, $2, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    cp_balance = user_cp_balance.cp_balance + $2,
                    total_cp_earned = user_cp_balance.total_cp_earned + $2,
                    last_updated = NOW()
                """,
                UUID(user_id), cp_earned
            )
        except Exception as _cp_err:
            logger.warning(f"CP credit failed (non-fatal): {_cp_err}")

        # Record in cp_transactions ledger
        try:
            await execute(
                """
                INSERT INTO cp_transactions (user_id, amount, reason, reference_id)
                VALUES ($1, $2, 'suggestion', $3)
                """,
                UUID(user_id), cp_earned, suggestion_id,
            )
        except Exception as _tx_err:
            logger.warning(f"CP transaction ledger write failed (non-fatal): {_tx_err}")

        # Get updated totals
        cp_row = await fetchrow(
            "SELECT cp_balance, total_cp_earned FROM user_cp_balance WHERE user_id = $1",
            UUID(user_id)
        )
        result = dict(row)
        result["suggestion"] = result["content"]
        result["cp_earned"] = cp_earned
        result["total_dao_cp"] = int(cp_row["total_cp_earned"] or 0) if cp_row else cp_earned
        result["cp_balance"] = int(cp_row["cp_balance"] or 0) if cp_row else cp_earned
        result["message"] = f"Earned {cp_earned} CP! Total: {result['total_dao_cp']} CP"
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Suggestion create failed: {e}")
        raise HTTPException(status_code=500, detail="Could not create suggestion")


@router.post("/{suggestion_id}/vote")
async def vote_suggestion(
    suggestion_id: str,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Upvote a suggestion."""
    try:
        row = await fetchrow(
            """
            UPDATE user_suggestions
            SET vote_count = vote_count + 1
            WHERE id = $1
            RETURNING id, content, vote_count
            """,
            UUID(suggestion_id),
        )
        if not row:
            raise HTTPException(status_code=404, detail="Suggestion not found")
        return dict(row)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Suggestion vote failed: {e}")
        raise HTTPException(status_code=500, detail="Could not vote")


@router.get("/mine")
async def get_my_suggestions(
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Get the current user's suggestions and CP balance."""
    try:
        await _ensure_suggestion_automation_schema()
        suggestions = await fetch(
            """
            SELECT id, COALESCE(content, body, title) AS content, category, status, vote_count, cp_earned,
                   integration_status, integration_reference, adopted_cp_awarded, created_at
            FROM user_suggestions
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT 50
            """,
            str(UUID(user_id)),
        )
        cp_row = await fetchrow(
            "SELECT cp_balance, total_cp_earned FROM user_cp_balance WHERE user_id = $1",
            UUID(user_id),
        )
        return {
            "suggestions": [dict(r) for r in suggestions],
            "total_suggestions": len(suggestions),
            "total_cp_earned": int(cp_row["total_cp_earned"] or 0) if cp_row else 0,
            "total_dao_cp": int(cp_row["total_cp_earned"] or 0) if cp_row else 0,
            "cp_balance": int(cp_row["cp_balance"] or 0) if cp_row else 0,
            "tier": "contributor" if (cp_row and int(cp_row["total_cp_earned"] or 0) >= 100) else "observer",
        }
    except Exception as e:
        logger.error(f"Get my suggestions failed: {e}")
        return {"suggestions": [], "total_suggestions": 0, "total_cp_earned": 0, "total_dao_cp": 0, "cp_balance": 0, "tier": "observer"}


async def process_suggestion_automation(body: SuggestionAutomationRun) -> Dict[str, Any]:
    """Import app feedback into the suggestion queue and award implemented/adopted suggestion CP.

    This is intentionally conservative: automation may queue/accept suggestions
    for integration, but extra CP is paid only when a suggestion is explicitly
    adopted or implemented. Submission CP remains handled by the feedback and
    suggestion creation endpoints.
    """
    await _ensure_suggestion_automation_schema()
    imported: list[dict[str, Any]] = []
    queued: list[str] = []
    awards: list[dict[str, Any]] = []
    limit = max(1, min(int(body.limit or 50), 200))

    if body.import_app_feedback:
        rows = await fetch(
            """
            SELECT af.id, af.user_id, af.category, af.message, af.route, af.metadata, af.created_at
            FROM app_feedback af
            LEFT JOIN user_suggestions us
              ON us.source = 'app_feedback' AND us.source_id = af.id::text
            WHERE us.id IS NULL
              AND af.message IS NOT NULL
              AND length(trim(af.message)) >= 3
            ORDER BY af.created_at ASC
            LIMIT $1
            """,
            limit,
        )
        for feedback in rows:
            category = feedback["category"] or "Other"
            message = feedback["message"] or ""
            kind = _feedback_to_suggestion_kind(category, message)
            integration_status = "queued" if body.auto_queue_low_risk and category in AUTO_QUEUE_CATEGORIES else "pending"
            status = "accepted" if integration_status == "queued" else "pending"
            metadata = feedback["metadata"] or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except Exception:
                    metadata = {}
            triage = {
                "kind": kind,
                "route": feedback["route"],
                "source": "app_feedback",
                "source_id": str(feedback["id"]),
                "auto_queued": integration_status == "queued",
                "feedback_metadata": metadata,
            }
            try:
                row = await fetchrow(
                    """
                    INSERT INTO user_suggestions (
                        user_id, title, content, body, category, status, vote_count, cp_earned,
                        source, source_id, integration_status, triage_metadata, updated_at
                    )
                    VALUES ($1, $2, $3, $3, $4, $5, 0, 0, 'app_feedback', $6, $7, $8::jsonb, NOW())
                    ON CONFLICT (source, source_id) WHERE source IS NOT NULL AND source_id IS NOT NULL
                    DO UPDATE SET updated_at = NOW()
                    RETURNING id, status, integration_status
                    """,
                    str(feedback["user_id"]),
                    _short_title(category, message),
                    message,
                    category,
                    status,
                    str(feedback["id"]),
                    integration_status,
                    json.dumps(triage),
                )
                if row:
                    imported.append({"suggestion_id": str(row["id"]), "feedback_id": str(feedback["id"]), "status": row["status"], "integration_status": row["integration_status"], "kind": kind})
                    if integration_status == "queued":
                        queued.append(str(row["id"]))
            except Exception as exc:
                logger.warning("Feedback -> suggestion import skipped for %s: %s", feedback["id"], exc)

    if body.auto_queue_low_risk:
        queue_rows = await fetch(
            """
            UPDATE user_suggestions
            SET status = 'accepted',
                integration_status = 'queued',
                triage_metadata = COALESCE(triage_metadata, '{}'::jsonb)
                  || jsonb_build_object('auto_queued', true, 'queued_by', 'suggestion_automation'),
                updated_at = NOW()
            WHERE id IN (
                SELECT id FROM user_suggestions
                WHERE status IN ('pending', 'new')
                  AND COALESCE(integration_status, 'pending') IN ('pending', 'new')
                  AND category = ANY($1::text[])
                ORDER BY created_at ASC
                LIMIT $2
            )
            RETURNING id
            """,
            list(AUTO_QUEUE_CATEGORIES),
            limit,
        )
        for row in queue_rows:
            sid = str(row["id"])
            if sid not in queued:
                queued.append(sid)

    if body.award_implemented:
        rows = await fetch(
            """
            SELECT id, user_id, COALESCE(content, body, title) AS content, category, integration_status, integration_reference
            FROM user_suggestions
            WHERE integration_status = ANY($1::text[])
              AND COALESCE(adopted_cp_awarded, 0) = 0
            ORDER BY updated_at ASC
            LIMIT $2
            """,
            list(INTEGRATION_AWARDABLE_STATUSES),
            limit,
        )
        for suggestion in rows:
            reason = f"{suggestion['integration_status']} suggestion: {_short_title(suggestion['category'] or 'Suggestion', suggestion['content'] or '')}"
            result = await _award_suggestion_integration_cp(
                suggestion,
                amount=INTEGRATION_AWARD_CP_DEFAULT,
                reason=reason,
                reference=suggestion["integration_reference"],
            )
            if result:
                awards.append(result)

    return {
        "ok": True,
        "imported_count": len(imported),
        "queued_count": len(queued),
        "awards_count": len([a for a in awards if a.get("cp_awarded")]),
        "imported": imported,
        "queued": queued,
        "awards": awards,
    }


@router.post("/automation/run")
async def run_suggestion_automation(
    body: SuggestionAutomationRun,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    await _require_suggestion_admin(user_id)
    return await process_suggestion_automation(body)


@router.post("/{suggestion_id}/integrate")
async def integrate_suggestion(
    suggestion_id: str,
    body: SuggestionIntegrationUpdate,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Mark a suggestion as accepted/adopted/implemented and auto-award final CP when appropriate."""
    await _require_suggestion_admin(user_id)
    await _ensure_suggestion_automation_schema()
    status = body.status.lower().strip()
    allowed = {"pending", "accepted", "queued", "adopted", "implemented", "rejected"}
    if status not in allowed:
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(allowed)}")
    public_status = "accepted" if status in {"queued", "adopted", "implemented"} else status
    metadata_patch = {"integration_note": body.note, "updated_by": "suggestion_integration_api"}
    row = await fetchrow(
        """
        UPDATE user_suggestions
        SET status = $1,
            integration_status = $2,
            integration_reference = COALESCE($3, integration_reference),
            triage_metadata = COALESCE(triage_metadata, '{}'::jsonb) || $4::jsonb,
            adopted_at = CASE WHEN $2 = ANY($5::text[]) THEN COALESCE(adopted_at, NOW()) ELSE adopted_at END,
            updated_at = NOW()
        WHERE id = $6::uuid
        RETURNING id, user_id, COALESCE(content, body, title) AS content, category, status, integration_status, integration_reference, adopted_cp_awarded
        """,
        public_status,
        status,
        body.reference_url,
        json.dumps(metadata_patch),
        list(INTEGRATION_AWARDABLE_STATUSES),
        suggestion_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    award = None
    if status in INTEGRATION_AWARDABLE_STATUSES and int(row["adopted_cp_awarded"] or 0) == 0:
        amount = body.cp_award or INTEGRATION_AWARD_CP_DEFAULT
        if amount < 50 or amount > 600:
            raise HTTPException(status_code=422, detail="suggestion integration CP must be between 50 and 600")
        reason = body.note or f"{status} suggestion: {_short_title(row['category'] or 'Suggestion', row['content'] or '')}"
        award = await _award_suggestion_integration_cp(row, amount=amount, reason=reason, reference=body.reference_url or row["integration_reference"])

    return {"ok": True, "suggestion": dict(row), "award": award}
