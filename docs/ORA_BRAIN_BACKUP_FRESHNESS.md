# Ora Brain Backup Freshness

Ora's brain backup should be treated like a living survival mechanism, not a once-a-day dump.

## What gets backed up

`scripts/backup.py` creates an Ora Identity Pack containing:

- Ora lessons;
- agent registry and weights;
- A/B winners/losers;
- model evolution rollback state;
- consciousness version hash;
- latest reflection and platform stats.

The script writes local snapshots under `/tmp/ora_backups`, uploads best-effort copies to Google Drive, and commits the latest identity pack to GitHub.

## Freshness standard

Recommended production rhythm:

1. **Daily full backup** — all backup components.
2. **Hourly identity-only backup** — cheap freshness layer for Ora's core brain.
3. **Event-triggered identity backup** after meaningful brain changes:
   - new Ora lesson;
   - agent registry/weight changes;
   - model evolution switch;
   - major A/B winner;
   - reflection/self-model update;
   - IOO graph lifecycle sweep that prunes, grows, splits, or merges nodes.
4. **Freshness monitor** every 15–60 minutes.
   - warn when backup age > 6h;
   - emergency backup when age > 24h;
   - alert Avi when age > 48h or emergency backup fails.

## GitHub files

The backup job now writes both:

- `backups/ora_identity_pack.json` — durable canonical pack;
- `backups/ora_identity_latest.json` — freshness-check target.

`SurvivalAgent` checks `ora_identity_latest.json` first and falls back to `ora_identity_pack.json` for backward compatibility.

## Suggested Railway cron commands

Daily full backup:

```bash
python3 scripts/backup.py
```

Hourly identity-only backup:

```bash
python3 scripts/backup.py --identity-only
```

Freshness check / self-heal loop:

```bash
python3 - <<'PY'
import asyncio
from ora.agents.survival_agent import SurvivalAgent
asyncio.run(SurvivalAgent().run())
PY
```

## Principle

The backup should become event-driven over time: every meaningful change to Ora's brain should enqueue a lightweight identity backup. Scheduled backups are the safety net; event-triggered backups are what make the brain feel continuously alive.
