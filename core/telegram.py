"""
Telegram alert helper for cloud-safe Connectome runtime.

Production should use env vars, not Avi's laptop filesystem:
- ORA_TELEGRAM_TOKEN or TELEGRAM_BOT_TOKEN
- ORA_TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_CHAT_ID = "5716959016"


def get_telegram_token() -> Optional[str]:
    return os.getenv("ORA_TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")


def get_telegram_chat_id(default: str = DEFAULT_CHAT_ID) -> str:
    return os.getenv("ORA_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or default


async def send_telegram_message(message: str, *, chat_id: Optional[str] = None, parse_mode: Optional[str] = None) -> bool:
    token = get_telegram_token()
    if not token:
        logger.warning("Telegram alert skipped: ORA_TELEGRAM_TOKEN/TELEGRAM_BOT_TOKEN not configured")
        return False

    payload = {"chat_id": chat_id or get_telegram_chat_id(), "text": message}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
            )
            response.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("Telegram alert failed: %s", exc)
        return False
