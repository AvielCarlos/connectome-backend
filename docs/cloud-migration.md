# Connectome Backend Cloud Migration Checklist

## Current target

Run Connectome backend safely in Railway/cloud without depending on Avi's laptop, OpenClaw local secrets, local CLIs, or ephemeral-only production storage.

## Completed in current branch

- Runtime file paths moved under `CONNECTOME_RUNTIME_DIR`.
- Telegram alerts use env vars via `core.telegram`.
- Hard-coded laptop secret paths removed.
- Hard-coded test-login fallbacks removed from production paths.
- Production startup guard added via `settings.validate_production_safety()`.
- Admin routes no longer accept `connectome-admin-secret` as a default token.
- GitHub webhook fails closed in production if `GITHUB_WEBHOOK_SECRET` is missing.
- Stripe webhooks fail closed in production if Stripe is enabled without `STRIPE_WEBHOOK_SECRET`.
- Local `himalaya` email delivery replaced with SMTP env configuration.
- OpenClaw cron CLI checks are opt-in via `OPENCLAW_CLI_ENABLED`.
- Legacy `gog` Drive sync is skipped in production unless explicitly enabled via `GOG_CLI_ENABLED`.
- `/api/drive/*` routes use OAuth-backed `DriveAgentV2` instead of the local `gog` CLI.
- WebSpawn Railway CLI deploy fallback is dev-only; production requires Railway API IDs.
- `.env.example`, `render.yaml`, `deploy.sh`, and `requirements.txt` updated.

## Required production env vars before deploying this branch

Mandatory:

- `APP_ENV=production`
- `DATABASE_URL`
- `REDIS_URL`
- `SECRET_KEY` — non-default, 32+ chars
- `ADMIN_TOKEN` or `ADMIN_SECRET` — non-default, 32+ chars
- `GITHUB_WEBHOOK_SECRET`
- `CONNECTOME_WORKER_JWT` or `ORA_JWT_TOKEN`
- `FEEDBACK_SCREENSHOT_STORAGE_BACKEND=s3`
- S3/R2 screenshot storage vars:
  - `FEEDBACK_SCREENSHOT_S3_BUCKET`
  - `FEEDBACK_SCREENSHOT_S3_ENDPOINT_URL`
  - `FEEDBACK_SCREENSHOT_S3_ACCESS_KEY_ID`
  - `FEEDBACK_SCREENSHOT_S3_SECRET_ACCESS_KEY`
  - `FEEDBACK_SCREENSHOT_PUBLIC_BASE_URL` is optional; omit it for private ephemeral screenshot processing.

If Stripe is enabled:

- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`

If customer service delivery should email users:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_FROM_EMAIL`

Recommended integrations:

- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `GOOGLE_PLACES_API_KEY`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GITHUB_CLIENT_ID`
- `GITHUB_CLIENT_SECRET`
- `ORA_TELEGRAM_TOKEN` or `TELEGRAM_BOT_TOKEN`
- `RAILWAY_API_TOKEN`, `RAILWAY_SERVICE_ID`, `RAILWAY_ENVIRONMENT_ID`

## Avi involvement needed

1. Confirm Cloudflare R2 lifecycle rule deletes leftover screenshot objects after 1 day.
2. Verify Resend DNS for `atdao.org`, then rotate the Resend API key that was pasted in chat.
3. Choose/provision the cloud host for OpenClaw Gateway so Nea's runtime can move off Avi's Mac.

## Verification gates

Local/static:

```bash
python3 -m compileall -q core api ora main.py
git diff --check
```

Production safety simulation:

```bash
env -i PATH="$PATH" PYTHONPATH=. APP_ENV=production \
  DATABASE_URL=postgresql://u:p@db.railway.internal:5432/connectome \
  REDIS_URL=redis://redis.railway.internal:6379 \
  SECRET_KEY=12345678901234567890123456789012 \
  ADMIN_TOKEN=abcdefabcdefabcdefabcdefabcdefab \
  GITHUB_WEBHOOK_SECRET=githubsecret \
  CONNECTOME_WORKER_JWT=jwt \
  FEEDBACK_SCREENSHOT_STORAGE_BACKEND=s3 \
  python3 - <<'PY'
from core.config import settings
settings.validate_production_safety()
print('production validation OK')
PY
```

After deploy:

```bash
curl -fsS https://connectome-api-production.up.railway.app/health
curl -s -o /dev/null -w '%{http_code}' https://connectome-api-production.up.railway.app/api/auth/github/callback
```

Expected callback status is `422`, not `404`.

## Remaining non-blocking follow-up

- Retire the legacy `DriveAgent` object from `AuraBrain` after discovery/search paths are fully verified against `DriveAgentV2`.
- Add cloud scheduler telemetry to replace local OpenClaw cron inspection.
- Move OpenClaw Gateway to a Linux VPS/systemd host and keep the Mac as an optional node for macOS-only tools.
- Consider making `/health` return non-200 when DB/Redis are degraded, or add a separate strict `/ready` endpoint for Railway.
