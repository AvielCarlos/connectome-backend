-- ─── 2026-05-04: Rename ora_* tables and columns to aura_* ───────────────────
-- Atomic, idempotent. Preserves all data.
-- Run before deploying the corresponding code change. The middleware in main.py
-- keeps /api/ora/* HTTP routes working as legacy aliases via in-process rewrite.

DO $$
BEGIN
  -- Tables
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ora_lessons') THEN
    ALTER TABLE ora_lessons RENAME TO aura_lessons;
    ALTER INDEX IF EXISTS idx_aura_lessons_created_at RENAME TO idx_aura_lessons_created_at;
    ALTER INDEX IF EXISTS idx_aura_lessons_source RENAME TO idx_aura_lessons_source;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ora_reflections') THEN
    ALTER TABLE ora_reflections RENAME TO aura_reflections;
    ALTER INDEX IF EXISTS idx_aura_reflections_created_at RENAME TO idx_aura_reflections_created_at;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ora_conversations') THEN
    ALTER TABLE ora_conversations RENAME TO aura_conversations;
    ALTER INDEX IF EXISTS idx_aura_conversations_user_id RENAME TO idx_aura_conversations_user_id;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ora_self_checks') THEN
    ALTER TABLE ora_self_checks RENAME TO aura_self_checks;
    ALTER INDEX IF EXISTS idx_aura_self_checks_created_at RENAME TO idx_aura_self_checks_created_at;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ora_heal_events') THEN
    ALTER TABLE ora_heal_events RENAME TO aura_heal_events;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ora_knowledge') THEN
    ALTER TABLE ora_knowledge RENAME TO aura_knowledge;
  END IF;

  -- Columns: ora_note in (probably) screen_specs / users; ora_narrative; ora_evaluation; ora_confidence
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE column_name = 'ora_note'
  ) THEN
    -- iterate every table that has the column
    PERFORM 1;  -- placeholder; per-table renames done below
  END IF;
END$$;

-- Per-table column renames (idempotent)
DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT table_schema, table_name, column_name
    FROM information_schema.columns
    WHERE column_name IN ('ora_note', 'ora_narrative', 'ora_evaluation', 'ora_confidence')
  LOOP
    EXECUTE format(
      'ALTER TABLE %I.%I RENAME COLUMN %I TO %I',
      r.table_schema,
      r.table_name,
      r.column_name,
      replace(r.column_name, 'ora_', 'aura_')
    );
  END LOOP;
END$$;

-- Update any 'pending'/'ora_review' enum-like text values inside status columns
UPDATE community_proposals
SET status = 'aura_review'
WHERE status = 'ora_review';

-- More tables
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ora_heal_policies') THEN
    ALTER TABLE ora_heal_policies RENAME TO aura_heal_policies;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ora_knowledge_graph') THEN
    ALTER TABLE ora_knowledge_graph RENAME TO aura_knowledge_graph;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ora_messages') THEN
    ALTER TABLE ora_messages RENAME TO aura_messages;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ora_surfaces') THEN
    ALTER TABLE ora_surfaces RENAME TO aura_surfaces;
  END IF;
END$$;

-- Status enum updates
UPDATE contributions SET status = 'aura_review' WHERE status = 'ora_review';

-- Profile JSONB key migration: users.profile['ora_memory'] → users.profile['aura_memory']
UPDATE users
SET profile = jsonb_set(
    (profile - 'ora_memory'),
    '{aura_memory}',
    profile->'ora_memory',
    true
)
WHERE profile ? 'ora_memory';

-- system_config: rename ora_system_prompt key
UPDATE system_config SET key = 'aura_system_prompt' WHERE key = 'ora_system_prompt';
