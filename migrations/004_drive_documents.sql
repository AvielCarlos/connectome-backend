-- Migration 004: Google Drive Documents
-- Ora's long-term memory from the user's own writing.
-- Added: 2026-04-27
--
-- This table stores indexed Google Drive documents with pgvector embeddings
-- so Ora can do semantic search over personal notes during coaching sessions.
--
-- Run via: psql $DATABASE_URL -f migrations/004_drive_documents.sql
-- (The migration is also baked into core/database.py and runs automatically on startup.)

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS drive_documents (
    id            SERIAL PRIMARY KEY,
    drive_id      VARCHAR(200) UNIQUE NOT NULL,
    name          TEXT,
    mime_type     VARCHAR(100),
    content       TEXT,
    embedding     vector(1536),
    last_synced   TIMESTAMPTZ DEFAULT NOW(),
    modified_time TIMESTAMPTZ
);

-- Fast lookup by Drive file ID
CREATE INDEX IF NOT EXISTS idx_drive_documents_drive_id
    ON drive_documents(drive_id);

-- Recency index for status queries
CREATE INDEX IF NOT EXISTS idx_drive_documents_last_synced
    ON drive_documents(last_synced DESC);

-- IVFFlat ANN index for cosine similarity search
-- lists=20 is appropriate for up to ~10k documents; tune upward as corpus grows.
CREATE INDEX IF NOT EXISTS idx_drive_documents_embedding
    ON drive_documents USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 20);
