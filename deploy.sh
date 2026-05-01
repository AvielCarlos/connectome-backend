#!/bin/bash
# Deploy Connectome backend to Railway.
# Prerequisites: railway login && railway link (run once)
# Usage: export required env vars below, then ./deploy.sh

set -euo pipefail

echo "🚀 Deploying Connectome backend to Railway..."

required_vars=(
  SECRET_KEY
  ADMIN_TOKEN
  GITHUB_WEBHOOK_SECRET
  CONNECTOME_WORKER_JWT
)

missing=()
for var in "${required_vars[@]}"; do
  if [ -z "${!var:-}" ]; then
    missing+=("$var")
  fi
done

if [ "${FEEDBACK_SCREENSHOT_STORAGE_BACKEND:-s3}" = "s3" ]; then
  for var in \
    FEEDBACK_SCREENSHOT_S3_BUCKET \
    FEEDBACK_SCREENSHOT_S3_ENDPOINT_URL \
    FEEDBACK_SCREENSHOT_S3_ACCESS_KEY_ID \
    FEEDBACK_SCREENSHOT_S3_SECRET_ACCESS_KEY \
    FEEDBACK_SCREENSHOT_PUBLIC_BASE_URL; do
    if [ -z "${!var:-}" ]; then
      missing+=("$var")
    fi
  done
fi

if [ -n "${STRIPE_SECRET_KEY:-}" ] && [ -z "${STRIPE_WEBHOOK_SECRET:-}" ]; then
  missing+=("STRIPE_WEBHOOK_SECRET")
fi

if [ "${#missing[@]}" -gt 0 ]; then
  echo "❌ Missing required production env vars:"
  printf '  - %s\n' "${missing[@]}"
  echo ""
  echo "Set them in your shell or directly in Railway before deploying."
  exit 1
fi

set_var_if_present() {
  local key="$1"
  if [ -n "${!key:-}" ]; then
    echo "Setting $key..."
    railway variables set "$key=${!key}"
  fi
}

# Safe fixed production defaults
railway variables set APP_ENV=production
railway variables set LOG_LEVEL="${LOG_LEVEL:-INFO}"
railway variables set CONNECTOME_API_BASE="${CONNECTOME_API_BASE:-https://connectome-api-production.up.railway.app}"
railway variables set CONNECTOME_REPO_DIR="${CONNECTOME_REPO_DIR:-/app}"
railway variables set CONNECTOME_RUNTIME_DIR="${CONNECTOME_RUNTIME_DIR:-/tmp/connectome}"
railway variables set FEEDBACK_SCREENSHOT_STORAGE_BACKEND="${FEEDBACK_SCREENSHOT_STORAGE_BACKEND:-s3}"

# Required secrets / internal auth
for var in \
  SECRET_KEY \
  ADMIN_TOKEN \
  ADMIN_SECRET \
  ORA_JWT_TOKEN \
  CONNECTOME_WORKER_JWT \
  GITHUB_WEBHOOK_SECRET; do
  set_var_if_present "$var"
done

# Public/app config
for var in \
  CORS_ORIGINS \
  FRONTEND_BASE_URL \
  ADMIN_EMAILS; do
  set_var_if_present "$var"
done

# Integrations
for var in \
  OPENAI_API_KEY \
  ANTHROPIC_API_KEY \
  GOOGLE_PLACES_API_KEY \
  GOOGLE_CLIENT_ID \
  GOOGLE_CLIENT_SECRET \
  GITHUB_TOKEN \
  GITHUB_CLIENT_ID \
  GITHUB_CLIENT_SECRET \
  ORA_TELEGRAM_TOKEN \
  TELEGRAM_BOT_TOKEN \
  STRIPE_SECRET_KEY \
  STRIPE_WEBHOOK_SECRET \
  SERPAPI_KEY \
  EVENTBRITE_TOKEN \
  RAILWAY_API_TOKEN \
  RAILWAY_SERVICE_ID \
  RAILWAY_ENVIRONMENT_ID; do
  set_var_if_present "$var"
done

# Durable screenshot/object storage
for var in \
  FEEDBACK_SCREENSHOT_PUBLIC_BASE_URL \
  FEEDBACK_SCREENSHOT_S3_BUCKET \
  FEEDBACK_SCREENSHOT_S3_REGION \
  FEEDBACK_SCREENSHOT_S3_ENDPOINT_URL \
  FEEDBACK_SCREENSHOT_S3_ACCESS_KEY_ID \
  FEEDBACK_SCREENSHOT_S3_SECRET_ACCESS_KEY; do
  set_var_if_present "$var"
done

# Deploy
echo "Uploading and building..."
railway up --detach

echo ""
echo "✅ Deploy triggered. Useful commands:"
echo "  railway status   — check build/deploy status"
echo "  railway logs     — tail live logs"
echo "  railway domain   — get the public URL"
echo "  railway open     — open Railway dashboard"
