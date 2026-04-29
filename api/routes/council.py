"""
Connectome Executive Council API routes.

POST /api/ora/council/consult — ask council members for decision advice
GET  /api/ora/council/members — council member bios
GET  /api/ora/council/last-brief — latest weekly brief
POST /api/ora/council/run-weekly-brief — cron/admin trigger for weekly brief
"""

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from pydantic import BaseModel, Field

from core.config import settings
from core.database import fetchrow
from uuid import UUID

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ora/council", tags=["ora_council"])

ADMIN_EMAILS = {"avi@atdao.org", "nea@atdao.org", "carlosandromeda8@gmail.com"}
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "connectome-admin-secret")
optional_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/users/login", auto_error=False)


class CouncilConsultRequest(BaseModel):
    proposal: str = Field(..., min_length=1)
    context: str = ""
    members: Optional[List[str]] = None


async def _require_admin(
    request: Request,
    x_admin_token: Optional[str] = Header(default=None, alias="x-admin-token"),
    token: Optional[str] = Depends(optional_oauth2_scheme),
) -> str:
    """Allow admin users by email OR valid X-Admin-Token header for crons."""
    if x_admin_token and x_admin_token == ADMIN_TOKEN:
        return "admin-token"
    user_id = _decode_optional_token(token)
    if user_id:
        row = await fetchrow("SELECT email FROM users WHERE id = $1", UUID(user_id))
        if row and row["email"] in ADMIN_EMAILS:
            return user_id
    raise HTTPException(status_code=403, detail="Admin access required")


def _decode_optional_token(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


@router.post("/consult")
async def consult(
    body: CouncilConsultRequest,
    _: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """Get one or more council members' perspectives on a proposal."""
    try:
        from ora.agents.executive_council import consult_council
        from ora.brain import get_brain

        brain = get_brain()
        perspectives = await consult_council(
            proposal=body.proposal,
            context=body.context,
            members=body.members,
            brain=brain,
        )
        summary = await _summarize_perspectives(body.proposal, perspectives, brain=brain)
        return {"perspectives": perspectives, "summary": summary}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Council consultation failed")
        raise HTTPException(status_code=500, detail=f"Council consultation failed: {e}")


@router.get("/members")
async def members() -> Dict[str, Any]:
    """Return council member bios."""
    from ora.agents.executive_council import get_council_members

    return {"members": get_council_members()}


@router.get("/last-brief")
async def last_brief(
    _: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """Return the most recent generated weekly council brief."""
    from ora.agents.executive_council import load_last_council_brief

    return await load_last_council_brief()


@router.post("/run-weekly-brief")
async def run_weekly_brief(
    _: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """Cron/admin trigger for the weekly council brief."""
    try:
        from ora.agents.executive_council import run_weekly_council_brief
        from ora.brain import get_brain

        brief = await run_weekly_council_brief(brain=get_brain())
        latest = await _load_latest_brief_metadata()
        return {"ok": True, "brief": brief, "generated_at": latest.get("generated_at")}
    except Exception as e:
        logger.exception("Weekly council brief failed")
        raise HTTPException(status_code=500, detail=f"Weekly council brief failed: {e}")


async def _summarize_perspectives(proposal: str, perspectives: Dict[str, str], brain=None) -> str:
    client = getattr(brain, "_openai", None) if brain is not None else None
    if not client:
        return "Council perspectives generated. Review member-specific advice for tradeoffs and next actions."
    try:
        response = await client.chat.completions.create(
            model=os.getenv("COUNCIL_MODEL", "gpt-4o-mini"),
            messages=[{
                "role": "user",
                "content": (
                    "Summarize the Connectome Executive Council's advice in 2-3 direct sentences. "
                    "Call out consensus, tension, and the recommended next move.\n\n"
                    f"PROPOSAL: {proposal}\n\nPERSPECTIVES:\n{perspectives}"
                ),
            }],
            temperature=0.35,
            max_tokens=220,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("Council summary failed: %s", e)
        return "Council perspectives generated. Review member-specific advice for tradeoffs and next actions."


async def _load_latest_brief_metadata() -> Dict[str, Any]:
    from ora.agents.executive_council import load_last_council_brief

    return await load_last_council_brief()
