-- Connectome migrations reference
-- The authoritative migration runner is in database.py (run_migrations).
-- This file is a human-readable reference for all schema definitions.

-- ============================================================
-- Core tables (original)
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMP DEFAULT NOW(),
    last_active TIMESTAMP,
    email VARCHAR(255) UNIQUE,
    hashed_password TEXT,
    embedding vector(1536),
    subscription_tier VARCHAR(20) DEFAULT 'free',
    streak_current INTEGER DEFAULT 0,
    xp_level INTEGER DEFAULT 1,
    fulfilment_score FLOAT DEFAULT 0.0,
    profile JSONB DEFAULT '{}'
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS streak_current INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS xp_level INTEGER DEFAULT 1;

CREATE TABLE IF NOT EXISTS screen_specs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    spec JSONB NOT NULL,
    agent_type VARCHAR(50),
    global_rating FLOAT DEFAULT 0,
    impression_count INT DEFAULT 0,
    completion_count INT DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Screen Pattern Library lifecycle:
-- create/reuse → test variants → reinforce winners → trim stale/unused/low-outcome patterns.
CREATE TABLE IF NOT EXISTS screen_patterns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    pattern_type TEXT,
    intent TEXT,
    domain TEXT,
    template JSONB DEFAULT '{}',
    embedding vector(1536),
    usage_count INT DEFAULT 0,
    success_score FLOAT DEFAULT 0.0,
    outcome_score FLOAT DEFAULT 0.0,
    last_used_at TIMESTAMP,
    deprecated_at TIMESTAMP,
    pruned_at TIMESTAMP,
    prune_reason TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_screen_patterns_active
    ON screen_patterns(domain, pattern_type)
    WHERE pruned_at IS NULL AND deprecated_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_screen_patterns_prune
    ON screen_patterns(last_used_at, usage_count, outcome_score)
    WHERE pruned_at IS NULL;

CREATE TABLE IF NOT EXISTS screen_pattern_variants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pattern_id UUID REFERENCES screen_patterns(id) ON DELETE CASCADE,
    name TEXT,
    variant_key TEXT,
    spec_patch JSONB DEFAULT '{}',
    usage_count INT DEFAULT 0,
    success_score FLOAT DEFAULT 0.0,
    outcome_score FLOAT DEFAULT 0.0,
    last_used_at TIMESTAMP,
    deprecated_at TIMESTAMP,
    pruned_at TIMESTAMP,
    prune_reason TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_screen_pattern_variants_pattern
    ON screen_pattern_variants(pattern_id)
    WHERE pruned_at IS NULL;

CREATE TABLE IF NOT EXISTS interactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    screen_spec_id UUID REFERENCES screen_specs(id) ON DELETE SET NULL,
    rating INT,
    time_on_screen_ms INT,
    exit_point VARCHAR(100),
    completed BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS goals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    title TEXT,
    description TEXT,
    status VARCHAR(20) DEFAULT 'active',
    steps JSONB DEFAULT '[]',
    progress FLOAT DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ab_tests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) UNIQUE,
    variants JSONB DEFAULT '{}',
    results JSONB DEFAULT '{}',
    status VARCHAR(20) DEFAULT 'running',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS revenue_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    event_type VARCHAR(50),
    amount_cents INT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS daily_screen_counts (
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    date DATE DEFAULT CURRENT_DATE,
    count INT DEFAULT 0,
    PRIMARY KEY (user_id, date)
);

-- ============================================================
-- Intelligence Enhancement tables (added)
-- ============================================================

-- 1. Exit Intent Classification
--    Stores LLM-classified reasons for why a user left a screen.
CREATE TABLE IF NOT EXISTS exit_classifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    interaction_id UUID REFERENCES interactions(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    screen_spec_id UUID REFERENCES screen_specs(id) ON DELETE SET NULL,
    reason TEXT,
    category VARCHAR(50),   -- content_mismatch | timing | offer_failed | attention_lost | unknown
    confidence FLOAT,
    suggested_improvement TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_exit_classifications_screen_spec_id
    ON exit_classifications(screen_spec_id);

CREATE INDEX IF NOT EXISTS idx_exit_classifications_user_id
    ON exit_classifications(user_id);

-- 2. Session-End Summary
--    Ora's internal summary of what happened in a session.
CREATE TABLE IF NOT EXISTS session_summaries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    session_started_at TIMESTAMP,
    session_ended_at TIMESTAMP,
    screens_shown INT DEFAULT 0,
    highly_rated INT DEFAULT 0,
    early_exits INT DEFAULT 0,
    emerging_interests JSONB DEFAULT '[]',
    avoid_topics JSONB DEFAULT '[]',
    ora_note TEXT,
    fulfilment_delta FLOAT DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_session_summaries_user_id
    ON session_summaries(user_id);

-- 3. Re-engagement Push Notification Scheduler
--    Scheduled notifications Ora sends when a user exits mid-goal.
CREATE TABLE IF NOT EXISTS scheduled_notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    goal_id UUID REFERENCES goals(id) ON DELETE SET NULL,
    message TEXT,
    scheduled_for TIMESTAMP,
    sent BOOLEAN DEFAULT FALSE,
    opened BOOLEAN DEFAULT FALSE,
    return_rate_signal FLOAT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scheduled_notifications_user_id
    ON scheduled_notifications(user_id);

CREATE INDEX IF NOT EXISTS idx_scheduled_notifications_scheduled_for
    ON scheduled_notifications(scheduled_for)
    WHERE sent = FALSE;

-- ============================================================
-- Anti-Hallucination Safeguards (Safeguards 1, 2, 3)
-- ============================================================

-- Safeguard 1: Evidence thresholds
-- How many data points existed when the classification was made.
ALTER TABLE exit_classifications ADD COLUMN IF NOT EXISTS
    data_points_at_classification INT DEFAULT 0;

-- Safeguard 2: Consistency checking
-- Was the classification flagged as potentially inconsistent?
ALTER TABLE exit_classifications ADD COLUMN IF NOT EXISTS
    consistency_flagged BOOLEAN DEFAULT FALSE;

-- Human-readable note from the consistency checker.
ALTER TABLE exit_classifications ADD COLUMN IF NOT EXISTS
    consistency_note TEXT;

-- Safeguard 3: Ground truth labels from direct user prompts.
CREATE TABLE IF NOT EXISTS ground_truth_labels (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    interaction_id UUID REFERENCES interactions(id) ON DELETE CASCADE,
    exit_classification_id UUID REFERENCES exit_classifications(id) ON DELETE CASCADE,
    user_answer VARCHAR(50),   -- 'too_long' | 'not_interesting' | 'wrong_topic' | 'just_browsing' | 'other'
    answered_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ground_truth_labels_user_id
    ON ground_truth_labels(user_id);

CREATE INDEX IF NOT EXISTS idx_ground_truth_labels_ec_id
    ON ground_truth_labels(exit_classification_id);

-- ============================================================
-- WorldAgent: Real-world signals table
-- ============================================================

CREATE TABLE IF NOT EXISTS world_signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source VARCHAR(100),
    signal_type VARCHAR(50),             -- 'event' | 'trend' | 'inspiration' | 'opportunity' | 'weather' | 'historical'
    title TEXT,
    summary TEXT,
    url TEXT,
    location VARCHAR(100) DEFAULT '',
    tags JSONB DEFAULT '[]',
    relevance_score FLOAT DEFAULT 0.5,
    fetched_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_world_signals_fetched_at
    ON world_signals(fetched_at DESC);

CREATE INDEX IF NOT EXISTS idx_world_signals_signal_type
    ON world_signals(signal_type);

CREATE INDEX IF NOT EXISTS idx_world_signals_source
    ON world_signals(source);

-- ============================================================
-- FeedbackExperimenter: Meta-learning feedback A/B system
-- ============================================================

-- feedback_experiments: Ora's A/B tests on feedback collection methods
CREATE TABLE IF NOT EXISTS feedback_experiments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hypothesis TEXT,
    mechanism_type VARCHAR(50),
    control_mechanism VARCHAR(50) DEFAULT 'star_rating',
    screen_types JSONB,           -- which screen types this applies to
    status VARCHAR(20) DEFAULT 'running',  -- running | completed | failed
    sample_size_target INT DEFAULT 100,
    control_count INT DEFAULT 0,
    treatment_count INT DEFAULT 0,
    control_response_rate FLOAT DEFAULT 0,
    treatment_response_rate FLOAT DEFAULT 0,
    control_signal_quality FLOAT DEFAULT 0,
    treatment_signal_quality FLOAT DEFAULT 0,
    p_value FLOAT,
    winner VARCHAR(20),           -- control | treatment | inconclusive
    summary TEXT,
    started_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP,
    duration_days INT DEFAULT 7,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_feedback_experiments_status
    ON feedback_experiments(status);

-- experiment_signals: individual signals collected during experiments
CREATE TABLE IF NOT EXISTS experiment_signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    experiment_id UUID REFERENCES feedback_experiments(id),
    user_id UUID REFERENCES users(id),
    screen_spec_id UUID REFERENCES screen_specs(id),
    variant VARCHAR(20),          -- control | treatment
    mechanism_type VARCHAR(50),
    raw_signal JSONB,
    normalized_score FLOAT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_experiment_signals_experiment_id
    ON experiment_signals(experiment_id);

CREATE INDEX IF NOT EXISTS idx_experiment_signals_user_id
    ON experiment_signals(user_id);

-- ora_lessons: Ora's growing knowledge base, written by all agents
CREATE TABLE IF NOT EXISTS ora_lessons (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source VARCHAR(50),           -- 'feedback_experiment' | 'exit_analysis' | 'session_summary' | 'world_agent'
    lesson TEXT,
    confidence FLOAT DEFAULT 0.7,
    applied BOOLEAN DEFAULT FALSE,
    applies_to JSONB,             -- {screen_types: [...], user_segments: [...]}
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ora_lessons_created_at
    ON ora_lessons(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ora_lessons_source
    ON ora_lessons(source);

-- Seed first experiment
INSERT INTO feedback_experiments
    (hypothesis, mechanism_type, control_mechanism, screen_types, duration_days)
VALUES (
    'Emoji reactions will achieve higher response rates than star ratings on discovery cards due to lower interaction friction',
    'emoji_reaction',
    'star_rating',
    '["discovery_card", "opportunity_card"]',
    7
)
ON CONFLICT DO NOTHING;

-- ============================================================
-- OraConsciousness tables (added 2026-04-25)
-- ============================================================

CREATE TABLE IF NOT EXISTS ora_reflections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    period_start TIMESTAMP,
    period_end TIMESTAMP,
    decisions_made INT,
    top_performing_content JSONB,
    underperforming_areas JSONB,
    new_lessons_learned JSONB,
    model_changes JSONB,
    uncertainty_areas JSONB,
    self_note TEXT,
    fulfilment_delta_global FLOAT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ora_conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    role VARCHAR(20),  -- 'user' | 'ora'
    message TEXT,
    context JSONB,     -- user state at time of message
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ora_self_checks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    aligned BOOLEAN,
    issues JSONB,
    actions_taken JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ora_reflections_created_at
    ON ora_reflections(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ora_conversations_user_id
    ON ora_conversations(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ora_self_checks_created_at
    ON ora_self_checks(created_at DESC);

-- ora_memory is stored in users.profile JSONB (no separate table needed).
-- Format: plain prose paragraph, max 500 chars.
-- Key: users.profile['ora_memory']

-- ============================================================
-- Model Evolution System (Part 5)
-- ============================================================

-- model_candidates: discovered models being evaluated
CREATE TABLE IF NOT EXISTS model_candidates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_id VARCHAR(100),
    provider VARCHAR(50),
    discovered_at TIMESTAMP DEFAULT NOW(),
    eval_score FLOAT,
    status VARCHAR(20) DEFAULT 'discovered', -- discovered | evaluating | shadow | active | rejected
    notes TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_model_candidates_model_provider
    ON model_candidates(model_id, provider);

-- system_config: key/value store for dynamic Ora configuration
CREATE TABLE IF NOT EXISTS system_config (
    key VARCHAR(100) PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Seed: default active model
INSERT INTO system_config (key, value)
VALUES ('active_model', 'gpt-4o')
ON CONFLICT (key) DO NOTHING;

-- ============================================================
-- CollectiveIntelligenceAgent tables (added 2026-04-25)
-- ============================================================

-- collective_wisdom: aggregate insights computed every 24h across all users
-- PRIVACY: Only stores aggregate data — AVG/COUNT/GROUP BY results.
-- Individual user data is NEVER stored here.
CREATE TABLE IF NOT EXISTS collective_wisdom (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    computed_at TIMESTAMP DEFAULT NOW(),
    total_users_analyzed INT,
    total_interactions_analyzed INT,
    fulfilment_drivers JSONB,    -- [{content_type, domain, avg_rating, fulfilment_lift, sample_size}]
    distress_patterns JSONB,     -- [{content_type, domain, distress_signal, suppress_recommendation}]
    temporal_patterns JSONB,     -- {hour: {best_domain, best_agent, avg_engagement}}
    domain_synergies JSONB,      -- ["Users who engage with iVive content Mon-Wed show 34% higher Aventi engagement"]
    surprises JSONB,             -- ["Rest content outperformed motivational content by 22%"]
    collective_voice TEXT        -- LLM synthesis: what humanity is reaching for right now
);

CREATE INDEX IF NOT EXISTS idx_collective_wisdom_computed_at
    ON collective_wisdom(computed_at DESC);

-- collective_suppressions: agent+domain combos causing consistent distress globally
-- Auto-expires after 7 days, re-evaluated on next 24h cycle
CREATE TABLE IF NOT EXISTS collective_suppressions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_type VARCHAR(50),
    domain VARCHAR(10),
    reason TEXT,
    distress_signal FLOAT,   -- skip_fast_rate that triggered suppression
    sample_size INT,         -- number of interactions analyzed
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP     -- auto-expire after 7 days, re-evaluate
);

CREATE INDEX IF NOT EXISTS idx_collective_suppressions_active
    ON collective_suppressions(agent_type, domain)
    WHERE active = TRUE;

-- ============================================================
-- Push notification tokens & onboarding (added 2026-04-26)
-- ============================================================

ALTER TABLE users ADD COLUMN IF NOT EXISTS push_token TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS push_token_updated_at TIMESTAMP;
ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_completed BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_completed_at TIMESTAMP;
ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_variant TEXT;

CREATE TABLE IF NOT EXISTS onboarding_variants (
    id SERIAL PRIMARY KEY,
    variant_id TEXT NOT NULL UNIQUE,
    name TEXT,
    description TEXT,
    questions JSONB NOT NULL,
    active BOOLEAN DEFAULT true,
    weight INTEGER DEFAULT 25,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_onboarding_variant
ON users(onboarding_variant);

CREATE TABLE IF NOT EXISTS discovery_profile (
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    field_value TEXT,
    question_id TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (user_id, field_name)
);

CREATE INDEX IF NOT EXISTS idx_discovery_profile_user_id
ON discovery_profile(user_id);

CREATE INDEX IF NOT EXISTS idx_users_push_token
ON users(push_token) WHERE push_token IS NOT NULL;

-- ============================================================
-- Coaching streaks (added 2026-04-26)
-- ============================================================

CREATE TABLE IF NOT EXISTS coaching_streaks (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    current_streak INT DEFAULT 0,
    longest_streak INT DEFAULT 0,
    last_coaching_date DATE,
    total_sessions INT DEFAULT 0,
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================
-- Subscription premium fields (added 2026-04-26)
-- ============================================================

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_premium BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_since TIMESTAMP;
ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_screen_count INT DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_reset_at TIMESTAMP;

-- ============================================================
-- Goal completion tracking (added 2026-04-26)
-- ============================================================

ALTER TABLE goals ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP;
ALTER TABLE goals ADD COLUMN IF NOT EXISTS celebration_sent BOOLEAN DEFAULT FALSE;

-- ============================================================
-- Retention mechanics (added 2026-04-26)
-- ============================================================

ALTER TABLE users ADD COLUMN IF NOT EXISTS last_daily_checkin_at TIMESTAMP;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_weekly_summary_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS weekly_summaries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    week_start DATE,
    week_end DATE,
    screens_seen INT DEFAULT 0,
    goals_progressed INT DEFAULT 0,
    top_interests JSONB DEFAULT '[]',
    ora_narrative TEXT,
    fulfilment_change FLOAT DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_weekly_summaries_user_id
    ON weekly_summaries(user_id, created_at DESC);

-- ============================================================
-- DAO Contribution + Reward System (added 2026-04-26)
-- ============================================================

-- contributors: public registry of Ascension DAO participants
CREATE TABLE IF NOT EXISTS contributors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    github_username TEXT UNIQUE NOT NULL,
    display_name TEXT,
    telegram_username TEXT,
    email TEXT,
    joined_at TIMESTAMPTZ DEFAULT NOW(),
    total_cp INTEGER DEFAULT 0,
    tier TEXT DEFAULT 'observer',  -- observer, contributor, builder, steward
    bio TEXT
);

CREATE INDEX IF NOT EXISTS idx_contributors_github_username ON contributors(github_username);
CREATE INDEX IF NOT EXISTS idx_contributors_total_cp ON contributors(total_cp DESC);
ALTER TABLE contributors ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id);
CREATE INDEX IF NOT EXISTS idx_contributors_user_id ON contributors(user_id);

-- contributions: individual tracked contributions
CREATE TABLE IF NOT EXISTS contributions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contributor_id UUID REFERENCES contributors(id),
    contribution_type TEXT NOT NULL,  -- code, agent, design, doc, research, feedback, community
    title TEXT NOT NULL,
    description TEXT,
    github_pr_url TEXT,
    submitted_at TIMESTAMPTZ DEFAULT NOW(),
    status TEXT DEFAULT 'pending',  -- pending, ora_review, accepted, rejected
    base_cp INTEGER DEFAULT 0,
    multiplier FLOAT DEFAULT 1.0,
    final_cp INTEGER DEFAULT 0,
    ora_evaluation TEXT,
    ora_confidence FLOAT,
    impact_data JSONB,
    community_votes INTEGER DEFAULT 0,
    community_upvotes INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_contributions_contributor_id ON contributions(contributor_id);
CREATE INDEX IF NOT EXISTS idx_contributions_status ON contributions(status);
CREATE INDEX IF NOT EXISTS idx_contributions_submitted_at ON contributions(submitted_at DESC);
ALTER TABLE contributions ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id);
ALTER TABLE contributions ADD COLUMN IF NOT EXISTS external_link TEXT;
ALTER TABLE contributions ADD COLUMN IF NOT EXISTS evidence_text TEXT;

-- cp_ledger: immutable CP transaction log
CREATE TABLE IF NOT EXISTS cp_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contributor_id UUID REFERENCES contributors(id),
    contribution_id UUID REFERENCES contributions(id),
    cp_amount INTEGER NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cp_ledger_contributor_id ON cp_ledger(contributor_id, created_at DESC);

-- dao_proposals: community governance proposals
CREATE TABLE IF NOT EXISTS dao_proposals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposer_id UUID REFERENCES contributors(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    proposal_type TEXT,  -- feature, governance, budget, direction
    status TEXT DEFAULT 'open',  -- open, voting, passed, rejected
    votes_for INTEGER DEFAULT 0,
    votes_against INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    closes_at TIMESTAMPTZ,
    result_summary TEXT
);

CREATE INDEX IF NOT EXISTS idx_dao_proposals_status ON dao_proposals(status);
CREATE INDEX IF NOT EXISTS idx_dao_proposals_created_at ON dao_proposals(created_at DESC);

-- ============================================================
-- DAO LTV (Lifetime Value) scoring (added 2026-04-26)
-- ============================================================

-- LTV columns on contributions table
ALTER TABLE contributions ADD COLUMN IF NOT EXISTS ltv_cp_total INTEGER DEFAULT 0;
ALTER TABLE contributions ADD COLUMN IF NOT EXISTS ltv_last_evaluated_at TIMESTAMPTZ;
ALTER TABLE contributions ADD COLUMN IF NOT EXISTS ltv_monthly_rate INTEGER DEFAULT 0;
ALTER TABLE contributions ADD COLUMN IF NOT EXISTS is_ltv_active BOOLEAN DEFAULT false;
ALTER TABLE contributions ADD COLUMN IF NOT EXISTS months_active INTEGER DEFAULT 0;

-- Founding steward columns on contributors table
ALTER TABLE contributors ADD COLUMN IF NOT EXISTS is_founding_steward BOOLEAN DEFAULT false;
ALTER TABLE contributors ADD COLUMN IF NOT EXISTS founding_steward_number INTEGER;

CREATE INDEX IF NOT EXISTS idx_contributors_founding_steward
    ON contributors(founding_steward_number)
    WHERE is_founding_steward = TRUE;

CREATE INDEX IF NOT EXISTS idx_contributions_ltv_active
    ON contributions(is_ltv_active, ltv_last_evaluated_at)
    WHERE status = 'accepted' AND is_ltv_active = TRUE;

-- ============================================================
-- IOO Vector Integration (added 2026-04-29)
-- ============================================================

ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS embedding vector(1536);
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS goal_category TEXT;
ALTER TABLE ioo_user_state ADD COLUMN IF NOT EXISTS embedding vector(1536);
ALTER TABLE ioo_user_state ADD COLUMN IF NOT EXISTS embedding_updated_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_ioo_nodes_embedding
    ON ioo_nodes USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 20);
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS step_type TEXT DEFAULT 'hybrid';
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS physical_context TEXT;
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS best_time TEXT;
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS requirements JSONB DEFAULT '{}';
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS estimated_duration_days INTEGER;
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS difficulty_level INTEGER DEFAULT 5;

CREATE TABLE IF NOT EXISTS ioo_node_proposals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    description TEXT,
    goal_category TEXT,
    step_type TEXT DEFAULT 'hybrid',
    domain TEXT,
    tags TEXT[] DEFAULT '{}',
    source_url TEXT,
    confidence FLOAT DEFAULT 0.5,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ioo_node_proposals_status
    ON ioo_node_proposals(status, created_at DESC);
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS prerequisite_nodes UUID[] DEFAULT '{}';
-- IOO neural graph lifecycle: nodes are living possibilities, not static cards.
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS neural_state TEXT DEFAULT 'active';
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS generation_source TEXT DEFAULT 'seed';
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS growth_angle TEXT;
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS parent_node_ids UUID[] DEFAULT '{}';
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS split_from_node_id UUID REFERENCES ioo_nodes(id) ON DELETE SET NULL;
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS merged_into_node_id UUID REFERENCES ioo_nodes(id) ON DELETE SET NULL;
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS merged_from_node_ids UUID[] DEFAULT '{}';
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS spawned_count INT DEFAULT 0;
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS engagement_score NUMERIC(8,4) DEFAULT 0.5;
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS fulfilment_score NUMERIC(8,4) DEFAULT 0.5;
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS last_reinforced_at TIMESTAMPTZ;
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS pruned_at TIMESTAMPTZ;
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS prune_reason TEXT;
CREATE INDEX IF NOT EXISTS idx_ioo_nodes_neural_state ON ioo_nodes(neural_state, is_active);
CREATE INDEX IF NOT EXISTS idx_ioo_nodes_growth_angle ON ioo_nodes(growth_angle);

ALTER TABLE ioo_edges ADD COLUMN IF NOT EXISTS relation_type TEXT DEFAULT 'leads_to';
ALTER TABLE ioo_edges ADD COLUMN IF NOT EXISTS confidence NUMERIC(6,4) DEFAULT 0.5;
ALTER TABLE ioo_edges ADD COLUMN IF NOT EXISTS rationale TEXT;
ALTER TABLE ioo_edges ADD COLUMN IF NOT EXISTS last_reinforced_at TIMESTAMPTZ;
ALTER TABLE ioo_edges ADD COLUMN IF NOT EXISTS pruned_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_ioo_edges_relation_type ON ioo_edges(relation_type);

CREATE TABLE IF NOT EXISTS ioo_graph_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type TEXT NOT NULL,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    node_id UUID REFERENCES ioo_nodes(id) ON DELETE SET NULL,
    related_node_id UUID REFERENCES ioo_nodes(id) ON DELETE SET NULL,
    edge_id UUID REFERENCES ioo_edges(id) ON DELETE SET NULL,
    payload JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ioo_graph_events_type_created
    ON ioo_graph_events(event_type, created_at DESC);

-- Contribution system v2 (GitHub OAuth + attachments)
ALTER TABLE users ADD COLUMN IF NOT EXISTS github_username TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS github_avatar_url TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS github_connected BOOLEAN DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_users_github ON users(github_username);
ALTER TABLE contributions ADD COLUMN IF NOT EXISTS attachment_urls JSONB DEFAULT '[]';
ALTER TABLE contributions ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'manual';
ALTER TABLE contributions ADD COLUMN IF NOT EXISTS source_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_contributions_source_id ON contributions(source_id) WHERE source_id IS NOT NULL;

-- Global app feedback: context + screenshot capture for lightweight CP earning
CREATE TABLE IF NOT EXISTS app_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    category TEXT NOT NULL DEFAULT 'Other',
    message TEXT NOT NULL,
    route TEXT,
    screenshot_data_url TEXT,
    screenshot_url TEXT,
    screenshot_key TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE app_feedback ADD COLUMN IF NOT EXISTS screenshot_url TEXT;
ALTER TABLE app_feedback ADD COLUMN IF NOT EXISTS screenshot_key TEXT;
CREATE INDEX IF NOT EXISTS idx_app_feedback_user_created ON app_feedback(user_id, created_at DESC);

-- IOO 3D neural graph scale/domain/unlock axes (2026-04-30)
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS node_scale TEXT DEFAULT 'meso';
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS macro_depth INTEGER DEFAULT 50;
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS primary_macro_domain TEXT;
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS contributes_to_domains TEXT[] DEFAULT '{}';
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS unlocks_domains TEXT[] DEFAULT '{}';
ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS dimensional_axes JSONB DEFAULT '{}';
CREATE INDEX IF NOT EXISTS idx_ioo_nodes_scale_depth ON ioo_nodes(node_scale, macro_depth);
CREATE INDEX IF NOT EXISTS idx_ioo_nodes_primary_macro_domain ON ioo_nodes(primary_macro_domain);
