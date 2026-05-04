"""
QAAgent — Daily automated quality checks on all key endpoints.

Reports to: CTO Agent
Schedule: daily 3am Pacific
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

import httpx

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)
QA_DIR = os.path.join(os.getenv("CONNECTOME_RUNTIME_DIR", "/tmp/connectome"), "qa")
QA_BASE_URL = os.getenv("CONNECTOME_API_BASE", "https://connectome-api-production.up.railway.app")
QA_TEST_EMAIL = os.getenv("CONNECTOME_TEST_EMAIL")
QA_TEST_PASSWORD = os.getenv("CONNECTOME_TEST_PASSWORD")

ENDPOINTS = [
    {"method": "GET",  "path": "/health",              "expect_key": None,           "name": "health"},
    {"method": "POST", "path": "/api/users/login",     "expect_key": "access_token", "name": "login"},
    {"method": "POST", "path": "/api/screens/next",    "expect_key": "title",        "name": "screens_next"},
    {"method": "GET",  "path": "/api/dao/tasks",       "expect_key": None,           "name": "dao_tasks"},
    {"method": "GET",  "path": "/api/services/catalog","expect_key": None,           "name": "services_catalog"},
    {"method": "GET",  "path": "/api/executive/agents","expect_key": None,           "name": "executive_agents"},
    {"method": "GET",  "path": "/api/goals",           "expect_key": None,           "name": "goals"},
    {"method": "GET",  "path": "/api/aura/lessons",     "expect_key": None,           "name": "aura_lessons"},
    {"method": "GET",  "path": "/api/users/me",        "expect_key": None,           "name": "user_me"},
    {"method": "GET",  "path": "/api/screens",         "expect_key": None,           "name": "screens"},
]


class QAAgent(BaseWorkerAgent):
    name = "qa_agent"
    role = "QA Engineer"
    reports_to = "CTO"

    async def run(self) -> None:
        logger.info("QAAgent: starting daily endpoint checks")
        os.makedirs(QA_DIR, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        token = await self._get_jwt()
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        results = []
        failures = []

        async with httpx.AsyncClient(timeout=30, base_url=QA_BASE_URL) as client:
            for ep in ENDPOINTS:
                if ep["name"] == "login" and (not QA_TEST_EMAIL or not QA_TEST_PASSWORD):
                    results.append({"name": ep["name"], "status": "skipped", "reason": "CONNECTOME_TEST_EMAIL/PASSWORD not configured"})
                    continue
                t0 = time.perf_counter()
                try:
                    if ep["method"] == "GET":
                        resp = await client.get(ep["path"], headers=headers)
                    else:
                        body = ep.get("body") or {"email": QA_TEST_EMAIL, "password": QA_TEST_PASSWORD}
                        resp = await client.post(ep["path"], json=body, headers=headers)

                    elapsed_ms = round((time.perf_counter() - t0) * 1000)
                    ok = resp.status_code in (200, 201)

                    if ok and ep.get("expect_key"):
                        body_data = resp.json() if resp.content else {}
                        if ep["expect_key"] not in (body_data if isinstance(body_data, dict) else {}):
                            ok = False

                    result = {
                        "endpoint": ep["name"],
                        "path": ep["path"],
                        "status_code": resp.status_code,
                        "elapsed_ms": elapsed_ms,
                        "passed": ok,
                    }
                except Exception as e:
                    elapsed_ms = round((time.perf_counter() - t0) * 1000)
                    result = {
                        "endpoint": ep["name"],
                        "path": ep["path"],
                        "status_code": 0,
                        "elapsed_ms": elapsed_ms,
                        "passed": False,
                        "error": str(e),
                    }

                results.append(result)
                if not result["passed"]:
                    failures.append(result)
                status_char = "v" if result["passed"] else "x"
                logger.info(f"QA: {ep['name']} [{status_char}] {result.get('status_code')} ({result['elapsed_ms']}ms)")

        # Save report
        pass_rate = round((len(results) - len(failures)) / len(results) * 100, 1) if results else 0
        report = {
            "date": today,
            "results": results,
            "failures": failures,
            "pass_rate": pass_rate,
        }
        path = os.path.join(QA_DIR, f"daily_{today}.json")
        self._save_json(path, report)

        # Alert on failures
        if failures:
            names = ", ".join(f["endpoint"] for f in failures)
            await self.escalate(f"QA FAILURES ({today}): {len(failures)}/{len(results)} checks failed -- {names}")
            for f in failures:
                ep_name = f["endpoint"]
                ep_path = f["path"]
                ep_status = f.get("status_code", "?")
                ep_error = f.get("error", "unexpected response")
                cmd = (
                    'gh issue create --repo AvielCarlos/connectome-backend '
                    '--label bug --label qa '
                    '--title "QA failure: ' + ep_name + ' endpoint" '
                    '--body "Endpoint ' + ep_path + ' failed QA on ' + today +
                    '. Status: ' + str(ep_status) + '. Error: ' + ep_error + '"'
                )
                self._sh(cmd)

        logger.info(f"QAAgent: done. {len(results)-len(failures)}/{len(results)} passed.")

    async def report(self) -> str:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        data = self._load_json(f"{QA_DIR}/daily_{today}.json", {})
        return f"QAAgent: {data.get('pass_rate','?')}% pass rate ({today})"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(QAAgent().run())
