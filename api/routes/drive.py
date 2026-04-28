"""
Drive API Routes
================
Endpoints for Google Drive indexing and semantic search.

  POST /api/drive/sync          — trigger Drive scan + embedding (admin only)
  GET  /api/drive/search?q=...  — semantic search over the caller's indexed docs
  GET  /api/drive/status        — indexing stats for the caller's docs

PRIVACY: All endpoints require a valid JWT Bearer token.
         Search and status results are ALWAYS scoped to the authenticated user's
         own documents via owner_user_id. No cross-user data leakage is possible.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from api.middleware import get_current_user_id
from core.database import fetchrow
from ora.brain import get_brain

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/drive", tags=["drive"])


def _get_drive_agent():
    """Return DriveAgent from the live Ora brain."""
    try:
        brain = get_brain()
        return brain.drive_agent
    except Exception as e:
        logger.warning(f"Drive: could not get brain drive_agent: {e}")
        return None


async def _require_admin(user_id: str = Depends(get_current_user_id)) -> str:
    """
    FastAPI dependency: only admin users may trigger Drive sync.
    Admin is indicated by profile->>'is_admin' = 'true'.
    """
    row = await fetchrow(
        "SELECT profile FROM users WHERE id = $1",
        UUID(user_id),
    )
    if not row or not row["profile"].get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Drive sync is restricted to admin accounts",
        )
    return user_id


@router.post("/sync")
async def sync_drive(
    max_files: int = Query(default=50, ge=1, le=200),
    user_id: str = Depends(_require_admin),
):
    """
    Trigger a Google Drive sync (admin only).
    Scans Drive, extracts content, creates embeddings, stores in pgvector.
    All indexed documents are tagged with the caller's user_id as owner.
    Returns a summary of what was indexed.
    """
    agent = _get_drive_agent()
    if agent is None:
        raise HTTPException(
            status_code=503,
            detail="Drive agent not available — Ora brain may not be initialized",
        )

    try:
        summary = await agent.sync(max_files=max_files, owner_user_id=user_id)
        return {"ok": True, "sync": summary}
    except Exception as e:
        logger.error(f"Drive sync failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Drive sync error: {e}")


@router.get("/search")
async def search_drive(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(default=5, ge=1, le=20),
    min_similarity: float = Query(default=0.70, ge=0.0, le=1.0),
    user_id: str = Depends(get_current_user_id),
):
    """
    Semantic search over the caller's indexed Google Drive documents.
    Results are always scoped to owner_user_id = current user. No cross-user access.
    """
    agent = _get_drive_agent()
    if agent is None:
        return {"ok": True, "results": [], "count": 0, "message": "Drive agent not available"}

    try:
        results = await agent.semantic_search(
            query=q,
            owner_user_id=user_id,
            limit=limit,
            min_similarity=min_similarity,
        )
        return {"ok": True, "results": results, "count": len(results)}
    except Exception as e:
        logger.error(f"Drive search failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Drive search error: {e}")


@router.get("/status")
async def drive_status(
    user_id: str = Depends(get_current_user_id),
):
    """
    Return Drive indexing status for the authenticated user only.
    Document counts and sync times reflect only the caller's own documents.
    """
    agent = _get_drive_agent()
    if agent is None:
        return {"ok": True, "indexed_documents": 0, "last_sync": None, "status": "agent_unavailable"}

    try:
        stat = await agent.status(owner_user_id=user_id)
        return {"ok": True, **stat}
    except Exception as e:
        logger.warning(f"Drive status failed: {e}")
        return {"ok": True, "indexed_documents": 0, "last_sync": None, "status": "error", "error": str(e)}
