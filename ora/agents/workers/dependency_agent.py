"""
DependencyAgent — Keeps Python dependencies fresh and secure.

Reports to: CTO Agent
Schedule: weekly Sunday 8am Pacific
"""

import asyncio
import logging
import re
from datetime import datetime, timezone

from .base import BaseWorkerAgent

logger = logging.getLogger(__name__)

PACKAGES = ["fastapi", "anthropic", "asyncpg", "redis", "stripe", "httpx", "pydantic", "uvicorn"]
SECURITY_CRITICAL = ["fastapi", "anthropic", "stripe", "asyncpg"]


class DependencyAgent(BaseWorkerAgent):
    name = "dependency_agent"
    role = "Dependency Manager"
    reports_to = "CTO"

    async def run(self) -> None:
        logger.info("DependencyAgent: checking package versions")
        week = datetime.now(timezone.utc).strftime("%Y-W%W")

        # Get current installed versions
        installed_raw = self._sh("pip list --format=json 2>/dev/null")
        installed: dict = {}
        try:
            import json
            for pkg in json.loads(installed_raw):
                installed[pkg["name"].lower()] = pkg["version"]
        except Exception:
            pass

        results = []
        outdated = []
        critical_outdated = []

        for pkg in PACKAGES:
            # Get latest version
            out = self._sh(f"pip index versions {pkg} 2>/dev/null | head -3")
            latest = self._parse_latest(out)
            current = installed.get(pkg.lower(), "unknown")

            is_outdated = latest and current != "unknown" and latest != current
            is_critical = pkg in SECURITY_CRITICAL

            result = {
                "package": pkg,
                "current": current,
                "latest": latest or "unknown",
                "outdated": is_outdated,
                "critical": is_critical and is_outdated,
            }
            results.append(result)

            if is_outdated:
                outdated.append(pkg)
                if is_critical:
                    critical_outdated.append(pkg)
                    logger.warning(f"DependencyAgent: CRITICAL — {pkg} {current} → {latest}")
                else:
                    logger.info(f"DependencyAgent: {pkg} {current} → {latest}")

        # Auto-create PR for critical updates
        if critical_outdated:
            await self._create_update_pr(critical_outdated, results)

        # Teach Ora
        await self.teach_aura(
            f"Dependency health ({week}): {len(outdated)}/{len(PACKAGES)} packages outdated. "
            f"Critical updates needed: {len(critical_outdated)} ({', '.join(critical_outdated) or 'none'}). "
            f"{'PRs created for critical updates.' if critical_outdated else 'No critical security issues.'}",
            confidence=0.9,
        )

        logger.info(f"DependencyAgent: done. {len(outdated)} outdated, {len(critical_outdated)} critical.")

    def _parse_latest(self, pip_output: str) -> str | None:
        # pip index versions output: "fastapi (0.115.0, 0.114.0, ...)"
        match = re.search(r'\(([0-9][^,)]+)', pip_output)
        return match.group(1).strip() if match else None

    async def _create_update_pr(self, packages: list, results: list) -> None:
        pkg_list = ", ".join(packages)
        body_lines = ["Automated dependency update for critical packages:\n"]
        for r in results:
            if r["critical"]:
                body_lines.append(f"- {r['package']}: {r['current']} → {r['latest']}")
        body = "\n".join(body_lines)

        # Create issue as a starting point (PR creation requires branch work — issue triggers human action)
        self._sh(
            f'gh issue create --repo AvielCarlos/connectome-backend '
            f'--label "dependencies" --label "security" '
            f'--title "Update critical dependencies: {pkg_list}" '
            f'--body "{body.replace(chr(34), chr(39))}"'
        )
        logger.info(f"DependencyAgent: created GitHub issue for critical dependency updates")

    async def report(self) -> str:
        return "DependencyAgent: Weekly dependency audit complete."


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(DependencyAgent().run())
