-- Migration 006: Google OAuth Tokens + User Auth Provider
-- Enables Google Sign-In and per-user Google Drive integration.
-- Added: 2026-04-27
--
-- Run via: psql $DATABASE_URL -f migrations/006_google_oauth.sql
-- (Also baked into core/database.py and runs automatically on startup.)

-- Track which auth provider the user used (email or google)
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS auth_provider VARCHAR(20) DEFAULT 'email';

-- Store the Google account ID for linking
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS google_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS users_google_id_idx
    ON users(google_id) WHERE google_id IS NOT NULL;

-- OAuth tokens + Drive integration state per user
CREATE TABLE IF NOT EXISTS google_oauth_tokens (
    id                  SERIAL PRIMARY KEY,
    user_id             UUID REFERENCES users(id) ON DELETE CASCADE UNIQUE,
    access_token        TEXT,
    refresh_token       TEXT,
    token_expiry        TIMESTAMPTZ,
    scopes              TEXT[],
    drive_connected     BOOLEAN DEFAULT FALSE,
    drive_privacy_level VARCHAR(20) DEFAULT 'none',  -- 'none', 'goals_only', 'full'
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS google_oauth_tokens_user_idx
    ON google_oauth_tokens(user_id);
