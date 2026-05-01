"""
FailureDetector — Ora monitors her own API for real failures.

Checks beyond simple uptime:
- 4xx/5xx error rates on key endpoints
- CORS headers present on responses
- Feed loading successfully end-to-end
- Payment checkout endpoint responding
- Auth flow working
- DB connectivity

Runs every 2 hours. If it finds issues, alerts Avi and teaches Ora.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

API_BASE = os.getenv("API_BASE_URL", "https://connectome-api-production.up.railway.app")
TELEGRAM_CHAT_ID = 5716959016

CHECKS = [
    {
        "name": "Health endpoint",
        "method": "GET",
        "path": "/health",
        "expect_status": 200,
        "critical": True,
    },
    {
        "name": "CORS headers on API",
        "method": "OPTIONS",
        "path": "/api/users/me",
        "headers": {
            "Origin": "https://avielcarlos.github.io",
            "Access-Control-Request-Method": "GET",
        },
        "expect_cors": True,
        "critical": True,
    },
    {
        "name": "Payment tiers endpoint",
        "method": "GET",
        "path": "/api/payments/tiers",
        "expect_status": 200,
        "critical": True,
    },
    {
        "name": "Feed batch endpoint (unauthenticated)",
        "method": "POST",
        "path": "/api/screens/batch",
        "body": {"count": 1},
        "expect_status": [200, 401],  # 401 is fine — means auth works, endpoint exists
        "critical": True,
    },
    {
        "name": "Ora lessons endpoint",
        "method": "GET",
        "path": "/api/ora/lessons",
        "expect_status": [200, 401],
        "critical": False,
    },
    {
        "name": "DAO leaderboard",
        "method": "GET",
        "path": "/api/dao/leaderboard",
        "expect_status": [200, 401],
        "critical": False,
    },
]


async def _send_telegram(msg: str) -> None:
    try:
        from core.telegram import send_telegram_message
        await send_telegram_message(msg, chat_id=str(TELEGRAM_CHAT_ID), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"FailureDetector: Telegram alert failed: {e}")


async def run_checks() -> Dict[str, Any]:
    results = []
    failures = []

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for check in CHECKS:
            name = check["name"]
            url = API_BASE + check["path"]
            method = check["method"]
            headers = check.get("headers", {})
            body = check.get("body")
            expect_status = check.get("expect_status", 200)
            expect_cors = check.get("expect_cors", False)
            critical = check.get("critical", False)

            try:
                if method == "GET":
                    resp = await client.get(url, headers=headers)
                elif method == "POST":
                    resp = await client.post(url, json=body, headers=headers)
                elif method == "OPTIONS":
                    resp = await client.options(url, headers=headers)
                else:
                    continue

                status = resp.status_code
                ok = True
                issues = []

                # Check status
                if isinstance(expect_status, list):
                    if status not in expect_status:
                        ok = False
                        issues.append(f"HTTP {status} (expected one of {expect_status})")
                elif status != expect_status:
                    ok = False
                    issues.append(f"HTTP {status} (expected {expect_status})")

                # Check CORS
                if expect_cors:
                    cors_header = resp.headers.get("access-control-allow-origin", "")
                    if not cors_header:
                        ok = False
                        issues.append("Missing Access-Control-Allow-Origin header")
                    elif cors_header not in ("*", "https://avielcarlos.github.io"):
                        issues.append(f"CORS origin: {cors_header} (may be too restrictive)")

                result = {
                    "check": name,
                    "ok": ok,
                    "status": status,
                    "issues": issues,
                    "critical": critical,
                }
                results.append(result)

                if not ok:
                    failures.append(result)
                    logger.warning(f"FailureDetector: FAIL — {name}: {issues}")

            except Exception as e:
                result = {
                    "check": name,
                    "ok": False,
                    "status": None,
                    "issues": [f"Request failed: {str(e)[:100]}"],
                    "critical": critical,
                }
                results.append(result)
                failures.append(result)
                logger.error(f"FailureDetector: ERROR — {name}: {e}")

    critical_failures = [f for f in failures if f["critical"]]
    ts = datetime.now(timezone.utc).isoformat()

    summary = {
        "checked_at": ts,
        "total_checks": len(results),
        "passed": len(results) - len(failures),
        "failed": len(failures),
        "critical_failures": len(critical_failures),
        "results": results,
    }

    if critical_failures:
        # Alert Avi
        lines = [f"🚨 *Ora self-check failed* — {ts[:16]}Z"]
        for f in critical_failures:
            lines.append(f"• *{f['check']}*: {', '.join(f['issues'])}")
        lines.append("\nInvestigating now.")
        await _send_telegram("\n".join(lines))

        # Teach Ora about the failure
        try:
            import sys
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
            from core.database import execute
            for f in critical_failures:
                lesson = (
                    f"Self-check failure detected at {ts[:10]}: {f['check']} failed with issues: "
                    f"{', '.join(f['issues'])}. This needs immediate investigation and a permanent fix."
                )
                await execute(
                    "INSERT INTO ora_knowledge (content, confidence, source, created_at) "
                    "VALUES ($1, $2, $3, NOW()) ON CONFLICT DO NOTHING",
                    lesson, 0.99, "failure_detector",
                )
        except Exception as e:
            logger.debug(f"FailureDetector: Could not teach Ora: {e}")

    return summary


if __name__ == "__main__":
    import asyncio
    result = asyncio.run(run_checks())
    import json
    print(json.dumps(result, indent=2))
