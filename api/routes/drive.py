"""
Drive API Routes
================
Endpoints for Google Drive indexing and semantic search.

  POST /api/drive/sync          — trigger Drive scan + embedding
  GET  /api/drive/search?q=...  — semantic search over indexed docs
  GET  /api/drive/status        — indexing stats
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from api.middleware import get_current_user_id
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


@router.post("/sync")
async def sync_drive(
    max_files: int = Query(default=50, ge=1, le=200),
    user_id: str = Depends(get_current_user_id),
):
    """
    Trigger a Google Drive sync.
    Scans Drive, extracts content, creates embeddings, stores in pgvector.
    Returns a summary of what was indexed.
    """
    agent = _get_drive_agent()
    if agent is None:
        raise HTTPException(
            status_code=503,
            detail="Drive agent not available — Ora brain may not be initialized",
        )

    try:
        summary = await agent.sync(max_files=max_files)
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
    Semantic search over indexed Google Drive documents.
    Returns documents sorted by relevance with an excerpt.
    """
    agent = _get_drive_agent()
    if agent is None:
        return {"ok": True, "results": [], "count": 0, "message": "Drive agent not available"}

    try:
        results = await agent.semantic_search(
            query=q, limit=limit, min_similarity=min_similarity
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
    Return Drive indexing status: document count, last sync time.
    """
    agent = _get_drive_agent()
    if agent is None:
        return {"ok": True, "indexed_documents": 0, "last_sync": None, "status": "agent_unavailable"}

    try:
        stat = await agent.status()
        return {"ok": True, **stat}
    except Exception as e:
        logger.warning(f"Drive status failed: {e}")
        return {"ok": True, "indexed_documents": 0, "last_sync": None, "status": "error", "error": str(e)}
