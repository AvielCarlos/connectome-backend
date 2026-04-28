"""
Base Executive Agent — Interface all C-suite agents implement.

Every agent in Ora's Executive Council inherits from this class.
They each own a domain, analyze it, report on it, and can act autonomously.
"""

import json
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_CHAT_ID = 5716959016
LOG_DIR = "/Users/avielcarlos/.openclaw/workspace/tmp/executive_council"
API_BASE = "https://connectome-api-production.up.railway.app"


class BaseExecutiveAgent(ABC):
    """
    Interface all executive agents implement.
    
    Each agent:
    - Has a domain (finance, marketing, product, etc.)
    - Analyzes data in that domain
    - Reports insights in plain English
    - Can take safe autonomous actions
    - Teaches Ora what it learns via /api/ora/learn
    """

    name: str = "base"
    display_name: str = "Base Agent"

    def __init__(self):
        self._jwt_token: Optional[str] = None
        self._telegram_token: Optional[str] = None
        os.makedirs(LOG_DIR, exist_ok=True)

    # ─── Interface ──────────────────────────────────────────────────────────

    @abstractmethod
    async def analyze(self) -> Dict[str, Any]:
        """Gather data and compute metrics for this domain."""
        ...

    @abstractmethod
    async def report(self) -> str:
        """Return a human-readable insight summary."""
        ...

    @abstractmethod
    async def recommend(self) -> List[str]:
        """Return a list of recommended actions."""
        ...

    @abstractmethod
    async def act(self) -> Dict[str, Any]:
        """Take safe autonomous actions based on analysis."""
        ...

    # ─── Shared helpers ─────────────────────────────────────────────────────

    async def teach_ora(self, insight: str, confidence: float = 0.8) -> bool:
        """
        POST an insight to /api/ora/learn so Ora compounds in intelligence.
        Uses JWT auth. Returns True on success.
        """
        try:
            token = await self._get_jwt()
            if not token:
                logger.warning(f"{self.name}: no JWT, cannot teach Ora")
                return False

            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{API_BASE}/api/ora/learn",
                    json={
                        "lesson": insight,
                        "confidence": confidence,
                        "source": f"executive_council.{self.name}",
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code in (200, 201):
                    logger.info(f"{self.name}: taught Ora — {insight[:80]}")
                    return True
                else:
                    logger.warning(f"{self.name}: teach_ora failed {resp.status_code}: {resp.text[:200]}")
                    return False
        except Exception as e:
            logger.error(f"{self.name}: teach_ora error: {e}")
            return False

    async def save_report(self, data: Dict[str, Any], filename: Optional[str] = None) -> str:
        """Save a report dict as JSON to the executive_council log dir."""
        fname = filename or f"{self.name}_report.json"
        path = os.path.join(LOG_DIR, fname)
        data["_saved_at"] = datetime.now(timezone.utc).isoformat()
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        return path

    async def load_last_report(self, filename: Optional[str] = None) -> Optional[Dict]:
        """Load the most recent saved report for this agent."""
        fname = filename or f"{self.name}_report.json"
        path = os.path.join(LOG_DIR, fname)
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return None

    async def set_redis_report(self, summary: str) -> None:
        """Store latest summary in Redis so other agents can read it."""
        try:
            from core.redis_client import get_redis
            redis = await get_redis()
            key = f"ora:executive:last_report:{self.name}"
            await redis.setex(key, 604800, summary)  # 7 day TTL
        except Exception as e:
            logger.debug(f"{self.name}: Redis set failed: {e}")

    async def get_redis_report(self, agent_name: str) -> Optional[str]:
        """Get another agent's latest report from Redis."""
        try:
            from core.redis_client import get_redis
            redis = await get_redis()
            return await redis.get(f"ora:executive:last_report:{agent_name}")
        except Exception as e:
            logger.debug(f"{self.name}: Redis get failed: {e}")
            return None

    async def alert_avi(self, message: str) -> None:
        """Send an urgent Telegram message to Avi."""
        await self._send_telegram(f"🚨 *{self.display_name}*\n\n{message}")

    async def _send_telegram(self, message: str, chat_id: int = TELEGRAM_CHAT_ID) -> None:
        """Send a Telegram message."""
        token = await self._get_telegram_token()
        if not token:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
                )
                if resp.status_code != 200:
                    logger.warning(f"{self.name}: Telegram {resp.status_code}")
        except Exception as e:
            logger.warning(f"{self.name}: Telegram send failed: {e}")

    async def _get_jwt(self) -> Optional[str]:
        """Get JWT by logging in with test credentials."""
        if self._jwt_token:
            return self._jwt_token
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{API_BASE}/api/users/login",
                    json={"email": "test@test.com", "password": "test1234"},
                )
                if resp.status_code == 200:
                    self._jwt_token = resp.json().get("token") or resp.json().get("access_token")
                    return self._jwt_token
        except Exception as e:
            logger.error(f"{self.name}: JWT login failed: {e}")
        return None

    async def _api_get(self, path: str) -> Optional[Dict]:
        """Authenticated GET against the Connectome API."""
        try:
            token = await self._get_jwt()
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(f"{API_BASE}{path}", headers=headers)
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            logger.debug(f"{self.name}: GET {path} failed: {e}")
        return None

    async def _get_telegram_token(self) -> Optional[str]:
        if self._telegram_token:
            return self._telegram_token
        token = os.environ.get("ORA_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            try:
                with open("/Users/avielcarlos/.openclaw/secrets/telegram-bot-token.txt") as f:
                    token = f.read().strip()
            except Exception:
                pass
        if token:
            self._telegram_token = token
        return token
