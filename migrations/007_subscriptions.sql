-- Migration 007: Aura's Subscription System
-- Creates the subscriptions table for Stripe-backed tier management.
-- Run after 006_google_oauth.sql

-- ─── Subscriptions ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS subscriptions (
    id                      SERIAL PRIMARY KEY,
    user_id                 UUID REFERENCES users(id) ON DELETE CASCADE UNIQUE,
    tier                    VARCHAR(20) DEFAULT 'free',         -- 'free', 'explorer', 'sovereign'
    stripe_customer_id      VARCHAR(100),
    stripe_subscription_id  VARCHAR(100),
    stripe_price_id         VARCHAR(100),
    status                  VARCHAR(20) DEFAULT 'active',       -- 'active', 'canceled', 'past_due', 'trialing'
    current_period_start    TIMESTAMPTZ,
    current_period_end      TIMESTAMPTZ,
    cancel_at_period_end    BOOLEAN DEFAULT FALSE,
    trial_end               TIMESTAMPTZ,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

-- Indices for fast lookup
CREATE INDEX IF NOT EXISTS subs_user_idx            ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS subs_stripe_customer_idx ON subscriptions(stripe_customer_id);
CREATE INDEX IF NOT EXISTS subs_tier_idx            ON subscriptions(tier);
CREATE INDEX IF NOT EXISTS subs_status_idx          ON subscriptions(status);

-- Backfill: create free-tier subscription rows for existing users
-- who don't have one yet. Safe to run multiple times (ON CONFLICT DO NOTHING).
INSERT INTO subscriptions (user_id, tier, status)
SELECT id, COALESCE(subscription_tier, 'free'), 'active'
FROM users
WHERE id NOT IN (SELECT user_id FROM subscriptions WHERE user_id IS NOT NULL)
ON CONFLICT (user_id) DO NOTHING;

-- ─── interactions table: add interaction_type if missing ──────────────────────
-- (needed for chat_messages_daily and event_recommendations_weekly counts)

ALTER TABLE interactions
    ADD COLUMN IF NOT EXISTS interaction_type VARCHAR(50) DEFAULT 'screen_view';

-- ─── journal_entries: ensure user_id + created_at are indexed ─────────────────
CREATE INDEX IF NOT EXISTS journal_user_created_idx
    ON journal_entries(user_id, created_at)
    WHERE created_at IS NOT NULL;

-- Done.
COMMENT ON TABLE subscriptions IS
    'Aura subscription records. Tier managed by PricingAgent + Stripe webhooks.';
