"""
ORA Drive Storage — Ora's Google Drive persistent memory interface.

Distinct from drive_agent.py (pgvector semantic indexer).
This module handles simple upload/download/backup operations so agents
can persist their outputs to Google Drive for durability and Avi's visibility.

Ora uses Drive as:
  - Identity backup destination  (backups folder)
  - Training data archive        (training folder)
  - Weekly knowledge export      (knowledge folder)
  - Agent reports & reflections  (reports folder)
  - Experiment results           (experiments folder)
  - Strategic documents          (strategic folder)

All operations use the `gog` CLI with the carlosandromeda8@gmail.com account.
"""

import json
import logging
import mimetypes
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"

_MISSING_GOG_WARNED = False

FOLDER_IDS_FILE = "/Users/avielcarlos/.openclaw/workspace/tmp/drive_folder_ids.json"

# Fallback hard-coded IDs in case the file is unavailable at runtime
_HARDCODED_IDS: Dict[str, str] = {
    "ora_brain":   "1JTBrC4zxOdhtDsW1osuxU77hgQhEHgvN",
    "knowledge":   "1SymVTcfh-KSXID39DkuNIMub6hb6V9Gf",
    "backups":     "1J_fp5UJi9mGzici9BOb-BzcnIF-9IdXm",
    "training":    "1dnNL-gLeawXLd_AkMbyE6hwJP3JzN1jy",
    "reports":     "1GaPxqcoPWvFRWO6CAlCInSupE159BCMg",
    "experiments": "1wYszk-UNcrNFSFFHMHG4xIGTAVFirrXH",
    "strategic":   "1rlzBzXzn0S1ghr1cyKHqAVOGb4RwY69x",
}


def get_folder_ids() -> Dict[str, str]:
    """Load folder IDs from file, falling back to hardcoded defaults."""
    try:
        with open(FOLDER_IDS_FILE) as f:
            ids = json.load(f)
        # Merge: file values take precedence
        merged = dict(_HARDCODED_IDS)
        merged.update(ids)
        return merged
    except Exception:
        return dict(_HARDCODED_IDS)


class DriveStorage:
    """
    Ora's Google Drive storage operations.

    All methods are synchronous so they can be called from background
    threads, cron scripts, and async contexts alike (run in executor if
    inside an async event loop).
    """

    # ------------------------------------------------------------------
    # Core upload helpers
    # ------------------------------------------------------------------

    def _api_access_token(self) -> str:
        """
        Return an access token for production Drive uploads when Railway has
        OAuth env vars configured. This avoids depending on the local `gog` CLI.

        Expected env:
          - GOOGLE_CLIENT_ID
          - GOOGLE_CLIENT_SECRET
          - ORA_DRIVE_REFRESH_TOKEN or GOOGLE_DRIVE_REFRESH_TOKEN
        """
        refresh_token = (
            os.getenv("ORA_DRIVE_REFRESH_TOKEN")
            or os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN")
            or ""
        )
        client_id = os.getenv("GOOGLE_CLIENT_ID", "")
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
        if not (refresh_token and client_id and client_secret):
            return ""

        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(
                    GOOGLE_TOKEN_URL,
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            if resp.status_code != 200:
                logger.warning("DriveStorage: API token refresh failed: %s", resp.text[:300])
                return ""
            return resp.json().get("access_token", "")
        except Exception as e:
            logger.warning("DriveStorage: API token refresh exception: %s", e)
            return ""

    def _upload_bytes_via_api(
        self,
        content: bytes,
        filename: str,
        folder_id: str,
        mime_type: str = "text/plain",
    ) -> str:
        """Upload bytes to Google Drive using the Drive REST API."""
        access_token = self._api_access_token()
        if not access_token:
            return ""

        metadata = {"name": filename, "parents": [folder_id]}
        files = {
            "metadata": (None, json.dumps(metadata), "application/json; charset=UTF-8"),
            "file": (filename, content, mime_type),
        }
        try:
            with httpx.Client(timeout=45) as client:
                resp = client.post(
                    GOOGLE_UPLOAD_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                    files=files,
                )
            if resp.status_code not in (200, 201):
                logger.warning(
                    "DriveStorage: API upload failed for '%s': %s %s",
                    filename,
                    resp.status_code,
                    resp.text[:300],
                )
                return ""
            file_id = resp.json().get("id", "")
            logger.info("DriveStorage: API uploaded '%s' (id=%s)", filename, file_id)
            return file_id
        except Exception as e:
            logger.warning("DriveStorage: API upload exception for '%s': %s", filename, e)
            return ""

    def _gog_available(self) -> bool:
        """Return whether the local gog CLI is available; warn once if absent."""
        global _MISSING_GOG_WARNED
        if shutil.which("gog"):
            return True
        if not _MISSING_GOG_WARNED:
            logger.warning(
                "DriveStorage: gog CLI unavailable and no API upload token configured; "
                "Drive uploads are skipped. Set ORA_DRIVE_REFRESH_TOKEN in Railway "
                "or install/configure gog in this runtime."
            )
            _MISSING_GOG_WARNED = True
        return False

    def upload_text(self, content: str, filename: str, folder_key: str) -> str:
        """
        Upload a text string as a file to a Drive folder.
        Returns the file ID on success, empty string on failure.
        """
        folders = get_folder_ids()
        folder_id = folders.get(folder_key) or folders.get("ora_brain", "")
        if not folder_id:
            logger.warning(f"DriveStorage.upload_text: unknown folder_key={folder_key!r}")
            return ""

        api_id = self._upload_bytes_via_api(
            content.encode("utf-8"),
            filename,
            folder_id,
            "text/plain; charset=utf-8",
        )
        if api_id:
            return api_id

        if not self._gog_available():
            return ""

        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            ) as f:
                f.write(content)
                tmp_path = f.name

            result = subprocess.run(
                [
                    "gog", "drive", "upload", tmp_path,
                    "--name", filename,
                    "--parent", folder_id,
                    "-j",
                ],
                capture_output=True,
                text=True,
                timeout=45,
            )
            os.unlink(tmp_path)

            if result.returncode == 0:
                try:
                    data = json.loads(result.stdout)
                    # gog returns {"file": {"id": ...}} or {"id": ...}
                    if "file" in data:
                        file_id = data["file"].get("id", result.stdout.strip())
                    else:
                        file_id = data.get("id", result.stdout.strip())
                except Exception:
                    file_id = result.stdout.strip()
                logger.info(
                    f"DriveStorage: uploaded '{filename}' to {folder_key} (id={file_id})"
                )
                return file_id
            else:
                logger.warning(
                    f"DriveStorage: upload failed for '{filename}': {result.stderr.strip()}"
                )
                return ""
        except Exception as e:
            logger.error(f"DriveStorage: upload_text exception for '{filename}': {e}")
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            return ""

    def upload_json(self, data: Any, filename: str, folder_key: str) -> str:
        """Serialize data as JSON and upload to Drive."""
        return self.upload_text(
            json.dumps(data, indent=2, default=str),
            filename,
            folder_key,
        )

    def upload_file(self, local_path: str, filename: str, folder_key: str) -> str:
        """Upload an existing local file to Drive."""
        folders = get_folder_ids()
        folder_id = folders.get(folder_key) or folders.get("ora_brain", "")
        if not folder_id:
            logger.warning(f"DriveStorage.upload_file: unknown folder_key={folder_key!r}")
            return ""

        try:
            data = Path(local_path).read_bytes()
            mime_type = mimetypes.guess_type(filename or local_path)[0] or "application/octet-stream"
            api_id = self._upload_bytes_via_api(data, filename, folder_id, mime_type)
            if api_id:
                return api_id
        except Exception as e:
            logger.warning("DriveStorage: could not read file for API upload '%s': %s", local_path, e)

        if not self._gog_available():
            return ""

        try:
            result = subprocess.run(
                [
                    "gog", "drive", "upload", local_path,
                    "--name", filename,
                    "--parent", folder_id,
                    "-j",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                try:
                    data = json.loads(result.stdout)
                    if "file" in data:
                        file_id = data["file"].get("id", result.stdout.strip())
                    else:
                        file_id = data.get("id", result.stdout.strip())
                except Exception:
                    file_id = result.stdout.strip()
                logger.info(
                    f"DriveStorage: file uploaded '{filename}' to {folder_key} (id={file_id})"
                )
                return file_id
            else:
                logger.warning(
                    f"DriveStorage: file upload failed for '{filename}': {result.stderr.strip()}"
                )
                return ""
        except Exception as e:
            logger.error(f"DriveStorage: upload_file exception for '{filename}': {e}")
            return ""

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def read_file(self, file_id: str) -> str:
        """Read a Google Doc or Drive file by ID."""
        try:
            result = subprocess.run(
                ["gog", "docs", "cat", file_id],
                capture_output=True,
                text=True,
                timeout=25,
            )
            return result.stdout if result.returncode == 0 else ""
        except Exception as e:
            logger.warning(f"DriveStorage: read_file failed for {file_id}: {e}")
            return ""

    def list_folder(self, folder_key: str) -> List[Dict[str, Any]]:
        """List files in a Drive folder. Returns list of file metadata dicts."""
        folders = get_folder_ids()
        folder_id = folders.get(folder_key, "")
        if not folder_id:
            return []
        try:
            result = subprocess.run(
                ["gog", "drive", "ls", "--parent", folder_id, "-j"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if isinstance(data, list):
                    return data
                return data.get("files", [])
        except Exception as e:
            logger.warning(f"DriveStorage: list_folder failed for {folder_key}: {e}")
        return []

    def search_drive(self, query: str) -> List[Dict[str, Any]]:
        """Full-text search across Drive."""
        try:
            result = subprocess.run(
                ["gog", "drive", "search", query, "-j"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if isinstance(data, list):
                    return data
                return data.get("files", [])
        except Exception as e:
            logger.warning(f"DriveStorage: search_drive failed for {query!r}: {e}")
        return []

    # ------------------------------------------------------------------
    # High-level agent helpers
    # ------------------------------------------------------------------

    def save_backup(self, backup_data: Any, backup_name: str) -> str:
        """
        Save a backup bundle to the Identity Backups Drive folder.
        filename: {backup_name}_{timestamp}.json
        Returns Drive file ID.
        """
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{backup_name}_{ts}.json"
        file_id = self.upload_json(backup_data, filename, "backups")
        if file_id:
            logger.info(f"DriveStorage: backup saved → Drive/backups/{filename}")
        return file_id

    def save_weekly_report(self, agent_name: str, report: str) -> str:
        """Save an agent's weekly report to Drive reports folder."""
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filename = f"{agent_name}_report_{date}.txt"
        return self.upload_text(report, filename, "reports")

    def save_reflection(self, reflection_text: str, week: str) -> str:
        """Save Ora's weekly reflection markdown to Drive knowledge folder."""
        filename = f"reflection_{week}.md"
        return self.upload_text(reflection_text, filename, "knowledge")

    def save_training_example(self, example: Dict[str, Any]) -> str:
        """
        Save a single training example (JSONL line) to Drive training folder.
        Each call appends one line; files are named by date so they batch naturally.
        """
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filename = f"training_{date}.jsonl"
        line = json.dumps(example, default=str)
        return self.upload_text(line, filename, "training")

    def save_lesson(self, lesson: str, source: str) -> str:
        """Save a knowledge lesson to Drive knowledge folder."""
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        content = f"Date: {date}\nSource: {source}\n\n{lesson}"
        filename = f"lesson_{source.replace('/', '_')}_{date}.txt"
        return self.upload_text(content, filename, "knowledge")

    def save_experiment_result(self, experiment_data: Any, name: str) -> str:
        """Save an A/B experiment result to Drive experiments folder."""
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filename = f"experiment_{name}_{date}.json"
        return self.upload_json(experiment_data, filename, "experiments")

    def save_strategic_doc(self, content: str, title: str) -> str:
        """Save a strategic document to Drive strategic folder."""
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filename = f"{title}_{date}.txt"
        return self.upload_text(content, filename, "strategic")

    def publish_knowledge_base(self, lessons: List[Dict[str, Any]]) -> str:
        """
        Publish Ora's knowledge base to Drive knowledge folder.
        Formats all lessons as a readable markdown document.
        Returns Drive file ID.
        """
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        content = f"# Ora's Knowledge Base — {date}\n\n"
        for i, lesson in enumerate(lessons[:100], 1):
            source = lesson.get("source", "general")
            text = lesson.get("lesson", "")
            content += f"## {i}. [{source}]\n{text}\n\n"
        filename = f"Ora_Knowledge_Base_{date}.md"
        file_id = self.upload_text(content, filename, "knowledge")
        if file_id:
            logger.info(f"DriveStorage: knowledge base published → Drive/knowledge/{filename}")
        return file_id

    def scan_avi_drive(self) -> List[Dict[str, Any]]:
        """
        Scan Avi's Google Drive root for Google Docs.
        Returns list of file metadata dicts with mimeType = google-apps.document.
        """
        try:
            result = subprocess.run(
                ["gog", "drive", "ls", "-j"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                files = data if isinstance(data, list) else data.get("files", [])
                return [f for f in files if "google-apps" in f.get("mimeType", "")]
        except Exception as e:
            logger.warning(f"DriveStorage: scan_avi_drive failed: {e}")
        return []


# Module-level singleton — import and use directly
drive = DriveStorage()
