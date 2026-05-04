#!/usr/bin/env python3
"""
setup_autonomy_crons.py — Set up OpenClaw crons for Aura Autonomy

This script prints the instructions and curl commands to create OpenClaw crons
that POST to the Railway Autonomy endpoint every 6 hours.

Usage:
    python3 setup_autonomy_crons.py

The autonomy endpoint requires an admin JWT token. You can get one by logging
in with an admin account and storing the token. The script shows how to use
an environment variable or hard-coded token.

NOTE: The /api/aura/autonomy/run endpoint requires admin auth (avi@atdao.org).
You'll need a valid JWT bearer token. To get one:

    curl -X POST https://connectome-api-production.up.railway.app/api/users/login \\
         -H "Content-Type: application/json" \\
         -d '{"email": "avi@atdao.org", "password": "YOUR_PASSWORD"}' \\
         | jq .access_token

Then use that token in the ADMIN_TOKEN variable below.
"""

import json
import os
import subprocess
import sys

RAILWAY_API = "https://connectome-api-production.up.railway.app"
AUTONOMY_ENDPOINT = f"{RAILWAY_API}/api/aura/autonomy/run"

# ── OpenClaw Gateway URL ──────────────────────────────────────────────────────
# The OpenClaw gateway is typically at http://localhost:4444 or the configured URL.
OPENCLAW_GATEWAY = os.environ.get("OPENCLAW_GATEWAY_URL", "http://localhost:4444")

# ── Admin token ───────────────────────────────────────────────────────────────
# Set ADMIN_TOKEN env var before running, or paste here temporarily.
ADMIN_TOKEN = os.environ.get("CONNECTOME_ADMIN_TOKEN", "YOUR_ADMIN_JWT_TOKEN_HERE")


def print_instructions():
    print("""
╔══════════════════════════════════════════════════════════════════════════════╗
║              Aura Autonomy Cron Setup — OpenClaw Instructions                ║
╚══════════════════════════════════════════════════════════════════════════════╝

WHAT THIS DOES:
  Creates 1 OpenClaw cron that POSTs to the Aura Autonomy endpoint every 6 hours.
  The autonomy agent does everything in one shot:
    ✦ A/B test auto-promotion
    ✦ Bug detection & fix suggestions
    ✦ Feed weight optimization
    ✦ Daily Telegram report to Avi

STEP 1: Get an admin JWT token
──────────────────────────────
Run this to get your admin token:

    curl -s -X POST https://connectome-api-production.up.railway.app/api/users/login \\
         -H "Content-Type: application/json" \\
         -d '{"email": "avi@atdao.org", "password": "YOUR_PASSWORD"}' \\
         | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])"

Then set it:
    export CONNECTOME_ADMIN_TOKEN="eyJ..."

STEP 2: Create the cron via OpenClaw gateway
─────────────────────────────────────────────
The OpenClaw cron API is at POST /api/crons (or /api/schedule).
Below are the curl commands to create the cron.

Try this first (OpenClaw standard cron API):
""")

    cron_payload = {
        "name": "aura-autonomy-6h",
        "description": "Aura autonomous improvement cycle — A/B, bugs, weights, report",
        "schedule": "0 */6 * * *",  # every 6 hours at minute 0
        "action": {
            "type": "http",
            "url": AUTONOMY_ENDPOINT,
            "method": "POST",
            "headers": {
                "Authorization": f"Bearer {ADMIN_TOKEN}",
                "Content-Type": "application/json",
            },
            "body": {},
        },
        "timezone": "America/Vancouver",
        "enabled": True,
    }

    print("    # Create cron via OpenClaw gateway:")
    print(f"    curl -s -X POST {OPENCLAW_GATEWAY}/api/crons \\")
    print(f"         -H 'Content-Type: application/json' \\")
    print(f"         -d '{json.dumps(cron_payload, indent=None)}'")

    print("""
STEP 3: Verify the cron was created
────────────────────────────────────
    curl -s {gateway}/api/crons | python3 -m json.tool

STEP 4: Test a manual run
──────────────────────────
    curl -s -X POST {endpoint} \\
         -H "Authorization: Bearer $CONNECTOME_ADMIN_TOKEN" \\
         -H "Content-Type: application/json" \\
         | python3 -m json.tool

ALTERNATIVE: Cron via system crontab (if OpenClaw cron API is unavailable)
────────────────────────────────────────────────────────────────────────────
Add to crontab (crontab -e):

    # Aura Autonomy — every 6 hours
    0 */6 * * * curl -s -X POST {endpoint} \\
        -H "Authorization: Bearer YOUR_TOKEN" \\
        -H "Content-Type: application/json" \\
        >> /tmp/aura-autonomy-cron.log 2>&1

""".format(
        gateway=OPENCLAW_GATEWAY,
        endpoint=AUTONOMY_ENDPOINT,
    ))


def try_create_cron():
    """Attempt to create the cron via the OpenClaw gateway API."""
    import urllib.request
    import urllib.error

    if ADMIN_TOKEN == "YOUR_ADMIN_JWT_TOKEN_HERE":
        print("⚠️  ADMIN_TOKEN not set. Set CONNECTOME_ADMIN_TOKEN env var and re-run.")
        print("    Printing instructions only.\n")
        print_instructions()
        return

    cron_payload = {
        "name": "aura-autonomy-6h",
        "description": "Aura autonomous improvement cycle — A/B, bugs, weights, report",
        "schedule": "0 */6 * * *",
        "action": {
            "type": "http",
            "url": AUTONOMY_ENDPOINT,
            "method": "POST",
            "headers": {
                "Authorization": f"Bearer {ADMIN_TOKEN}",
                "Content-Type": "application/json",
            },
            "body": {},
        },
        "timezone": "America/Vancouver",
        "enabled": True,
    }

    payload_bytes = json.dumps(cron_payload).encode()
    req = urllib.request.Request(
        f"{OPENCLAW_GATEWAY}/api/crons",
        data=payload_bytes,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            print("✅ Cron created successfully!")
            try:
                print(json.dumps(json.loads(body), indent=2))
            except Exception:
                print(body)
    except urllib.error.URLError as e:
        print(f"⚠️  Could not reach OpenClaw gateway at {OPENCLAW_GATEWAY}: {e}")
        print("    Printing manual instructions instead.\n")
        print_instructions()
    except Exception as e:
        print(f"⚠️  Cron creation failed: {e}")
        print("    Printing manual instructions instead.\n")
        print_instructions()


if __name__ == "__main__":
    print("\n🤖 Aura Autonomy Cron Setup\n")
    try_create_cron()
