-- Migration 005: Drive Document Owner Isolation
-- Adds per-user ownership to drive_documents to ensure personal Drive data
-- is NEVER accessible to other users. This is a hard privacy requirement.
-- Added: 2026-04-27
--
-- Run via: psql $DATABASE_URL -f migrations/005_drive_privacy.sql
-- (Also baked into core/database.py and runs automatically on startup.)

-- Add owner column (idempotent)
ALTER TABLE drive_documents
    ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES users(id);

-- Index for fast per-user lookups
CREATE INDEX IF NOT EXISTS drive_docs_owner_idx
    ON drive_documents(owner_user_id);

-- Backfill: assign all pre-existing docs to the oldest/first user (Avi)
UPDATE drive_documents
SET owner_user_id = (
    SELECT id FROM users ORDER BY created_at ASC LIMIT 1
)
WHERE owner_user_id IS NULL;
