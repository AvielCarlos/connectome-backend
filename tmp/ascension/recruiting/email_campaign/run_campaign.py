#!/usr/bin/env python3
"""
Ascension Technologies — Daily Email Recruiting Campaign
Finds 5 new AI/dev candidates daily and sends personalized outreach from carlosandromeda8@gmail.com
"""

import json
import os
import sys
import subprocess
import random
import re
from datetime import datetime, timezone
from pathlib import Path
import urllib.request
import urllib.parse

CAMPAIGN_DIR = Path(__file__).parent
SENT_LOG = CAMPAIGN_DIR / "sent_log.json"
CANDIDATES_FILE = CAMPAIGN_DIR / "candidates.json"
DAILY_LIMIT = 5

# ─── Email Templates ──────────────────────────────────────────────────────────

SUBJECT_LINES = [
    "Building an AI that helps people live better — want in?",
    "Open source AI + DAO rewards — Ascension Technologies",
    "You'd be a great fit for what we're building at Connectome",
    "We're building Ora — an AI OS for human fulfilment. Join us.",
    "DAO contributor opportunity — AI project your skills fit perfectly",
]

EMAIL_TEMPLATE = """Hi {name},

I came across your work on {context} and wanted to reach out directly.

I'm Aviel, founder of Ascension Technologies. We're building Connectome — an AI operating system for human fulfilment, powered by Ora, a multi-agent intelligence layer that learns from its users and adapts to the real world.

Here's what makes it different:

• Server-driven UI — Ora generates the entire interface dynamically as JSON, rendered on-device
• Multi-agent architecture — Discovery, Coaching, Exploration, DAO, and World agents working in parallel
• Real-world awareness — Ora reads live weather, moon phases, trending topics, and history to generate personalized life suggestions
• DAO contribution model — contributors (including users) earn CP that compounds over time with longevity multipliers
• Founding Steward program — first 10 contributors to 3,000 CP get permanent governance seats

The stack: FastAPI + PostgreSQL + pgvector + Redis + Expo (React Native) + Railway

We're early and building fast. Everything is open source. Contributions earn CP through our DAO LTV model — not a token, but a real governance stake that grows monthly based on the living impact of your work.

Given your background in {role_context}, you'd be a natural fit for {fit_description}.

If this resonates, I'd love to have a quick conversation. You can also jump straight into the Telegram community: https://t.me/ascensiontechai

Or check the live product: https://avielcarlos.github.io/connectome-web/
DAO: https://atdao.org

Would love to have you involved.

Aviel Carlos
Founder, Ascension Technologies
https://atdao.org | https://t.me/ascensiontechai
"""

# ─── Candidate Pool ──────────────────────────────────────────────────────────

# Manually curated high-quality candidates (will be extended by web search)
CANDIDATE_POOL = [
    {
        "name": "Wassim",
        "email": None,  # no email — GitHub only, skip
        "github": "wassim249",
        "role": "FastAPI + LangGraph Engineer",
        "context": "your fastapi-langgraph-agent-production-ready-template (2,200+ stars)",
        "role_context": "production FastAPI and LangGraph agent systems",
        "fit_description": "the Ora agent orchestration layer and backend API architecture",
    },
    {
        "name": "Developer",
        "email": "contact@ryjox.com",
        "github": "RyjoxTechnologies",
        "role": "AI Memory Systems Engineer",
        "context": "Octopoda-OS — your AI agent memory and persistence system",
        "role_context": "AI agent memory, persistence, and observability",
        "fit_description": "Ora's cross-agent memory consolidation and context persistence layer",
    },
    {
        "name": "Developer",
        "email": "shibing624@gmail.com",
        "github": "shibing624",
        "role": "Python AI Framework Engineer",
        "context": "agentica — your async-first Python agent framework",
        "role_context": "Python async agent frameworks with tool calling and RAG",
        "fit_description": "Ora's async agent execution layer and tool-use patterns",
    },
    {
        "name": "Developer",
        "email": "hoangsonww@gmail.com",
        "github": "hoangsonww",
        "role": "Agentic AI Pipeline Engineer",
        "context": "your Agentic-AI-Pipeline project with tool chaining and RAG",
        "role_context": "agentic AI pipelines, Docker deployments, and AWS",
        "fit_description": "Ora's production deployment pipeline and agent tool-chaining architecture",
    },
    {
        "name": "Developer",
        "email": "rma.mun@proton.me",
        "github": "RMA-MUN",
        "role": "RAG + FastAPI Engineer",
        "context": "your LangChain-RAG-FastAPI-Service — we already had a great conversation on GitHub",
        "role_context": "production RAG pipelines with LangChain and FastAPI",
        "fit_description": "Ora's ContextAgent and cross-agent memory retrieval system",
    },
    {
        "name": "Developer",
        "email": None,
        "github": "agentscope-ai",
        "role": "Agent Runtime Engineer",
        "context": "agentscope-runtime — your production agent execution framework",
        "role_context": "production agent runtimes, sandboxing, and observability",
        "fit_description": "Ora's agent sandboxing and runtime execution layer",
    },
]


def load_sent_log():
    if SENT_LOG.exists():
        with open(SENT_LOG) as f:
            return json.load(f)
    return {"sent": [], "last_run": None, "total_sent": 0}


def save_sent_log(log):
    with open(SENT_LOG, "w") as f:
        json.dump(log, f, indent=2)


def already_sent(log, email):
    return email in [s["email"] for s in log["sent"]]


def send_email(to_email, to_name, subject, body):
    """Send via himalaya CLI"""
    mml = f"""From: Aviel Carlos <carlosandromeda8@gmail.com>
To: {to_name} <{to_email}>
Subject: {subject}

{body}"""

    result = subprocess.run(
        ["himalaya", "message", "send"],
        input=mml.encode(),
        capture_output=True,
        timeout=30,
    )

    if result.returncode == 0:
        return True, "sent"
    else:
        err = result.stderr.decode()
        return False, err


def run_campaign():
    log = load_sent_log()
    sent_today = 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Count how many sent today already
    sent_today_count = sum(1 for s in log["sent"] if s.get("date", "").startswith(today))
    remaining = DAILY_LIMIT - sent_today_count

    if remaining <= 0:
        print(f"Daily limit reached ({DAILY_LIMIT} emails). Done for today.")
        return

    print(f"Sending up to {remaining} emails today ({sent_today_count} already sent)...")

    # Filter candidates with emails not yet contacted
    candidates = [c for c in CANDIDATE_POOL if c.get("email") and not already_sent(log, c["email"])]

    if not candidates:
        print("No new candidates with email addresses available. Add more to the pool.")
        return

    # Shuffle for variety
    random.shuffle(candidates)

    for candidate in candidates[:remaining]:
        subject = random.choice(SUBJECT_LINES)
        body = EMAIL_TEMPLATE.format(
            name=candidate["name"],
            context=candidate["context"],
            role_context=candidate["role_context"],
            fit_description=candidate["fit_description"],
        )

        print(f"Sending to {candidate['email']} ({candidate['name']})...")
        success, msg = send_email(
            candidate["email"],
            candidate["name"],
            subject,
            body,
        )

        record = {
            "email": candidate["email"],
            "name": candidate["name"],
            "github": candidate.get("github", ""),
            "role": candidate["role"],
            "subject": subject,
            "date": datetime.now(timezone.utc).isoformat(),
            "status": "sent" if success else f"failed: {msg[:100]}",
        }
        log["sent"].append(record)
        log["total_sent"] = len([s for s in log["sent"] if s["status"] == "sent"])
        log["last_run"] = datetime.now(timezone.utc).isoformat()
        save_sent_log(log)

        if success:
            sent_today += 1
            print(f"  ✅ Sent to {candidate['email']}")
        else:
            print(f"  ❌ Failed: {msg[:100]}")

    print(f"\nDone. Sent {sent_today} emails today. Total campaign: {log['total_sent']} sent.")
    return sent_today


if __name__ == "__main__":
    run_campaign()
