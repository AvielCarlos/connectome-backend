-- Migration 005: Live Events Intelligence
-- Adds events table + user location/preference columns
-- Run via database.py run_migrations() at startup — fully idempotent.

-- ============================================================
-- Events table
-- ============================================================

CREATE TABLE IF NOT EXISTS events (
    id SERIAL PRIMARY KEY,
    external_id VARCHAR(200) UNIQUE,
    title TEXT NOT NULL,
    description TEXT,
    category VARCHAR(100),          -- music, tech, wellness, arts, food, sports, networking, etc.
    venue_name TEXT,
    address TEXT,
    city VARCHAR(100),
    latitude FLOAT,
    longitude FLOAT,
    starts_at TIMESTAMPTZ,
    ends_at TIMESTAMPTZ,
    url TEXT,
    image_url TEXT,
    price_range TEXT,               -- 'free', '$10-$20', '$50+', etc.
    source VARCHAR(50),             -- 'serpapi', 'eventbrite', 'meetup_scrape', etc.
    relevance_tags TEXT[],          -- ['wellness', 'community', 'tech']
    embedding vector(1536),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS events_city_idx      ON events(city);
CREATE INDEX IF NOT EXISTS events_starts_at_idx ON events(starts_at);
CREATE INDEX IF NOT EXISTS events_source_idx    ON events(source);
CREATE INDEX IF NOT EXISTS events_category_idx  ON events(category);
-- Note: ivfflat index requires data; created lazily by event_agent after first bulk load.
-- CREATE INDEX IF NOT EXISTS events_embedding_idx ON events USING ivfflat (embedding vector_cosine_ops);

-- ============================================================
-- User location + event preference columns
-- ============================================================

ALTER TABLE users ADD COLUMN IF NOT EXISTS city              VARCHAR(100);
ALTER TABLE users ADD COLUMN IF NOT EXISTS location_lat      FLOAT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS location_lng      FLOAT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS event_preferences TEXT[];   -- ['wellness', 'tech', 'music']
