-- Suggestion integration + CP award automation
-- Links app feedback to actionable suggestions and records adoption/implementation CP.

ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS content TEXT;
ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS body TEXT;
ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'manual';
ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS source_id TEXT;
ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS integration_status TEXT DEFAULT 'pending';
ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS integration_reference TEXT;
ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS triage_metadata JSONB DEFAULT '{}'::jsonb;
ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS adopted_cp_awarded INTEGER DEFAULT 0;
ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS adopted_at TIMESTAMPTZ;

UPDATE user_suggestions
SET title = COALESCE(title, content, body, 'Suggestion')
WHERE title IS NULL;

UPDATE user_suggestions
SET updated_at = COALESCE(updated_at, created_at, NOW())
WHERE updated_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_user_suggestions_integration_status
    ON user_suggestions(integration_status);

CREATE UNIQUE INDEX IF NOT EXISTS idx_user_suggestions_source_unique
    ON user_suggestions(source, source_id)
    WHERE source IS NOT NULL AND source_id IS NOT NULL;
