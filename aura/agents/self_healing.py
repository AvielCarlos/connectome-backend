"""
SelfHealingAgent — watches Connectome's own logs and fixes errors autonomously.

Safe/Observe-only mode by default. Set SELF_HEALING_ENABLED=true to enable auto-fix.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SELF_HEALING_ENABLED = os.environ.get("SELF_HEALING_ENABLED", "false").lower() == "true"
LOG_FILE = os.environ.get("LOG_FILE", "")
CONTAINER_NAME = os.environ.get("BACKEND_CONTAINER", "connectome_backend")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ErrorEvent:
    raw: str
    category: str   # 'import_error' | 'missing_module' | 'version_mismatch' | 'db_error' | 'api_error' | 'unknown'
    context: str    # last 10 lines of log for context
    confidence: float


@dataclass
class ProposedFix:
    description: str
    fix_type: str   # 'pip_install' | 'file_edit' | 'restart' | 'migration' | 'config_change'
    commands: list = field(default_factory=list)
    file_edits: list = field(default_factory=list)
    confidence: float = 0.0
    risk: str = "medium"   # 'low' | 'medium' | 'high'


# ---------------------------------------------------------------------------
# Pre-seeded pattern library
# ---------------------------------------------------------------------------

BUILTIN_KNOWN_FIXES = [
    {
        "error_pattern": r"ModuleNotFoundError: No module named 'distutils'",
        "error_category": "missing_module",
        "fix_description": "Install redis[asyncio] which bundles the missing distutils shim",
        "fix_type": "pip_install",
        "commands": ["pip install 'redis[asyncio]'"],
        "file_edits": [],
    },
    {
        "error_pattern": r"ValueError: unknown type: public\.vector",
        "error_category": "db_error",
        "fix_description": "Enable pgvector extension in PostgreSQL",
        "fix_type": "migration",
        "commands": ["psql $DATABASE_URL -c 'CREATE EXTENSION IF NOT EXISTS vector'"],
        "file_edits": [],
    },
    {
        "error_pattern": r"ModuleNotFoundError: No module named '(?P<mod>[^']+)'",
        "error_category": "missing_module",
        "fix_description": "Install missing Python module via pip",
        "fix_type": "pip_install",
        "commands": ["pip install {mod}"],   # {mod} is substituted at runtime
        "file_edits": [],
    },
    {
        "error_pattern": r"(bcrypt|passlib).*error|error.*(bcrypt|passlib)",
        "error_category": "import_error",
        "fix_description": "Reinstall bcrypt directly (passlib compatibility layer removed)",
        "fix_type": "pip_install",
        "commands": ["pip install bcrypt --upgrade"],
        "file_edits": [],
    },
]

# Regex → category mapping for fast classification
ERROR_PATTERNS: list[tuple[re.Pattern, str, float]] = [
    (re.compile(r"ModuleNotFoundError|ImportError", re.I),       "import_error",       0.9),
    (re.compile(r"No module named",                re.I),        "missing_module",     0.95),
    (re.compile(r"version.*mismatch|requires.*but.*found", re.I),"version_mismatch",   0.8),
    (re.compile(r"asyncpg|psycopg|sqlalchemy.*error|DB error", re.I), "db_error",      0.85),
    (re.compile(r"HTTPError|ConnectionRefusedError|aiohttp", re.I), "api_error",       0.75),
    (re.compile(r"Traceback|Error:|Exception:", re.I),            "unknown",           0.5),
]


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class SelfHealingAgent:
    """
    Watches Connectome's own logs in real-time.
    When errors are detected, reasons about the fix, applies it, and restarts.
    Stores error→fix pairs to learn from past repairs.
    """

    def __init__(self):
        self.watching: bool = False
        self._log_buffer: list[str] = []   # rolling last-10 lines
        self._buffer_max = 10
        self._watch_task: Optional[asyncio.Task] = None
        self._errors_fixed_today: int = 0
        self._enabled = SELF_HEALING_ENABLED

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def start_watching(self):
        """Start background log-monitoring loop."""
        if self.watching:
            logger.info("[SelfHealingAgent] already watching")
            return
        self.watching = True
        self._watch_task = asyncio.create_task(self._watch_loop())
        mode = "AUTO-FIX" if self._enabled else "OBSERVE-ONLY"
        logger.info(f"[SelfHealingAgent] 👁  started ({mode})")

    async def stop_watching(self):
        """Graceful shutdown."""
        self.watching = False
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
        logger.info("[SelfHealingAgent] stopped")

    def status(self) -> dict:
        return {
            "watching": self.watching,
            "mode": "auto_fix" if self._enabled else "observe_only",
            "errors_fixed_today": self._errors_fixed_today,
        }

    # ------------------------------------------------------------------
    # Watch loop
    # ------------------------------------------------------------------

    async def _watch_loop(self):
        """Main loop: read Docker logs + optional log file."""
        tasks = []
        tasks.append(asyncio.create_task(self._watch_docker()))
        if LOG_FILE:
            tasks.append(asyncio.create_task(self._watch_file(LOG_FILE)))
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            raise

    async def _watch_docker(self):
        """Tail Docker container logs via subprocess."""
        cmd = ["docker", "logs", CONTAINER_NAME, "--tail", "1", "--follow"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            while self.watching:
                line_bytes = await proc.stdout.readline()
                if not line_bytes:
                    await asyncio.sleep(1)
                    continue
                line = line_bytes.decode("utf-8", errors="replace").rstrip()
                await self.analyze_line(line)
        except FileNotFoundError:
            logger.debug("[SelfHealingAgent] docker not available — skipping container watch")
        except Exception as e:
            logger.warning(f"[SelfHealingAgent] Docker watch error: {e}")

    async def _watch_file(self, path: str):
        """Tail a log file."""
        p = Path(path)
        if not p.exists():
            logger.warning(f"[SelfHealingAgent] LOG_FILE {path} not found")
            return
        with open(p, "r") as f:
            f.seek(0, 2)  # seek to end
            while self.watching:
                line = f.readline()
                if line:
                    await self.analyze_line(line.rstrip())
                else:
                    await asyncio.sleep(0.2)

    # ------------------------------------------------------------------
    # 2. Analyze line
    # ------------------------------------------------------------------

    async def analyze_line(self, line: str) -> Optional[ErrorEvent]:
        """Classify a log line. Returns None if normal, ErrorEvent if error."""
        # Update rolling buffer
        self._log_buffer.append(line)
        if len(self._log_buffer) > self._buffer_max:
            self._log_buffer.pop(0)

        category, confidence = self._classify_line(line)
        if category is None:
            return None

        error = ErrorEvent(
            raw=line,
            category=category,
            context="\n".join(self._log_buffer),
            confidence=confidence,
        )
        logger.warning(f"[SelfHealingAgent] 🚨 Error detected [{category}] conf={confidence:.2f}: {line[:120]}")
        asyncio.create_task(self.diagnose_and_fix(error))
        return error

    def _classify_line(self, line: str) -> tuple[Optional[str], float]:
        """Pattern-match a line to an error category."""
        for pattern, category, confidence in ERROR_PATTERNS:
            if pattern.search(line):
                return category, confidence
        return None, 0.0

    # ------------------------------------------------------------------
    # 3. Diagnose and fix
    # ------------------------------------------------------------------

    async def diagnose_and_fix(self, error: ErrorEvent):
        """Find or generate a fix, then apply if safe."""
        # 1. Check known_fixes in DB first
        fix = await self._lookup_known_fix(error)

        # 2. Fall back to LLM / pattern-based reasoning
        if fix is None:
            fix = await self._generate_fix(error)

        if fix is None:
            logger.info(f"[SelfHealingAgent] No fix found for: {error.raw[:80]}")
            await self._record_to_db(error, None, False)
            return

        logger.info(
            f"[SelfHealingAgent] 💡 Fix proposed [{fix.fix_type}] "
            f"conf={fix.confidence:.2f} risk={fix.risk}: {fix.description}"
        )

        # 3. Decide whether to apply
        if not self._enabled:
            logger.info(f"[SelfHealingAgent] OBSERVE-ONLY — would apply: {fix.commands}")
            await self._record_to_db(error, fix, None)  # None = not attempted
            return

        if fix.confidence >= 0.7 and fix.risk == "low":
            await self.apply_fix(error, fix)
        else:
            logger.info(
                f"[SelfHealingAgent] Skipping auto-apply "
                f"(confidence={fix.confidence:.2f}, risk={fix.risk}) — stored for review"
            )
            await self._record_to_db(error, fix, None)

    # ------------------------------------------------------------------
    # 4. Apply fix
    # ------------------------------------------------------------------

    async def apply_fix(self, error: ErrorEvent, fix: ProposedFix):
        """Apply the proposed fix: run commands, apply file edits, restart if needed."""
        success = True
        ran_commands = []

        for cmd in fix.commands:
            logger.info(f"[SelfHealingAgent] ▶ Running: {cmd}")
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                ran_commands.append(cmd)
                if proc.returncode != 0:
                    logger.error(
                        f"[SelfHealingAgent] Command failed (rc={proc.returncode}): "
                        f"{stderr.decode('utf-8', errors='replace')[:300]}"
                    )
                    success = False
                    break
                else:
                    logger.info(f"[SelfHealingAgent] ✅ Command succeeded: {cmd}")
            except asyncio.TimeoutError:
                logger.error(f"[SelfHealingAgent] Command timed out: {cmd}")
                success = False
                break
            except Exception as e:
                logger.error(f"[SelfHealingAgent] Command error: {e}")
                success = False
                break

        # Apply file edits
        if success and fix.file_edits:
            for edit in fix.file_edits:
                try:
                    path = Path(edit["path"])
                    content = path.read_text()
                    content = content.replace(edit["old_text"], edit["new_text"])
                    path.write_text(content)
                    logger.info(f"[SelfHealingAgent] ✅ File edited: {edit['path']}")
                except Exception as e:
                    logger.error(f"[SelfHealingAgent] File edit error: {e}")
                    success = False

        # Restart backend if fix type warrants it
        if success and fix.fix_type in ("pip_install", "migration", "file_edit"):
            logger.info("[SelfHealingAgent] 🔄 Triggering backend restart...")
            try:
                proc = await asyncio.create_subprocess_shell(
                    "docker-compose restart backend",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=60)
            except Exception as e:
                logger.warning(f"[SelfHealingAgent] Restart failed: {e}")

        await self.record_outcome(error, fix, success)

    # ------------------------------------------------------------------
    # 5. Record outcome
    # ------------------------------------------------------------------

    async def record_outcome(self, error: ErrorEvent, fix: ProposedFix, success: bool):
        """Store event in DB; promote to known_fixes on success."""
        await self._record_to_db(error, fix, success)

        if success:
            self._errors_fixed_today += 1
            await self._promote_to_known_fix(error, fix)
            logger.info(f"[SelfHealingAgent] 🎉 Fix successful — promoted to known_fixes")
        else:
            logger.warning(f"[SelfHealingAgent] Fix failed — recorded as ineffective")

    # ------------------------------------------------------------------
    # Internal DB helpers
    # ------------------------------------------------------------------

    async def _record_to_db(
        self,
        error: ErrorEvent,
        fix: Optional[ProposedFix],
        success: Optional[bool],
    ):
        """Write to self_healing_events table."""
        try:
            from core.database import execute
            await execute(
                """
                INSERT INTO self_healing_events
                    (error_category, error_raw, fix_description, fix_type,
                     commands_run, success, confidence, risk)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                error.category,
                error.raw[:2000],
                fix.description if fix else None,
                fix.fix_type if fix else None,
                json.dumps(fix.commands) if fix else None,
                success,
                fix.confidence if fix else error.confidence,
                fix.risk if fix else None,
            )
        except Exception as e:
            logger.warning(f"[SelfHealingAgent] DB record error: {e}")

    async def _lookup_known_fix(self, error: ErrorEvent) -> Optional[ProposedFix]:
        """Check known_fixes table for a matching pattern."""
        try:
            from core.database import fetch
            rows = await fetch(
                "SELECT * FROM known_fixes WHERE error_category = $1 ORDER BY success_count DESC",
                error.category,
            )
            for row in rows:
                try:
                    pattern = re.compile(row["error_pattern"], re.I)
                    if pattern.search(error.raw):
                        commands = json.loads(row["commands"]) if row["commands"] else []
                        # Substitute named groups from the error pattern
                        m = pattern.search(error.raw)
                        if m:
                            groups = m.groupdict()
                            commands = [c.format(**groups) for c in commands]
                        return ProposedFix(
                            description=row["fix_description"],
                            fix_type=row["fix_type"],
                            commands=commands,
                            file_edits=json.loads(row["file_edits"]) if row["file_edits"] else [],
                            confidence=0.85,
                            risk="low",
                        )
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"[SelfHealingAgent] known_fix lookup error: {e}")
        return None

    async def _generate_fix(self, error: ErrorEvent) -> Optional[ProposedFix]:
        """Use LLM or built-in patterns to generate a fix."""
        # First try built-in patterns (no DB lookup needed)
        for entry in BUILTIN_KNOWN_FIXES:
            try:
                pattern = re.compile(entry["error_pattern"], re.I)
                m = pattern.search(error.raw)
                if m:
                    commands = list(entry["commands"])
                    groups = m.groupdict()
                    if groups:
                        commands = [c.format(**groups) for c in commands]
                    return ProposedFix(
                        description=entry["fix_description"],
                        fix_type=entry["fix_type"],
                        commands=commands,
                        file_edits=list(entry["file_edits"]),
                        confidence=0.8,
                        risk="low",
                    )
            except Exception:
                continue

        # Try LLM-based reasoning
        return await self._llm_generate_fix(error)

    async def _llm_generate_fix(self, error: ErrorEvent) -> Optional[ProposedFix]:
        """Call the LLM to reason about a fix. Falls back to mock if unavailable."""
        try:
            import openai
            api_key = os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                raise ValueError("No OPENAI_API_KEY")

            client = openai.AsyncOpenAI(api_key=api_key)
            prompt = f"""You are an autonomous DevOps agent for a Python/FastAPI backend called Connectome.
An error was detected in the logs. Reason about the most likely fix.

Error category: {error.category}
Error line: {error.raw}
Context (last 10 log lines):
{error.context}

Respond with a JSON object ONLY (no markdown):
{{
  "description": "...",
  "fix_type": "pip_install|file_edit|restart|migration|config_change",
  "commands": ["shell command 1", ...],
  "file_edits": [{{"path": "...", "old_text": "...", "new_text": "..."}}],
  "confidence": 0.0-1.0,
  "risk": "low|medium|high"
}}

Only suggest low-risk, safe fixes. If unsure, set risk to 'high'.
"""
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=500,
                timeout=20,
            )
            raw = resp.choices[0].message.content.strip()
            data = json.loads(raw)
            return ProposedFix(
                description=data.get("description", "LLM-generated fix"),
                fix_type=data.get("fix_type", "restart"),
                commands=data.get("commands", []),
                file_edits=data.get("file_edits", []),
                confidence=float(data.get("confidence", 0.5)),
                risk=data.get("risk", "high"),
            )
        except Exception as e:
            logger.debug(f"[SelfHealingAgent] LLM fix generation failed: {e}")
            return None

    async def _promote_to_known_fix(self, error: ErrorEvent, fix: ProposedFix):
        """Promote a successful fix to known_fixes, or increment success_count."""
        try:
            from core.database import fetchrow, execute

            # Find a matching known fix to increment
            existing = await fetchrow(
                """
                SELECT id FROM known_fixes
                WHERE error_category = $1 AND fix_description = $2
                LIMIT 1
                """,
                error.category,
                fix.description,
            )
            if existing:
                await execute(
                    """
                    UPDATE known_fixes
                    SET success_count = success_count + 1, last_used = NOW()
                    WHERE id = $1
                    """,
                    existing["id"],
                )
            else:
                # Try to derive an error pattern
                pattern = _derive_pattern(error.raw, error.category)
                await execute(
                    """
                    INSERT INTO known_fixes
                        (error_pattern, error_category, fix_description, fix_type,
                         commands, file_edits, success_count, last_used)
                    VALUES ($1, $2, $3, $4, $5, $6, 1, NOW())
                    """,
                    pattern,
                    error.category,
                    fix.description,
                    fix.fix_type,
                    json.dumps(fix.commands),
                    json.dumps(fix.file_edits),
                )
        except Exception as e:
            logger.warning(f"[SelfHealingAgent] promote_to_known_fix error: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _derive_pattern(raw: str, category: str) -> str:
    """Extract a reasonable regex pattern from an error line."""
    # Use the first 80 chars, escaped, as a starting point
    truncated = raw[:80]
    # Escape special regex chars, then unescale common wildcards
    escaped = re.escape(truncated)
    return escaped


async def seed_known_fixes():
    """Seed built-in patterns into the known_fixes table on startup."""
    try:
        from core.database import fetchval, execute
        for entry in BUILTIN_KNOWN_FIXES:
            existing = await fetchval(
                "SELECT id FROM known_fixes WHERE error_pattern = $1",
                entry["error_pattern"],
            )
            if not existing:
                await execute(
                    """
                    INSERT INTO known_fixes
                        (error_pattern, error_category, fix_description, fix_type,
                         commands, file_edits, success_count)
                    VALUES ($1, $2, $3, $4, $5, $6, 0)
                    """,
                    entry["error_pattern"],
                    entry["error_category"],
                    entry["fix_description"],
                    entry["fix_type"],
                    json.dumps(entry["commands"]),
                    json.dumps(entry["file_edits"]),
                )
        logger.info("[SelfHealingAgent] Known fixes seeded")
    except Exception as e:
        logger.warning(f"[SelfHealingAgent] Seed error: {e}")


# Singleton
self_healing_agent = SelfHealingAgent()
