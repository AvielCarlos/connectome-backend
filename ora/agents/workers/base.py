"""
BaseWorkerAgent — Foundation for all Ora worker agents.

Workers are the operational staff. They:
- Run on a schedule, do one job well
- Report to a C-suite executive agent
- Teach Ora insights via /api/ora/learn
- Escalate critical issues to Avi via Telegram
"""

import json
import logging
import os
import subprocess
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

API_BASE = os.getenv("CONNECTOME_API_BASE", "https://connectome-api-production.up.railway.app")
AVI_TELEGRAM_CHAT_ID = int(os.getenv("ORA_TELEGRAM_CHAT_ID", "5716959016"))
WORKER_CHANNEL_ID = int(os.getenv("WORKER_TELEGRAM_CHANNEL_ID", "-1003968154861"))
APP_ENV = os.getenv("APP_ENV", "development").lower()


class BaseWorkerAgent(ABC):
    """Base class for all Ora worker agents."""

    name: str = "base_worker"
    role: str = "Worker"
    reports_to: str = "Ora"

    def __init__(self):
        self._jwt: Optional[str] = None
        self._tg_token: Optional[str] = None

    @abstractmethod
    async def run(self) -> None:
        """Main job — implement in each worker."""
        ...

    async def report(self) -> str:
        """Summary for Ora. Override for custom summaries."""
        return f"{self.name} completed its run at {datetime.now(timezone.utc).isoformat()}"

    # ─── Core helpers ────────────────────────────────────────────────────────

    async def teach_aura(self, insight: str, confidence: float = 0.8) -> bool:
        """POST an insight to /api/ora/learn."""
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
                        "source": f"worker.{self.name}",
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                ok = resp.status_code in (200, 201)
                if ok:
                    logger.info(f"{self.name}: taught Ora — {insight[:80]}")
                else:
                    logger.warning(f"{self.name}: teach_ora {resp.status_code}: {resp.text[:200]}")
                return ok
        except Exception as e:
            logger.error(f"{self.name}: teach_ora error: {e}")
            return False

    async def escalate(self, issue: str) -> None:
        """Send a critical alert to Avi via Telegram."""
        msg = f"🚨 *Worker Alert — {self.name}*\n\n{issue}"
        await self._telegram(msg, chat_id=AVI_TELEGRAM_CHAT_ID)

    async def post_to_channel(self, message: str) -> None:
        """Post to the @ascensionai Telegram channel."""
        await self._telegram(message, chat_id=WORKER_CHANNEL_ID)

    # ─── API helpers ─────────────────────────────────────────────────────────

    async def _get(self, path: str) -> Optional[Any]:
        token = await self._get_jwt()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(f"{API_BASE}{path}", headers=headers)
                if resp.status_code == 200:
                    return resp.json()
                logger.debug(f"{self.name}: GET {path} → {resp.status_code}")
        except Exception as e:
            logger.debug(f"{self.name}: GET {path} error: {e}")
        return None

    async def _post(self, path: str, data: Dict) -> Optional[Any]:
        token = await self._get_jwt()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(f"{API_BASE}{path}", json=data, headers=headers)
                if resp.status_code in (200, 201):
                    return resp.json()
                logger.debug(f"{self.name}: POST {path} → {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.debug(f"{self.name}: POST {path} error: {e}")
        return None

    async def _put(self, path: str, data: Dict) -> Optional[Any]:
        token = await self._get_jwt()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.put(f"{API_BASE}{path}", json=data, headers=headers)
                if resp.status_code in (200, 201):
                    return resp.json()
                logger.debug(f"{self.name}: PUT {path} → {resp.status_code}")
        except Exception as e:
            logger.debug(f"{self.name}: PUT {path} error: {e}")
        return None

    async def _get_jwt(self) -> Optional[str]:
        if self._jwt:
            return self._jwt
        token = os.environ.get("ORA_JWT_TOKEN") or os.environ.get("CONNECTOME_WORKER_JWT")
        if token:
            self._jwt = token
            return token
        # Fall back to fresh login in dev/test only, and only with explicit env creds.
        if APP_ENV == "production":
            logger.warning(f"{self.name}: ORA_JWT_TOKEN/CONNECTOME_WORKER_JWT missing; skipping prod login fallback")
            return None
        test_email = os.environ.get("CONNECTOME_TEST_EMAIL")
        test_password = os.environ.get("CONNECTOME_TEST_PASSWORD")
        if not test_email or not test_password:
            logger.warning(f"{self.name}: no worker JWT or CONNECTOME_TEST_EMAIL/PASSWORD configured")
            return None
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{API_BASE}/api/users/login",
                    json={"email": test_email, "password": test_password},
                )
                if resp.status_code == 200:
                    body = resp.json()
                    self._jwt = body.get("access_token") or body.get("token")
                    return self._jwt
        except Exception as e:
            logger.error(f"{self.name}: JWT login failed: {e}")
        return None

    async def _get_tg_token(self) -> Optional[str]:
        if self._tg_token:
            return self._tg_token
        token = os.environ.get("ORA_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
        self._tg_token = token
        return token

    async def _telegram(self, message: str, chat_id: int = AVI_TELEGRAM_CHAT_ID) -> None:
        token = await self._get_tg_token()
        if not token:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
                )
        except Exception as e:
            logger.warning(f"{self.name}: Telegram send failed: {e}")

    def _sh(self, cmd: str) -> str:
        """Run a shell command and return stdout."""
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
            return result.stdout.strip()
        except Exception as e:
            return f"error: {e}"

    def _save_json(self, path: str, data: Any) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def _load_json(self, path: str, default: Any = None) -> Any:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
        return default
