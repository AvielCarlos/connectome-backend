"""
GitHub Webhook Handler

Receives push and workflow_run events from GitHub.
On workflow_run completion with failure → alerts Avi via Telegram instantly.
No polling needed — GitHub pushes to us.

Setup: add webhook in each repo Settings → Webhooks → 
  Payload URL: https://connectome-api-production.up.railway.app/api/github/webhook
  Content type: application/json
  Secret: (set GITHUB_WEBHOOK_SECRET in Railway env)
  Events: Workflow runs
"""

import hashlib
import hmac
import logging
import os

from fastapi import APIRouter, HTTPException, Request

from core.config import settings
from core.telegram import send_telegram_message

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/github", tags=["github"])

WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
TELEGRAM_CHAT_ID = "5716959016"


def _verify_signature(payload: bytes, signature: str) -> bool:
    if not WEBHOOK_SECRET:
        return False
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


async def _send_telegram(msg: str) -> None:
    ok = await send_telegram_message(msg, chat_id=TELEGRAM_CHAT_ID, parse_mode="Markdown")
    if not ok:
        logger.error("GitHub webhook: Telegram alert skipped/failed")


@router.post("/webhook")
async def github_webhook(request: Request):
    """Receive GitHub webhook events."""
    payload = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    event = request.headers.get("X-GitHub-Event", "")

    if not WEBHOOK_SECRET:
        if settings.is_production:
            raise HTTPException(status_code=503, detail="GitHub webhook secret not configured")
        logger.warning("GitHub webhook secret not configured — accepting webhook only because APP_ENV is not production")
    elif not _verify_signature(payload, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Handle workflow_run events
    if event == "workflow_run":
        action = data.get("action")
        workflow = data.get("workflow_run", {})
        status = workflow.get("status")
        conclusion = workflow.get("conclusion")
        repo = data.get("repository", {}).get("full_name", "unknown")
        branch = workflow.get("head_branch", "")
        name = workflow.get("name", "Workflow")
        url = workflow.get("html_url", "")

        logger.info(f"GitHub workflow_run: {repo} / {name} — {action} / {conclusion}")

        # Only alert on failures on main branch
        if (
            action == "completed"
            and conclusion == "failure"
            and branch in ("main", "master")
        ):
            msg = (
                f"🚨 *Build failed* — {repo.split('/')[1]}\n"
                f"Workflow: {name}\n"
                f"Branch: {branch}\n"
                f"{url}"
            )
            await _send_telegram(msg)
            logger.warning(f"GitHub: build failure alerted for {repo}")

            # Feed to Aura knowledge base
            try:
                from core.database import execute
                await execute(
                    "INSERT INTO aura_knowledge (content, confidence, source, created_at) "
                    "VALUES ($1, 0.9, 'github_webhook', NOW()) ON CONFLICT DO NOTHING",
                    f"Build failure on {repo} branch {branch}: {name} workflow failed. URL: {url}"
                )
            except Exception:
                pass

        return {"ok": True, "event": event, "action": action}

    # Handle push events (for awareness)
    if event == "push":
        repo = data.get("repository", {}).get("full_name", "unknown")
        branch = data.get("ref", "").replace("refs/heads/", "")
        pusher = data.get("pusher", {}).get("name", "unknown")
        commits = len(data.get("commits", []))
        logger.info(f"GitHub push: {repo} / {branch} by {pusher} ({commits} commits)")
        return {"ok": True, "event": event}

    return {"ok": True, "event": event}
