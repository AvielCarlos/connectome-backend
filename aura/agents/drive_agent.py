"""
DriveAgent — Google Drive indexer for Aura
=========================================
Reads Avi's Google Drive via the `gog` CLI, creates OpenAI embeddings,
and stores them in pgvector so Aura can do semantic search over personal notes.

CLI primitives used:
  gog drive search "trashed=false" --max 50   → list files
  gog docs cat <docId>                         → read Google Doc content

# TODO (Railway cron):
# This sync should run once daily. Railway supports cron jobs natively.
# In railway.toml, add:
#
#   [[cron]]
#   schedule = "0 3 * * *"   # 3 AM UTC daily
#   command  = "python -m scripts.drive_sync"
#
# Or configure via the Railway dashboard → Settings → Cron Jobs.
# The endpoint POST /api/drive/sync can also be called manually or via webhook.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.config import settings
from core.database import execute, fetch, fetchrow, fetchval

logger = logging.getLogger(__name__)
GOG_CLI_ENABLED = os.getenv("GOG_CLI_ENABLED", "").lower() in {"1", "true", "yes"}

# Supported MIME types we know how to extract content from
INDEXABLE_MIME_TYPES = {
    "application/vnd.google-apps.document",       # Google Docs
    "text/plain",
    "application/vnd.google-apps.spreadsheet",    # Sheets — title+meta only
    "application/vnd.google-apps.presentation",   # Slides — title+meta only
}

# Minimum content length to bother embedding
MIN_CONTENT_LENGTH = 50

# How many chars of content to embed per doc (keep token cost sane)
MAX_CONTENT_CHARS = 6000


class DriveAgent:
    """
    Indexes Google Drive documents into pgvector for semantic search.
    Used by Aura to surface relevant personal notes during coaching.
    """

    AGENT_NAME = "DriveAgent"

    def __init__(self, openai_client=None):
        self.openai = openai_client

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def sync(self, max_files: int = 50, owner_user_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Full Drive sync: list files → read content → embed → upsert.
        owner_user_id: UUID string of the user who owns these documents.
                       ALL indexed docs are tagged with this owner — other users
                       will NEVER see them. Required for privacy isolation.
        Returns a summary dict with counts and any errors.
        """
        summary: Dict[str, Any] = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "files_found": 0,
            "files_indexed": 0,
            "files_skipped": 0,
            "files_errored": 0,
            "errors": [],
        }

        if settings.is_production and not GOG_CLI_ENABLED:
            summary["errors"].append("Legacy gog Drive sync disabled in production; use DriveAgentV2/API sync")
            summary["finished_at"] = datetime.now(timezone.utc).isoformat()
            logger.warning("DriveAgent: legacy gog sync skipped in production")
            return summary

        try:
            files = await self._list_drive_files(max_files)
        except Exception as e:
            logger.error(f"DriveAgent: failed to list Drive files: {e}")
            summary["errors"].append(f"list_files: {e}")
            summary["finished_at"] = datetime.now(timezone.utc).isoformat()
            return summary

        summary["files_found"] = len(files)
        logger.info(f"DriveAgent: found {len(files)} Drive files to process")

        for file_meta in files:
            drive_id = file_meta.get("id", "")
            mime = file_meta.get("mimeType", "")
            name = file_meta.get("name", "untitled")
            modified = file_meta.get("modifiedTime")

            try:
                # Skip if not indexable
                if mime not in INDEXABLE_MIME_TYPES:
                    summary["files_skipped"] += 1
                    continue

                # Check if already up-to-date
                if await self._is_current(drive_id, modified):
                    summary["files_skipped"] += 1
                    continue

                # Extract content
                content = await self._extract_content(drive_id, mime, name)
                if not content or len(content.strip()) < MIN_CONTENT_LENGTH:
                    summary["files_skipped"] += 1
                    continue

                # Truncate for embedding
                content_for_embed = content[:MAX_CONTENT_CHARS]

                # Embed
                embedding = await self._embed(content_for_embed)
                if embedding is None:
                    summary["files_skipped"] += 1
                    continue

                # Upsert to DB
                await self._upsert_document(
                    drive_id=drive_id,
                    name=name,
                    mime_type=mime,
                    content=content_for_embed,
                    embedding=embedding,
                    modified_time=modified,
                    owner_user_id=owner_user_id,
                )
                summary["files_indexed"] += 1
                logger.debug(f"DriveAgent: indexed '{name}' ({drive_id})")

            except Exception as e:
                logger.warning(f"DriveAgent: error on '{name}' ({drive_id}): {e}")
                summary["files_errored"] += 1
                summary["errors"].append(f"{name}: {e}")

        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            f"DriveAgent sync complete — indexed={summary['files_indexed']} "
            f"skipped={summary['files_skipped']} errored={summary['files_errored']}"
        )
        return summary

    async def semantic_search(
        self,
        query: str,
        owner_user_id: Optional[str] = None,
        limit: int = 5,
        min_similarity: float = 0.70,
    ) -> List[Dict[str, Any]]:
        """
        Semantic search over indexed Drive documents.
        owner_user_id MUST be provided and is ALWAYS enforced — results are
        strictly filtered to documents owned by that user. If None, returns [].
        Returns list of {drive_id, name, excerpt, similarity}.
        """
        # Hard privacy guard: never search without an owner filter
        if owner_user_id is None:
            logger.warning("DriveAgent: semantic_search called without owner_user_id — refusing")
            return []

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
                owner_user_id,
            )

            results = []
            for row in rows:
                sim = float(row["similarity"])
                if sim < min_similarity:
                    continue
                results.append(
                    {
                        "drive_id": row["drive_id"],
                        "name": row["name"],
                        "excerpt": _extract_excerpt(row["content"], query),
                        "similarity": round(sim, 3),
                    }
                )
            return results

        except Exception as e:
            logger.warning(f"DriveAgent: semantic search failed: {e}")
            return []

    async def status(self, owner_user_id: Optional[str] = None) -> Dict[str, Any]:
        """Return indexing status, filtered to the given owner."""
        try:
            if owner_user_id is not None:
                count = await fetchval(
                    "SELECT COUNT(*) FROM drive_documents WHERE owner_user_id = $1",
                    owner_user_id,
                ) or 0
                last_sync = await fetchval(
                    "SELECT MAX(last_synced) FROM drive_documents WHERE owner_user_id = $1",
                    owner_user_id,
                )
            else:
                count = 0
                last_sync = None
            return {
                "indexed_documents": int(count),
                "last_sync": last_sync.isoformat() if last_sync else None,
                "status": "ok",
            }
        except Exception as e:
            logger.warning(f"DriveAgent: status query failed: {e}")
            return {"indexed_documents": 0, "last_sync": None, "status": "error", "error": str(e)}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _list_drive_files(self, max_files: int) -> List[Dict[str, Any]]:
        """Run `gog drive search` and return parsed file list."""
        loop = asyncio.get_event_loop()

        def _run():
            result = subprocess.run(
                ["gog", "drive", "search", "trashed=false", "--max", str(max_files), "--json"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result

        try:
            result = await loop.run_in_executor(None, _run)
        except subprocess.TimeoutExpired:
            raise RuntimeError("gog drive search timed out")
        except FileNotFoundError:
            raise RuntimeError("gog CLI not found — install via npm i -g gog")

        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise RuntimeError(f"gog drive search failed: {stderr}")

        stdout = result.stdout.strip()
        if not stdout:
            return []

        try:
            data = json.loads(stdout)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("files", data.get("items", []))
        except json.JSONDecodeError:
            # Try line-by-line JSON objects
            files = []
            for line in stdout.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        files.append(json.loads(line))
                    except Exception:
                        pass
            return files

        return []

    async def _extract_content(
        self, drive_id: str, mime: str, name: str
    ) -> Optional[str]:
        """Extract text content from a Drive file."""
        if mime == "application/vnd.google-apps.document":
            return await self._read_google_doc(drive_id, name)
        elif mime == "text/plain":
            return await self._read_google_doc(drive_id, name)
        else:
            # Sheets/Slides: return name + MIME as minimal content
            return f"{name} ({mime})"

    async def _read_google_doc(self, drive_id: str, name: str) -> Optional[str]:
        """Run `gog docs cat <docId>` and return cleaned text."""
        loop = asyncio.get_event_loop()

        def _run():
            return subprocess.run(
                ["gog", "docs", "cat", drive_id],
                capture_output=True,
                text=True,
                timeout=20,
            )

        try:
            result = await loop.run_in_executor(None, _run)
        except subprocess.TimeoutExpired:
            logger.warning(f"DriveAgent: gog docs cat timed out for '{name}'")
            return None
        except FileNotFoundError:
            logger.error("gog CLI not found")
            return None

        if result.returncode != 0:
            logger.warning(f"DriveAgent: gog docs cat failed for '{name}': {result.stderr.strip()}")
            return None

        raw = result.stdout
        return _clean_text(raw)

    async def _embed(self, text: str) -> Optional[List[float]]:
        """Create an embedding via OpenAI text-embedding-3-small."""
        if not self.openai or not settings.has_openai:
            return None
        try:
            response = await self.openai.embeddings.create(
                model="text-embedding-3-small",
                input=text[:8000],  # hard token guard
            )
            return response.data[0].embedding
        except Exception as e:
            logger.warning(f"DriveAgent: embedding failed: {e}")
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
        owner_user_id: Optional[str] = None,
    ):
        """Insert or update a drive_document row, always stamping the owner."""
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


# ------------------------------------------------------------------
# Pure utilities
# ------------------------------------------------------------------

def _clean_text(raw: str) -> str:
    """Strip formatting noise from gog docs cat output."""
    raw = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    raw = re.sub(r"[ \t]+", " ", raw)
    return raw.strip()


def _vector_to_pg(vec: List[float]) -> str:
    """Convert a Python float list to pgvector literal: [0.1,0.2,...]"""
    return "[" + ",".join(f"{v:.8f}" for v in vec) + "]"


def _extract_excerpt(content: str, query: str, window: int = 300) -> str:
    """
    Extract a ~300-char excerpt from content near the query terms.
    Falls back to the first 300 chars if no match found.
    """
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
