#!/usr/bin/env python3
"""Fail if costly/API secrets are present in files that can become public.

Real secrets belong only in managed secret stores (Railway variables, GitHub
Secrets, 1Password, etc.), never in repo files, docs, examples, screenshots, or
agent notes.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Stripe live secret key", re.compile(r"sk_live_[A-Za-z0-9_\-]{20,}")),
    ("Stripe restricted key", re.compile(r"rk_live_[A-Za-z0-9_\-]{20,}")),
    ("Stripe webhook secret", re.compile(r"whsec_[A-Za-z0-9_\-]{20,}")),
    ("OpenAI API key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{40,}")),
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{30,}")),
    ("Perplexity API key", re.compile(r"pplx-[A-Za-z0-9_\-]{30,}")),
    ("Resend API key", re.compile(r"re_[A-Za-z0-9]{32,}")),
    ("SendGrid API key", re.compile(r"SG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}")),
    ("GitHub token", re.compile(r"gh(?:p|o|u|s|r)_[A-Za-z0-9_]{30,}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{25,}")),
    ("Google OAuth client secret", re.compile(r"GOCSPX-[0-9A-Za-z_\-]{20,}")),
    ("Telegram bot token", re.compile(r"\b\d{8,12}:AA[0-9A-Za-z_\-]{30,}")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----")),
    ("Hard-coded secret assignment", re.compile(r"(?i)(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"](?!your_|example|changeme|placeholder|dummy|test|xxx|\*\*\*|\$|\$\{)[^'\"]{20,}['\"]")),
]

PUBLIC_NOTE_FILES = {
    "TOOLS.md", "MEMORY.md", "SOUL.md", "AGENTS.md", "DREAMS.md", "USER.md", "IDENTITY.md", "HEARTBEAT.md",
}

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".pytest_cache", ".astro"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".mp4", ".mov", ".mp3", ".wav", ".sqlite", ".db"}
ALLOWLIST_REL = {"scripts/guard_no_public_secrets.py"}


def repo_root() -> Path:
    try:
        out = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
        return Path(out)
    except Exception:
        return Path.cwd()


def tracked_files(root: Path) -> list[Path]:
    out = subprocess.check_output(["git", "ls-files"], cwd=root, text=True)
    return [root / line for line in out.splitlines() if line.strip()]


def all_candidate_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            p = Path(dirpath) / filename
            if p.suffix.lower() not in BINARY_SUFFIXES:
                files.append(p)
    return files


def staged_files(root: Path) -> list[Path]:
    out = subprocess.check_output(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"], cwd=root, text=True)
    return [root / line for line in out.splitlines() if line.strip()]


def is_text(path: Path) -> bool:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return False
    try:
        with path.open("rb") as f:
            chunk = f.read(4096)
        return b"\0" not in chunk
    except OSError:
        return False


def scan_file(path: Path, root: Path) -> list[str]:
    rel = path.relative_to(root)
    rel_s = rel.as_posix()
    if rel_s in ALLOWLIST_REL:
        return []
    issues: list[str] = []
    if rel.name in PUBLIC_NOTE_FILES and path.exists():
        issues.append(f"{rel}: private/reference note file must not be tracked or committed")
    if not is_text(path):
        return issues
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return issues
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            issues.append(f"{rel}: contains {label}; move it to a secret store and redact the file")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", action="store_true", help="scan only staged files")
    parser.add_argument("--all", action="store_true", help="scan tracked + untracked text files")
    args = parser.parse_args()
    root = repo_root()
    files = staged_files(root) if args.staged else (all_candidate_files(root) if args.all else tracked_files(root))
    issues: list[str] = []
    for path in files:
        if path.exists():
            issues.extend(scan_file(path, root))
    if issues:
        print("Secret/public-file guard failed:\n", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        print("\nDo not place live keys, paid API tokens, passwords, or private agent notes in public repo files/docs.", file=sys.stderr)
        return 1
    print("Secret/public-file guard passed.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
