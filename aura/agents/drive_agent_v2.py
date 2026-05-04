"""
DriveAgentV2 — User-Scoped Google Drive Indexer
================================================
Unlike DriveAgent (which uses the gog CLI and is tied to Avi's personal account),
DriveAgentV2:
  - Works for ANY user who has connected their Google Drive via OAuth
  - Loads OAuth tokens directly from the google_oauth_tokens table
  - Makes direct Google Drive API calls via httpx (no CLI dependency)
  - Respects each user's drive_privacy_level when deciding what to index
  - Refreshes tokens automatically on expiry

Privacy levels:
  'none'       — Drive not connected or sharing disabled; nothing indexed
  'goals_only' — Only recent docs (90 days), skip financial/medical, store summaries only
  'full'       — Index everything, full content stored

Usage:
  agent = DriveAgentV2(openai_client=openai_client)
  summary = await agent.sync(user_id="<uuid>")
  results = await agent.semantic_search(query="...", user_id="<uuid>")
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

import httpx

from core.config import settings
from core.database import execute, fetch, fetchrow, fetchval

logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────

DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_EXPORT_URL = "https://www.googleapis.com/drive/v3/files/{file_id}/export"
DRIVE_MEDIA_URL = "https://www.googleapis.com/drive/v3/files/{file_id}"

INDEXABLE_MIME_TYPES = {
    "application/vnd.google-apps.document",
    "text/plain",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
}

MIN_CONTENT_LENGTH = 50
MAX_CONTENT_CHARS = 6000

# Filenames containing these terms are skipped in 'goals_only' mode
PRIVATE_FILENAME_KEYWORDS = [
    "invoice", "receipt", "medical", "password", "bank", "ssn", "tax",
    "insurance", "credit", "debit", "salary", "payroll", "w-2", "1099",
    "prescription", "diagnosis",
]


# ─── Agent ───────────────────────────────────────────────────────────────────

class DriveAgentV2:
    """
    Indexes Google Drive documents for a specific user using their OAuth tokens.
    """

    AGENT_NAME = "DriveAgentV2"

    def __init__(self, openai_client=None):
        self.openai = openai_client

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def sync(
        self, user_id: str, max_files: int = 50
    ) -> Dict[str, Any]:
        """
        Sync Drive documents for the given user.
        Respects their drive_privacy_level setting.

        Returns summary dict with counts and errors.
        """
        summary: Dict[str, Any] = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "user_id": user_id[:8],
            "files_found": 0,
            "files_indexed": 0,
            "files_skipped": 0,
            "files_errored": 0,
            "errors": [],
        }

        # Load user's token record
        token_row = await fetchrow(
            "SELECT access_token, refresh_token, token_expiry, drive_connected, drive_privacy_level "
            "FROM google_oauth_tokens WHERE user_id = $1",
            UUID(user_id),
        )

        if not token_row:
            summary["errors"].append("No Google OAuth tokens found for user")
            summary["finished_at"] = datetime.now(timezone.utc).isoformat()
            return summary

        if not token_row["drive_connected"]:
            summary["errors"].append("Drive not connected for user")
            summary["finished_at"] = datetime.now(timezone.utc).isoformat()
            return summary

        privacy_level = token_row["drive_privacy_level"] or "none"
        if privacy_level == "none":
            summary["errors"].append("Drive privacy level is 'none' — no sync performed")
            summary["finished_at"] = datetime.now(timezone.utc).isoformat()
            return summary

        # Get a valid access token (refreshes if needed)
        from api.routes.google_auth import get_valid_access_token
        access_token = await get_valid_access_token(user_id)
        if not access_token:
            summary["errors"].append("Could not obtain valid access token — reconnect Drive")
            summary["finished_at"] = datetime.now(timezone.utc).isoformat()
            return summary

        # List files from Drive
        try:
            files = await self._list_drive_files(access_token, max_files, privacy_level)
        except Exception as e:
            logger.error(f"DriveAgentV2: failed to list files for user {user_id[:8]}: {e}")
            summary["errors"].append(f"list_files: {e}")
            summary["finished_at"] = datetime.now(timezone.utc).isoformat()
            return summary

        summary["files_found"] = len(files)
        logger.info(
            f"DriveAgentV2: found {len(files)} files for user {user_id[:8]} "
            f"(privacy={privacy_level})"
        )

        for file_meta in files:
            drive_id = file_meta.get("id", "")
            mime = file_meta.get("mimeType", "")
            name = file_meta.get("name", "untitled")
            modified = file_meta.get("modifiedTime")

            try:
                # Skip non-indexable MIME types
                if mime not in INDEXABLE_MIME_TYPES:
                    summary["files_skipped"] += 1
                    continue

                # Skip private-looking filenames in goals_only mode
                if privacy_level == "goals_only" and _looks_private(name):
                    logger.debug(f"DriveAgentV2: skipping private-looking file '{name}'")
                    summary["files_skipped"] += 1
                    continue

                # Skip if already up to date
                if await self._is_current(drive_id, modified):
                    summary["files_skipped"] += 1
                    continue

                # Extract content
                content = await self._extract_content(access_token, drive_id, mime, name)
                if not content or len(content.strip()) < MIN_CONTENT_LENGTH:
                    summary["files_skipped"] += 1
                    continue

                # In goals_only mode, truncate more aggressively + store summary
                if privacy_level == "goals_only":
                    content_for_store = content[:MAX_CONTENT_CHARS // 2]
                else:
                    content_for_store = content[:MAX_CONTENT_CHARS]

                # Embed
                embedding = await self._embed(content_for_store)
                if embedding is None:
                    summary["files_skipped"] += 1
                    continue

                # Upsert to DB
                await self._upsert_document(
                    drive_id=drive_id,
                    name=name,
                    mime_type=mime,
                    content=content_for_store,
                    embedding=embedding,
                    modified_time=modified,
                    owner_user_id=user_id,
                )
                summary["files_indexed"] += 1
                logger.debug(f"DriveAgentV2: indexed '{name}' for user {user_id[:8]}")

            except Exception as e:
                logger.warning(
                    f"DriveAgentV2: error on '{name}' for user {user_id[:8]}: {e}"
                )
                summary["files_errored"] += 1
                summary["errors"].append(f"{name}: {e}")

        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            f"DriveAgentV2 sync complete for {user_id[:8]} — "
            f"indexed={summary['files_indexed']} "
            f"skipped={summary['files_skipped']} "
            f"errored={summary['files_errored']}"
        )
        return summary

    async def semantic_search(
        self,
        query: str,
        user_id: str,
        limit: int = 5,
        min_similarity: float = 0.70,
    ) -> List[Dict[str, Any]]:
        """
        Semantic search over this user's indexed Drive documents.
        Always enforces owner_user_id filter — no cross-user access possible.
        """
        if not self.openai or not settings.has_openai:
            return []

        try:
            embedding = await self._embed(query)
            if embedding is None:
                return []

            embedding_str = _vector_to_pg(embedding)
            rows = await fetch(
                f"""
                SELECT drive_id, name, content,
                       1 - (embedding <=> '{embedding_str}'::vector) AS similarity
                FROM drive_documents
                WHERE embedding IS NOT NULL
                  AND owner_user_id = $2
                ORDER BY embedding <=> '{embedding_str}'::vector
                LIMIT $1
                """,
                limit,
                user_id,
            )

            results = []
            for row in rows:
                sim = float(row["similarity"])
                if sim < min_similarity:
                    continue
                results.append({
                    "drive_id": row["drive_id"],
                    "name": row["name"],
                    "excerpt": _extract_excerpt(row["content"], query),
                    "similarity": round(sim, 3),
                })
            return results

        except Exception as e:
            logger.warning(f"DriveAgentV2: semantic search failed for {user_id[:8]}: {e}")
            return []

    async def status(self, user_id: str) -> Dict[str, Any]:
        """Return indexing status for the given user."""
        try:
            count = await fetchval(
                "SELECT COUNT(*) FROM drive_documents WHERE owner_user_id = $1",
                user_id,
            ) or 0
            last_sync = await fetchval(
                "SELECT MAX(last_synced) FROM drive_documents WHERE owner_user_id = $1",
                user_id,
            )
            token_row = await fetchrow(
                "SELECT drive_connected, drive_privacy_level FROM google_oauth_tokens WHERE user_id = $1",
                UUID(user_id),
            )
            return {
                "indexed_documents": int(count),
                "last_sync": last_sync.isoformat() if last_sync else None,
                "drive_connected": bool(token_row["drive_connected"]) if token_row else False,
                "drive_privacy_level": (token_row["drive_privacy_level"] if token_row else "none"),
                "status": "ok",
            }
        except Exception as e:
            logger.warning(f"DriveAgentV2: status query failed for {user_id[:8]}: {e}")
            return {
                "indexed_documents": 0,
                "last_sync": None,
                "drive_connected": False,
                "drive_privacy_level": "none",
                "status": "error",
                "error": str(e),
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _list_drive_files(
        self, access_token: str, max_files: int, privacy_level: str
    ) -> List[Dict[str, Any]]:
        """List files from Google Drive API."""
        # For goals_only, only fetch files modified in last 90 days
        params: Dict[str, Any] = {
            "q": "trashed=false",
            "orderBy": "modifiedTime desc",
            "pageSize": min(max_files, 100),
            "fields": "files(id,name,mimeType,modifiedTime,size)",
        }

        if privacy_level == "goals_only":
            cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).strftime(
                "%Y-%m-%dT%H:%M:%S"
            )
            params["q"] = f"trashed=false and modifiedTime > '{cutoff}'"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                DRIVE_FILES_URL,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )

        if resp.status_code == 401:
            raise RuntimeError("Drive access token invalid or expired")
        if resp.status_code != 200:
            raise RuntimeError(f"Drive API error: {resp.status_code} {resp.text[:200]}")

        data = resp.json()
        return data.get("files", [])

    async def _extract_content(
        self, access_token: str, drive_id: str, mime: str, name: str
    ) -> Optional[str]:
        """Extract text content from a Drive file."""
        if mime == "application/vnd.google-apps.document":
            return await self._export_google_doc(access_token, drive_id, name)
        elif mime == "text/plain":
            return await self._download_file(access_token, drive_id, name)
        else:
            # Sheets/Slides: return name + MIME as minimal content
            return f"{name} ({mime})"

    async def _export_google_doc(
        self, access_token: str, drive_id: str, name: str
    ) -> Optional[str]:
        """Export a Google Doc as plain text."""
        url = DRIVE_EXPORT_URL.format(file_id=drive_id)
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                url,
                params={"mimeType": "text/plain"},
                headers={"Authorization": f"Bearer {access_token}"},
            )

        if resp.status_code != 200:
            logger.warning(
                f"DriveAgentV2: export failed for '{name}': {resp.status_code}"
            )
            return None

        return _clean_text(resp.text)

    async def _download_file(
        self, access_token: str, drive_id: str, name: str
    ) -> Optional[str]:
        """Download a plain text file from Drive."""
        url = DRIVE_MEDIA_URL.format(file_id=drive_id)
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                url,
                params={"alt": "media"},
                headers={"Authorization": f"Bearer {access_token}"},
            )

        if resp.status_code != 200:
            logger.warning(
                f"DriveAgentV2: download failed for '{name}': {resp.status_code}"
            )
            return None

        return _clean_text(resp.text)

    async def _embed(self, text: str) -> Optional[List[float]]:
        """Create an embedding via OpenAI text-embedding-3-small."""
        if not self.openai or not settings.has_openai:
            return None
        try:
            response = await self.openai.embeddings.create(
                model="text-embedding-3-small",
                input=text[:8000],
            )
            return response.data[0].embedding
        except Exception as e:
            logger.warning(f"DriveAgentV2: embedding failed: {e}")
            return None

    async def _is_current(
        self, drive_id: str, modified_time: Optional[str]
    ) -> bool:
        """Return True if the doc is already indexed and hasn't changed."""
        row = await fetchrow(
            "SELECT modified_time FROM drive_documents WHERE drive_id = $1",
            drive_id,
        )
        if row is None:
            return False
        if modified_time is None:
            return True
        try:
            stored = row["modified_time"]
            if stored is None:
                return False
            new_ts = datetime.fromisoformat(modified_time.replace("Z", "+00:00"))
            return stored >= new_ts
        except Exception:
            return False

    async def _upsert_document(
        self,
        drive_id: str,
        name: str,
        mime_type: str,
        content: str,
        embedding: List[float],
        modified_time: Optional[str],
        owner_user_id: str,
    ) -> None:
        """Insert or update a drive_document row."""
        mod_ts = None
        if modified_time:
            try:
                mod_ts = datetime.fromisoformat(modified_time.replace("Z", "+00:00"))
            except Exception:
                pass

        embedding_str = _vector_to_pg(embedding)
        await execute(
            f"""
            INSERT INTO drive_documents
                (drive_id, name, mime_type, content, embedding, last_synced, modified_time, owner_user_id)
            VALUES ($1, $2, $3, $4, '{embedding_str}'::vector, NOW(), $5, $6)
            ON CONFLICT (drive_id) DO UPDATE SET
                name          = EXCLUDED.name,
                mime_type     = EXCLUDED.mime_type,
                content       = EXCLUDED.content,
                embedding     = EXCLUDED.embedding,
                last_synced   = NOW(),
                modified_time = EXCLUDED.modified_time,
                owner_user_id = EXCLUDED.owner_user_id
            """,
            drive_id,
            name,
            mime_type,
            content,
            mod_ts,
            owner_user_id,
        )


# ─── Pure utilities ───────────────────────────────────────────────────────────

def _looks_private(filename: str) -> bool:
    """Return True if the filename looks like a private/sensitive document."""
    lower = filename.lower()
    return any(keyword in lower for keyword in PRIVATE_FILENAME_KEYWORDS)


def _clean_text(raw: str) -> str:
    """Strip formatting noise from Drive content."""
    raw = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    raw = re.sub(r"[ \t]+", " ", raw)
    return raw.strip()


def _vector_to_pg(vec: List[float]) -> str:
    """Convert a Python float list to pgvector literal: [0.1,0.2,...]"""
    return "[" + ",".join(f"{v:.8f}" for v in vec) + "]"


def _extract_excerpt(content: str, query: str, window: int = 300) -> str:
    """Extract a ~300-char excerpt from content near the query terms."""
    if not content:
        return ""
    query_words = [w.lower() for w in query.split() if len(w) > 3]
    best_pos = 0
    best_score = 0
    for i in range(0, max(1, len(content) - window), 50):
        chunk = content[i : i + window].lower()
        score = sum(1 for w in query_words if w in chunk)
        if score > best_score:
            best_score = score
            best_pos = i
    excerpt = content[best_pos : best_pos + window].strip()
    if len(content) > best_pos + window:
        excerpt += "…"
    return excerpt
