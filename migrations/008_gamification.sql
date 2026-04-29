-- Migration 008: Gamification — Streaks, XP, Badges, Collections
-- Implements Duolingo-style retention mechanics + Pinterest-style save collections

-- ─── Streaks ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_streaks (
    id                  SERIAL PRIMARY KEY,
    user_id             UUID REFERENCES users(id) ON DELETE CASCADE UNIQUE,
    current_streak      INT DEFAULT 0,
    longest_streak      INT DEFAULT 0,
    last_activity_date  DATE,
    streak_frozen_until DATE,           -- freeze card = skip one day
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS streaks_user_idx ON user_streaks(user_id);
CREATE INDEX IF NOT EXISTS streaks_last_activity_idx ON user_streaks(last_activity_date);

-- ─── XP Log ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS xp_log (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
    amount      INT NOT NULL,
    reason      VARCHAR(80) NOT NULL,   -- 'card_view', 'card_rate', 'goal_step', 'daily_login', etc.
    ref_id      VARCHAR(120),           -- optional: goal_id, card_id, etc.
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS xp_log_user_idx       ON xp_log(user_id);
CREATE INDEX IF NOT EXISTS xp_log_created_at_idx ON xp_log(created_at);

-- XP totals view for performance
CREATE OR REPLACE VIEW user_xp AS
    SELECT user_id, SUM(amount) AS total_xp
    FROM xp_log
    GROUP BY user_id;

-- ─── Badges ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_badges (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
    badge_key   VARCHAR(60) NOT NULL,
    badge_name  VARCHAR(80) NOT NULL,
    badge_emoji VARCHAR(8)  NOT NULL,
    earned_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, badge_key)
);

CREATE INDEX IF NOT EXISTS badges_user_idx ON user_badges(user_id);

-- ─── Collections (Pinterest/Airbnb wishlist) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS collections (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
    name        VARCHAR(120) NOT NULL,
    emoji       VARCHAR(8) DEFAULT '✦',
    color       VARCHAR(20) DEFAULT '#00d4aa',
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS collections_user_idx ON collections(user_id);

CREATE TABLE IF NOT EXISTS collection_items (
    id              BIGSERIAL PRIMARY KEY,
    collection_id   UUID REFERENCES collections(id) ON DELETE CASCADE,
    screen_spec_id  VARCHAR(120),
    card_title      VARCHAR(255),
    card_body       TEXT,
    card_domain     VARCHAR(60),
    card_color      VARCHAR(20),
    saved_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(collection_id, screen_spec_id)
);

CREATE INDEX IF NOT EXISTS collection_items_col_idx ON collection_items(collection_id);

-- ─── Seed default "Saved" collection for existing users ──────────────────────
-- (will be populated lazily on first save)

-- Done.
COMMENT ON TABLE user_streaks   IS 'Daily activity streaks — Duolingo-style retention';
COMMENT ON TABLE xp_log         IS 'XP event log for gamification';
COMMENT ON TABLE user_badges    IS 'Achievement badges earned by users';
COMMENT ON TABLE collections    IS 'Pinterest-style save collections (wishlists)';
COMMENT ON TABLE collection_items IS 'Items saved to collections';
