#!/bin/bash
# Deploy Connectome backend to Railway
# Prerequisites: railway login && railway link (run once)
# Usage: ./deploy.sh

set -e

echo "🚀 Deploying Connectome backend to Railway..."

# Set environment variables
if [ -n "$OPENAI_API_KEY" ]; then
  echo "Setting OPENAI_API_KEY..."
  railway variables set OPENAI_API_KEY="$OPENAI_API_KEY"
fi

echo "Setting GOOGLE_PLACES_API_KEY..."
railway variables set GOOGLE_PLACES_API_KEY="$GOOGLE_PLACES_API_KEY"

echo "Setting APP_ENV..."
railway variables set APP_ENV=production

# Deploy
echo "Uploading and building..."
railway up --detach

echo ""
echo "✅ Deployed! Useful commands:"
echo "  railway status   — check build/deploy status"
echo "  railway logs     — tail live logs"
echo "  railway domain   — get the public URL"
echo "  railway open     — open Railway dashboard"
