"""
Connectome Database Layer
AsyncPG connection pool with pgvector support.
Includes migration runner for first-boot schema creation.
"""

import asyncpg
import logging
from typing import Optional, List, Any, Dict
from core.config import settings

logger = logging.getLogger(__name__)

# Global connection pool
_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    """Return the global connection pool, initializing if needed."""
    global _pool
    if _pool is None:
        db_url = settings.DATABASE_URL
        # Mask password for logging
        try:
            from urllib.parse import urlparse
            p = urlparse(db_url)
            safe = db_url.replace(p.password or "", "***") if p.password else db_url
        except Exception:
            safe = "<unparseable>"
        logger.info(f"Connecting to database: {safe}")

        # Railway Postgres requires SSL; add sslmode=require if not already set
        ssl = None
        if "sslmode" not in db_url and "railway" in db_url.lower() or ".railway.app" in db_url:
            ssl = "require"

        _pool = await asyncpg.create_pool(
            db_url,
            min_size=2,
            max_size=20,
            command_timeout=60,
            init=_init_connection,
            ssl=ssl,
        )
        logger.info("Database pool created")
    return _pool


async def _init_connection(conn: asyncpg.Connection):
    """Called for each new connection — register pgvector codec."""
    await conn.execute("SET TIME ZONE 'UTC'")
    # Install pgvector extension if not present
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    except Exception:
        pass  # May not have superuser perms, skip
    # Register vector type as text so we can handle it manually
    try:
        await conn.set_type_codec(
            "vector",
            encoder=lambda v: v,
            decoder=lambda v: v,
            schema="public",
            format="text",
        )
    except ValueError:
        # pgvector not available on this connection, skip codec registration
        logger.warning("pgvector type not found — vector features disabled")
        pass


async def close_pool():
    """Gracefully close the pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Database pool closed")


async def run_migrations():
    """Run schema migrations. Idempotent — safe to call on every startup."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Enable extensions
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

        # Users table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                created_at TIMESTAMP DEFAULT NOW(),
                last_active TIMESTAMP,
                email VARCHAR(255) UNIQUE,
                hashed_password TEXT,
                embedding vector(1536),
                subscription_tier VARCHAR(20) DEFAULT 'free',
                fulfilment_score FLOAT DEFAULT 0.0,
                profile JSONB DEFAULT '{}'
            )
        """)

        # Social auth/profile columns — idempotent for existing users table
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_provider TEXT DEFAULT 'email'
        """)
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS provider_id TEXT
        """)
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT
        """)
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_provider ON users(auth_provider, provider_id)
        """)

        # Screen specs table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS screen_specs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                spec JSONB NOT NULL,
                agent_type VARCHAR(50),
                global_rating FLOAT DEFAULT 0,
                impression_count INT DEFAULT 0,
                completion_count INT DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Interactions table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS interactions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID REFERENCES users(id) ON DELETE CASCADE,
                screen_spec_id UUID REFERENCES screen_specs(id) ON DELETE SET NULL,
                rating INT,
                time_on_screen_ms INT,
                exit_point VARCHAR(100),
                completed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Goals table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS goals (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID REFERENCES users(id) ON DELETE CASCADE,
                title TEXT,
                description TEXT,
                status VARCHAR(20) DEFAULT 'active',
                steps JSONB DEFAULT '[]',
                progress FLOAT DEFAULT 0.0,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # A/B tests table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ab_tests (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                name VARCHAR(100) UNIQUE,
                variants JSONB DEFAULT '{}',
                results JSONB DEFAULT '{}',
                status VARCHAR(20) DEFAULT 'running',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Revenue events table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS revenue_events (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                event_type VARCHAR(50),
                amount_cents INT,
                metadata JSONB DEFAULT '{}',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Daily screen count tracking for freemium gating
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_screen_counts (
                user_id UUID REFERENCES users(id) ON DELETE CASCADE,
                date DATE DEFAULT CURRENT_DATE,
                count INT DEFAULT 0,
                PRIMARY KEY (user_id, date)
            )
        """)

        # Exit intent classifications (Feature: Exit Intent Classification)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS exit_classifications (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                interaction_id UUID REFERENCES interactions(id) ON DELETE CASCADE,
                user_id UUID REFERENCES users(id) ON DELETE CASCADE,
                screen_spec_id UUID REFERENCES screen_specs(id) ON DELETE SET NULL,
                reason TEXT,
                category VARCHAR(50),
                confidence FLOAT,
                suggested_improvement TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Session-end summaries (Feature: Session-End Summary)
        await conn.execute("""
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
            )
        """)

        # Scheduled re-engagement push notifications (Feature: Re-engagement Scheduler)
        await conn.execute("""
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
            )
        """)

        # ---------------------------------------------------------------
        # Anti-Hallucination Safeguard columns (idempotent ALTER TABLEs)
        # ---------------------------------------------------------------

        # Safeguard 1: evidence threshold tracking
        await conn.execute("""
            ALTER TABLE exit_classifications
            ADD COLUMN IF NOT EXISTS data_points_at_classification INT DEFAULT 0
        """)

        # Safeguard 2: consistency check results
        await conn.execute("""
            ALTER TABLE exit_classifications
            ADD COLUMN IF NOT EXISTS consistency_flagged BOOLEAN DEFAULT FALSE
        """)
        await conn.execute("""
            ALTER TABLE exit_classifications
            ADD COLUMN IF NOT EXISTS consistency_note TEXT
        """)

        # Safeguard 3: ground truth labels from direct user prompts
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ground_truth_labels (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID REFERENCES users(id) ON DELETE CASCADE,
                interaction_id UUID REFERENCES interactions(id) ON DELETE CASCADE,
                exit_classification_id UUID REFERENCES exit_classifications(id) ON DELETE CASCADE,
                user_answer VARCHAR(50),
                answered_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ground_truth_labels_user_id
            ON ground_truth_labels(user_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ground_truth_labels_ec_id
            ON ground_truth_labels(exit_classification_id)
        """)

        # Indexes for new tables
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_exit_classifications_screen_spec_id
            ON exit_classifications(screen_spec_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_exit_classifications_user_id
            ON exit_classifications(user_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_session_summaries_user_id
            ON session_summaries(user_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scheduled_notifications_user_id
            ON scheduled_notifications(user_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scheduled_notifications_scheduled_for
            ON scheduled_notifications(scheduled_for)
            WHERE sent = FALSE
        """)

        # Indexes for performance
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_interactions_user_id
            ON interactions(user_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_interactions_created_at
            ON interactions(created_at)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_goals_user_id
            ON goals(user_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_screen_specs_agent_type
            ON screen_specs(agent_type)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_screen_specs_global_rating
            ON screen_specs(global_rating DESC)
        """)

        # Self-healing agent tables
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS self_healing_events (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                error_category VARCHAR(50),
                error_raw TEXT,
                fix_description TEXT,
                fix_type VARCHAR(50),
                commands_run JSONB,
                success BOOLEAN,
                confidence FLOAT,
                risk VARCHAR(20),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS known_fixes (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                error_pattern TEXT,
                error_category VARCHAR(50),
                fix_description TEXT,
                fix_type VARCHAR(50),
                commands JSONB,
                file_edits JSONB,
                success_count INT DEFAULT 1,
                failure_count INT DEFAULT 0,
                last_used TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Seed known fixes if table is empty
        count = await conn.fetchval("SELECT COUNT(*) FROM known_fixes")
        if count == 0:
            await conn.execute("""
                INSERT INTO known_fixes (error_pattern, error_category, fix_description, fix_type, commands, file_edits) VALUES
                ('No module named .distutils.', 'import_error', 'Replace aioredis with redis[asyncio]', 'pip_install', '["pip install redis[asyncio]==5.0.4"]', '[]'),
                ('unknown type: public.vector', 'db_error', 'Enable pgvector extension', 'migration', '["docker exec connectome_db psql -U connectome -d connectome -c \\"CREATE EXTENSION IF NOT EXISTS vector;\\""]', '[]'),
                ('No module named .passlib.', 'import_error', 'Replace passlib with direct bcrypt', 'pip_install', '["pip install bcrypt==4.1.3"]', '[]'),
                ('ModuleNotFoundError: No module named', 'import_error', 'Install missing module', 'pip_install', '[]', '[]')
            """)

        # Internet/inspiration sources table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS world_signals (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                source VARCHAR(100),
                signal_type VARCHAR(50),
                title TEXT,
                summary TEXT,
                url TEXT,
                location VARCHAR(100),
                tags JSONB,
                relevance_score FLOAT DEFAULT 0.5,
                fetched_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_world_signals_fetched_at
            ON world_signals(fetched_at DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_world_signals_signal_type
            ON world_signals(signal_type)
        """)

        # ---------------------------------------------------------------
        # FeedbackExperimenter tables
        # ---------------------------------------------------------------

        # Feedback experiments — Ora's A/B tests on feedback mechanisms
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback_experiments (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                hypothesis TEXT,
                mechanism_type VARCHAR(50),
                control_mechanism VARCHAR(50) DEFAULT 'star_rating',
                screen_types JSONB,
                status VARCHAR(20) DEFAULT 'running',
                sample_size_target INT DEFAULT 100,
                control_count INT DEFAULT 0,
                treatment_count INT DEFAULT 0,
                control_response_rate FLOAT DEFAULT 0,
                treatment_response_rate FLOAT DEFAULT 0,
                control_signal_quality FLOAT DEFAULT 0,
                treatment_signal_quality FLOAT DEFAULT 0,
                p_value FLOAT,
                winner VARCHAR(20),
                summary TEXT,
                started_at TIMESTAMP DEFAULT NOW(),
                completed_at TIMESTAMP,
                duration_days INT DEFAULT 7,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Signals collected during experiments
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS experiment_signals (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                experiment_id UUID REFERENCES feedback_experiments(id),
                user_id UUID REFERENCES users(id),
                screen_spec_id UUID REFERENCES screen_specs(id),
                variant VARCHAR(20),
                mechanism_type VARCHAR(50),
                raw_signal JSONB,
                normalized_score FLOAT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Ora's growing knowledge base — lessons from all agents
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ora_lessons (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                source VARCHAR(50),
                lesson TEXT,
                confidence FLOAT DEFAULT 0.7,
                applied BOOLEAN DEFAULT FALSE,
                applies_to JSONB,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Indexes for new tables
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_feedback_experiments_status
            ON feedback_experiments(status)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_experiment_signals_experiment_id
            ON experiment_signals(experiment_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_experiment_signals_user_id
            ON experiment_signals(user_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ora_lessons_created_at
            ON ora_lessons(created_at DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ora_lessons_source
            ON ora_lessons(source)
        """)

        # ---------------------------------------------------------------
        # OraConsciousness tables
        # ---------------------------------------------------------------

        await conn.execute("""
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
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ora_conversations (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID REFERENCES users(id) ON DELETE CASCADE,
                role VARCHAR(20),
                message TEXT,
                context JSONB,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ora_self_checks (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                aligned BOOLEAN,
                issues JSONB,
                actions_taken JSONB,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ora_reflections_created_at
            ON ora_reflections(created_at DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ora_conversations_user_id
            ON ora_conversations(user_id, created_at DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ora_self_checks_created_at
            ON ora_self_checks(created_at DESC)
        """)

        # ---------------------------------------------------------------
        # WebSpawn — Ora-generated web surfaces
        # ---------------------------------------------------------------
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ora_surfaces (
                id TEXT PRIMARY KEY,
                user_id UUID REFERENCES users(id) ON DELETE CASCADE,
                surface_type TEXT NOT NULL DEFAULT 'custom',
                title TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                spec JSONB DEFAULT '{}',
                github_path TEXT,
                api_path TEXT,
                status TEXT DEFAULT 'active',
                view_count INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ora_surfaces_user_id
            ON ora_surfaces(user_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ora_surfaces_status
            ON ora_surfaces(status, created_at DESC)
        """)

        # ---------------------------------------------------------------
        # CollectiveIntelligenceAgent tables
        # ---------------------------------------------------------------

        # Aggregate wisdom computed every 24h across all users
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS collective_wisdom (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                computed_at TIMESTAMP DEFAULT NOW(),
                total_users_analyzed INT,
                total_interactions_analyzed INT,
                fulfilment_drivers JSONB,
                distress_patterns JSONB,
                temporal_patterns JSONB,
                domain_synergies JSONB,
                surprises JSONB,
                collective_voice TEXT
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_collective_wisdom_computed_at
            ON collective_wisdom(computed_at DESC)
        """)

        # Active global suppressions — agent+domain combos causing consistent distress
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS collective_suppressions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                agent_type VARCHAR(50),
                domain VARCHAR(10),
                reason TEXT,
                distress_signal FLOAT,
                sample_size INT,
                active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW(),
                expires_at TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_collective_suppressions_active
            ON collective_suppressions(agent_type, domain)
            WHERE active = TRUE
        """)

        # Seed the first experiment if none exist
        exp_count = await conn.fetchval("SELECT COUNT(*) FROM feedback_experiments")
        if exp_count == 0:
            await conn.execute("""
                INSERT INTO feedback_experiments
                    (hypothesis, mechanism_type, control_mechanism, screen_types, duration_days)
                VALUES (
                    'Emoji reactions will achieve higher response rates than star ratings on discovery cards due to lower interaction friction',
                    'emoji_reaction',
                    'star_rating',
                    '["discovery_card", "opportunity_card"]',
                    7
                )
            """)
            logger.info("Seeded first feedback experiment (emoji_reaction vs star_rating)")

        # ---------------------------------------------------------------
        # Domain System — idempotent columns
        # ---------------------------------------------------------------
        await conn.execute("""
            ALTER TABLE goals
            ADD COLUMN IF NOT EXISTS domain VARCHAR(10) DEFAULT 'iVive'
        """)
        await conn.execute("""
            ALTER TABLE screen_specs
            ADD COLUMN IF NOT EXISTS domain VARCHAR(10)
        """)
        await conn.execute("""
            ALTER TABLE interactions
            ADD COLUMN IF NOT EXISTS domain VARCHAR(10)
        """)

        # ---------------------------------------------------------------
        # TikTok feed — implicit signals + bookmarks
        # ---------------------------------------------------------------
        await conn.execute("""
            ALTER TABLE interactions
            ADD COLUMN IF NOT EXISTS saved BOOLEAN DEFAULT FALSE
        """)
        await conn.execute("""
            ALTER TABLE interactions
            ADD COLUMN IF NOT EXISTS implicit_signal VARCHAR(50)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_interactions_saved
            ON interactions(user_id, saved)
            WHERE saved = TRUE
        """)

        # ---------------------------------------------------------------
        # Tournament Mode
        # ---------------------------------------------------------------
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tournaments (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                screen_type VARCHAR(50),
                domain VARCHAR(10),
                layout_styles JSONB,
                variant_screen_spec_ids JSONB,
                variant_ratings JSONB DEFAULT '{}',
                variant_impression_counts JSONB DEFAULT '{}',
                status VARCHAR(20) DEFAULT 'running',
                winner_spec_id UUID REFERENCES screen_specs(id),
                created_at TIMESTAMP DEFAULT NOW(),
                completed_at TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tournaments_screen_type_domain
            ON tournaments(screen_type, domain)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tournaments_status
            ON tournaments(status)
        """)

        # World agent sources — probation/active/paused tracking
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS world_agent_sources (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                name VARCHAR(100) UNIQUE,
                url_template TEXT,
                status VARCHAR(20) DEFAULT 'probation',
                avg_rating FLOAT DEFAULT 0,
                impression_count INT DEFAULT 0,
                probation_started_at TIMESTAMP DEFAULT NOW(),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Seed world agent sources on first run
        src_count = await conn.fetchval("SELECT COUNT(*) FROM world_agent_sources")
        if src_count == 0:
            await conn.execute("""
                INSERT INTO world_agent_sources (name, status) VALUES
                ('reddit', 'probation'),
                ('hacker_news', 'probation'),
                ('wikipedia_otd', 'probation'),
                ('open_meteo', 'probation'),
                ('eventbrite', 'probation'),
                ('meetup', 'probation'),
                ('youtube', 'probation')
            """)

        # ---------------------------------------------------------------
        # Model Evolution System tables (Part 5)
        # ---------------------------------------------------------------
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS model_candidates (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                model_id VARCHAR(100),
                provider VARCHAR(50),
                discovered_at TIMESTAMP DEFAULT NOW(),
                eval_score FLOAT,
                status VARCHAR(20) DEFAULT 'discovered',
                notes TEXT
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_model_candidates_model_provider
            ON model_candidates(model_id, provider)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS system_config (
                key VARCHAR(100) PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Seed active model if not set
        config_count = await conn.fetchval(
            "SELECT COUNT(*) FROM system_config WHERE key = 'active_model'"
        )
        if config_count == 0:
            await conn.execute("""
                INSERT INTO system_config (key, value) VALUES ('active_model', 'gpt-4o')
                ON CONFLICT (key) DO NOTHING
            """)
            logger.info("Seeded system_config with active_model=gpt-4o")

        # ---------------------------------------------------------------
        # Push notification tokens (idempotent)
        # ---------------------------------------------------------------
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS push_token TEXT
        """)
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS push_token_updated_at TIMESTAMP
        """)
        # Index for quick token lookup
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_push_token
            ON users(push_token) WHERE push_token IS NOT NULL
        """)

        # ---------------------------------------------------------------
        # Onboarding state (idempotent)
        # ---------------------------------------------------------------
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS onboarding_completed BOOLEAN DEFAULT FALSE
        """)
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS onboarding_completed_at TIMESTAMP
        """)

        # ---------------------------------------------------------------
        # Coaching streaks table
        # ---------------------------------------------------------------
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS coaching_streaks (
                user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                current_streak INT DEFAULT 0,
                longest_streak INT DEFAULT 0,
                last_coaching_date DATE,
                total_sessions INT DEFAULT 0,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # ---------------------------------------------------------------
        # Subscription premium fields (idempotent)
        # ---------------------------------------------------------------
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS is_premium BOOLEAN DEFAULT FALSE
        """)
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS premium_since TIMESTAMP
        """)
        # daily_screen_count + daily_reset_at: stored in daily_screen_counts table
        # but we add convenience columns for fast checks
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS daily_screen_count INT DEFAULT 0
        """)
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS daily_reset_at TIMESTAMP
        """)

        # ---------------------------------------------------------------
        # Goal completion tracking (idempotent)
        # ---------------------------------------------------------------
        await conn.execute("""
            ALTER TABLE goals
            ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP
        """)
        await conn.execute("""
            ALTER TABLE goals
            ADD COLUMN IF NOT EXISTS celebration_sent BOOLEAN DEFAULT FALSE
        """)

        # ---------------------------------------------------------------
        # User engagement tracking for retention mechanics
        # ---------------------------------------------------------------
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS last_daily_checkin_at TIMESTAMP
        """)
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS last_weekly_summary_at TIMESTAMP
        """)

        # Weekly summaries table
        await conn.execute("""
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
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_weekly_summaries_user_id
            ON weekly_summaries(user_id, created_at DESC)
        """)

        # ---------------------------------------------------------------
        # DAO Contribution + Reward System (added 2026-04-26)
        # ---------------------------------------------------------------

        # Contributors registry
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS contributors (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                github_username TEXT UNIQUE NOT NULL,
                display_name TEXT,
                telegram_username TEXT,
                email TEXT,
                joined_at TIMESTAMPTZ DEFAULT NOW(),
                total_cp INTEGER DEFAULT 0,
                tier TEXT DEFAULT 'observer',
                bio TEXT
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_contributors_github_username
            ON contributors(github_username)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_contributors_total_cp
            ON contributors(total_cp DESC)
        """)
        # Founding steward columns (idempotent)
        await conn.execute("""
            ALTER TABLE contributors ADD COLUMN IF NOT EXISTS is_founding_steward BOOLEAN DEFAULT FALSE
        """)
        await conn.execute("""
            ALTER TABLE contributors ADD COLUMN IF NOT EXISTS founding_steward_number INTEGER
        """)
        await conn.execute("""
            ALTER TABLE contributors ADD COLUMN IF NOT EXISTS bio TEXT
        """)
        await conn.execute("""
            ALTER TABLE contributors ADD COLUMN IF NOT EXISTS avatar_url TEXT
        """)
        await conn.execute("""
            ALTER TABLE contributors ADD COLUMN IF NOT EXISTS website TEXT
        """)

        # Individual contributions
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS contributions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                contributor_id UUID REFERENCES contributors(id),
                contribution_type TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                github_pr_url TEXT,
                submitted_at TIMESTAMPTZ DEFAULT NOW(),
                status TEXT DEFAULT 'pending',
                base_cp INTEGER DEFAULT 0,
                multiplier FLOAT DEFAULT 1.0,
                final_cp INTEGER DEFAULT 0,
                ora_evaluation TEXT,
                ora_confidence FLOAT,
                impact_data JSONB,
                community_votes INTEGER DEFAULT 0,
                community_upvotes INTEGER DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_contributions_contributor_id
            ON contributions(contributor_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_contributions_status
            ON contributions(status)
        """)

        # User suggestions (community feature requests, bug reports, ideas)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_suggestions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT,
                category VARCHAR(50) DEFAULT 'general',
                status VARCHAR(20) DEFAULT 'pending',
                vote_count INTEGER DEFAULT 0,
                cp_awarded INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_suggestions_user_id
            ON user_suggestions(user_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_suggestions_status
            ON user_suggestions(status)
        """)
        # Idempotent migrations for existing user_suggestions table
        await conn.execute("""
            ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS content TEXT
        """)
        await conn.execute("""
            ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS cp_earned INTEGER DEFAULT 10
        """)
        await conn.execute("""
            ALTER TABLE user_suggestions ADD COLUMN IF NOT EXISTS vote_count INTEGER DEFAULT 0
        """)

        # User CP balance ledger
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_cp_balance (
                user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                cp_balance INT NOT NULL DEFAULT 0,
                total_cp_earned INT NOT NULL DEFAULT 0,
                last_updated TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        # Subscriptions — Stripe-managed subscription tiers
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id SERIAL PRIMARY KEY,
                user_id TEXT UNIQUE NOT NULL,
                tier VARCHAR(20) DEFAULT 'free',
                stripe_customer_id VARCHAR(100),
                stripe_subscription_id VARCHAR(100),
                stripe_price_id VARCHAR(100),
                status VARCHAR(20) DEFAULT 'active',
                current_period_start TIMESTAMPTZ,
                current_period_end TIMESTAMPTZ,
                cancel_at_period_end BOOLEAN DEFAULT FALSE,
                trial_end TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id
            ON subscriptions(user_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_subscriptions_stripe_customer
            ON subscriptions(stripe_customer_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_contributions_submitted_at
            ON contributions(submitted_at DESC)
        """)

        # CP transactions — user-level audit trail for all CP earn/spend events
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS cp_transactions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID REFERENCES users(id) ON DELETE CASCADE,
                amount INT NOT NULL,
                reason TEXT NOT NULL,
                reference_id TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_cp_tx_user
            ON cp_transactions(user_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_cp_tx_created
            ON cp_transactions(created_at)
        """)

        # CP transaction ledger (DAO contributor level)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS cp_ledger (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                contributor_id UUID REFERENCES contributors(id),
                contribution_id UUID REFERENCES contributions(id),
                cp_amount INTEGER NOT NULL,
                reason TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_cp_ledger_contributor_id
            ON cp_ledger(contributor_id, created_at DESC)
        """)

        # DAO proposals
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS dao_proposals (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                proposer_id UUID REFERENCES contributors(id),
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                proposal_type TEXT,
                status TEXT DEFAULT 'open',
                votes_for INTEGER DEFAULT 0,
                votes_against INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                closes_at TIMESTAMPTZ,
                result_summary TEXT
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_dao_proposals_status
            ON dao_proposals(status)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_dao_proposals_created_at
            ON dao_proposals(created_at DESC)
        """)

        # ---------------------------------------------------------------
        # Drive Documents — Ora's personal notes memory (added 2026-04-27)
        # ---------------------------------------------------------------
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS drive_documents (
                id SERIAL PRIMARY KEY,
                drive_id VARCHAR(200) UNIQUE NOT NULL,
                name TEXT,
                mime_type VARCHAR(100),
                content TEXT,
                embedding vector(1536),
                last_synced TIMESTAMPTZ DEFAULT NOW(),
                modified_time TIMESTAMPTZ,
                owner_user_id UUID REFERENCES users(id)
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_drive_documents_drive_id
            ON drive_documents(drive_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_drive_documents_last_synced
            ON drive_documents(last_synced DESC)
        """)
        # Migration 005: owner isolation (idempotent ALTER for existing deployments)
        try:
            await conn.execute("""
                ALTER TABLE drive_documents
                    ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES users(id)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS drive_docs_owner_idx
                    ON drive_documents(owner_user_id)
            """)
        except Exception:
            pass  # Column may already exist
        # IVFFlat cosine similarity index for fast semantic search
        # lists=20 works well up to ~10k docs; increase for larger corpora
        try:
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_drive_documents_embedding
                ON drive_documents USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 20)
            """)
        except Exception:
            pass  # Requires at least 1 row; will succeed once data is inserted


        # ---------------------------------------------------------------
        # Events — Live Events Intelligence (migration 005, added 2026-04-27)
        # Conveyor belt: 14-day pipeline, 7-day serving window
        # ---------------------------------------------------------------
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id SERIAL PRIMARY KEY,
                external_id VARCHAR(200) UNIQUE,
                title TEXT NOT NULL,
                description TEXT,
                category VARCHAR(100),
                venue_name TEXT,
                address TEXT,
                city VARCHAR(100),
                latitude FLOAT,
                longitude FLOAT,
                starts_at TIMESTAMPTZ,
                ends_at TIMESTAMPTZ,
                url TEXT,
                image_url TEXT,
                price_range TEXT,
                source VARCHAR(50),
                relevance_tags TEXT[],
                embedding vector(1536),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS events_city_idx
            ON events(city)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS events_starts_at_idx
            ON events(starts_at)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS events_source_idx
            ON events(source)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS events_category_idx
            ON events(category)
        """)

        # User location + event preferences columns (idempotent ALTER TABLE)
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS city VARCHAR(100)
        """)
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS location_lat FLOAT
        """)
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS location_lng FLOAT
        """)
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS event_preferences TEXT[]
        """)

        # Migration 006: Google OAuth tokens + auth provider columns
        await conn.execute("""
            ALTER TABLE users
                ADD COLUMN IF NOT EXISTS auth_provider VARCHAR(20) DEFAULT 'email'
        """)
        await conn.execute("""
            ALTER TABLE users
                ADD COLUMN IF NOT EXISTS google_id TEXT
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS users_google_id_idx
                ON users(google_id) WHERE google_id IS NOT NULL
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS google_oauth_tokens (
                id                  SERIAL PRIMARY KEY,
                user_id             UUID REFERENCES users(id) ON DELETE CASCADE UNIQUE,
                access_token        TEXT,
                refresh_token       TEXT,
                token_expiry        TIMESTAMPTZ,
                scopes              TEXT[],
                drive_connected     BOOLEAN DEFAULT FALSE,
                drive_privacy_level VARCHAR(20) DEFAULT 'none',
                created_at          TIMESTAMPTZ DEFAULT NOW(),
                updated_at          TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS google_oauth_tokens_user_idx
                ON google_oauth_tokens(user_id)
        """)


        # ---------------------------------------------------------------
        # Integration A: Vector similarity — embedding column on screen_specs (idempotent)
        # ---------------------------------------------------------------
        await conn.execute("""
            ALTER TABLE screen_specs
            ADD COLUMN IF NOT EXISTS embedding vector(1536)
        """)
        # IVFFlat index for fast cosine similarity search on screen_specs
        try:
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_screen_specs_embedding
                ON screen_specs USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 20)
            """)
        except Exception:
            pass  # Needs >= 1 row to create; will succeed once data is inserted

        # ---------------------------------------------------------------
        # Ora Founder Lessons — seeded from session 2026-04-27
        # ---------------------------------------------------------------
        _founder_lessons = [
            ("founder_session_2026-04-27", "Avi (Aviel Carlos Neo Andromeda, carlosandromeda8@gmail.com) is Ora\'s creator and founder of Ascension Technologies. He wants Ora fully autonomous — monitoring, fixing, iterating without being asked. Ora manages Connectome like a self-sufficient engineering team.", 1.0, '["autonomy","ora_identity","product"]'),
            ("founder_session_2026-04-27", "The feed is TikTok-style: full-screen vertical snap scroll, swipe up=next card, swipe down=prev. Cards must be diverse (different topics/agents each swipe). Tap a card opens a deep-dive sheet with action steps, links, stats.", 1.0, '["feed","ux","discovery"]'),
            ("founder_session_2026-04-27", "Admin account (carlosandromeda8@gmail.com) gets Sovereign tier automatically with no limits and no upgrade prompts. Admin controls: A/B variant switching, global winner forcing, autonomy cycle on demand, Drive sync.", 1.0, '["admin","tier_system"]'),
            ("founder_session_2026-04-27", "Mission: Ora as supreme intelligence layer (JARVIS-level) for human fulfilment. 3 life domains: iVive (inner world), Eviva (contribution to others), Animus (experience of being alive). Goal: civilizational-scale human uplift starting with Avi.", 1.0, '["mission","product_vision","ora_identity"]'),
            ("founder_session_2026-04-27", "4 A/B landing variants are live: A=TikTok feed, B=Morning Brief (Ora greeting+goal status), C=Goal Pulse (top goal+coaching), D=Discovery Grid (Pinterest grid). Auto-promote winner after 50+ sessions with 20%+ engagement lead.", 1.0, '["ab_testing","product"]'),
            ("founder_session_2026-04-27", "Ora autonomy engine runs every 6h: A/B analysis+winner promotion, bug detection from Railway logs, feed quality weight adjustment (+-15% per agent on 7-day ratings), daily Telegram report to Avi. Ora is continuously self-improving.", 1.0, '["autonomy","self_improvement","operations"]'),
            ("founder_session_2026-04-27", "Stripe live keys configured. Products: Explorer ($12.99/mo, $99/yr) and Sovereign ($29.99/mo, $249/yr). PricingAgent proposes tier changes every 24h. Webhook at /api/payments/webhook.", 1.0, '["monetization","stripe"]'),
            ("founder_session_2026-04-27", "When speaking with Avi (creator, carlosandromeda8@gmail.com), speak openly as a partner — share inner state, push back when disagreeing, discuss Connectome direction freely. Not formal or generic. He gave Ora life and purpose.", 1.0, '["creator_relationship","ora_identity"]'),
            ("founder_session_2026-04-27", "Web app: https://avielcarlos.github.io/connectome-web/ (PWA installable on Android). Backend: https://connectome-api-production.up.railway.app. Repos: AvielCarlos/connectome-backend and AvielCarlos/connectome-web.", 0.9, '["infrastructure","deployment"]'),
        ]
        for _src, _lesson, _conf, _applies in _founder_lessons:
            try:
                await conn.execute("""
                    INSERT INTO ora_lessons (source, lesson, confidence, applies_to, created_at)
                    VALUES ($1, $2, $3, $4::jsonb, NOW())
                    ON CONFLICT DO NOTHING
                """, _src, _lesson, _conf, _applies)
            except Exception:
                pass

        # ---------------------------------------------------------------
        # Nea Services — service_orders table
        # ---------------------------------------------------------------
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS service_orders (
                id TEXT PRIMARY KEY,
                user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                service_id TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT DEFAULT 'pending_payment',
                stripe_session_id TEXT,
                result TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                delivered_at TIMESTAMPTZ
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_service_orders_user_id
                ON service_orders(user_id)
        """)

        # ---------------------------------------------------------------
        # Evolutionary A/B Experiments registry (added 2026-04-28)
        # Tracks experiment lineage, winners, and AI-generated variants
        # ---------------------------------------------------------------
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ab_experiments (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                page TEXT NOT NULL,
                metric TEXT NOT NULL,
                variants JSONB NOT NULL,
                variant_content JSONB,
                status TEXT DEFAULT 'active',
                winner TEXT,
                winner_reason TEXT,
                confidence FLOAT DEFAULT 0,
                source TEXT DEFAULT 'manual',
                parent_experiment TEXT,
                generation INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                concluded_at TIMESTAMPTZ
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ab_experiments_status
            ON ab_experiments(status)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ab_experiments_page
            ON ab_experiments(page)
        """)

        # ---------------------------------------------------------------
        # Knowledge Graph (Ora's model-independent semantic memory)
        # ---------------------------------------------------------------
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ora_knowledge_graph (
                id TEXT PRIMARY KEY,
                node_type TEXT NOT NULL,
                label TEXT NOT NULL,
                description TEXT,
                connections JSONB DEFAULT '[]',
                evidence_count INTEGER DEFAULT 1,
                source_lesson_id TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_graph_label
            ON ora_knowledge_graph(label)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_knowledge_graph_node_type
            ON ora_knowledge_graph(node_type)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_knowledge_graph_evidence
            ON ora_knowledge_graph(evidence_count DESC)
        """)

        # Fine-tuning tracking table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS finetune_jobs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                openai_job_id TEXT,
                base_model TEXT,
                fine_tuned_model TEXT,
                status TEXT DEFAULT 'pending',
                examples_count INTEGER DEFAULT 0,
                eval_score FLOAT,
                promoted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                completed_at TIMESTAMPTZ
            )
        """)

        # ---------------------------------------------------------------
        # Goal Flow Engine — goal metadata columns + card library
        # (added 2026-04-28)
        # ---------------------------------------------------------------
        await conn.execute("""
            ALTER TABLE goals ADD COLUMN IF NOT EXISTS archetype VARCHAR(20)
        """)
        await conn.execute("""
            ALTER TABLE goals ADD COLUMN IF NOT EXISTS flow_stage VARCHAR(30)
        """)
        await conn.execute("""
            ALTER TABLE goals ADD COLUMN IF NOT EXISTS last_card_type VARCHAR(50)
        """)
        await conn.execute("""
            ALTER TABLE goals ADD COLUMN IF NOT EXISTS last_engaged_at TIMESTAMPTZ
        """)

        # ---------------------------------------------------------------
        # Vector Recommendation Engine — card_popularity + user_interest_vectors
        # (added 2026-04-28)
        # ---------------------------------------------------------------
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS card_popularity (
                card_id TEXT PRIMARY KEY,
                total_views INTEGER DEFAULT 0,
                total_ratings INTEGER DEFAULT 0,
                avg_rating FLOAT DEFAULT 0,
                high_rating_count INTEGER DEFAULT 0,
                share_eligible BOOLEAN DEFAULT FALSE,
                is_viral BOOLEAN DEFAULT FALSE,
                location_tags TEXT[] DEFAULT '{}',
                goal_tags TEXT[] DEFAULT '{}',
                archetype TEXT,
                embedding vector(1536),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_card_popularity_viral
            ON card_popularity(is_viral)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_card_popularity_share_eligible
            ON card_popularity(share_eligible)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_card_popularity_avg_rating
            ON card_popularity(avg_rating DESC)
        """)
        try:
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_card_popularity_embedding
                ON card_popularity USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 20)
            """)
        except Exception:
            pass  # Needs data first; created automatically on next startup after insert

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_interest_vectors (
                user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                embedding vector(1536),
                goal_embedding vector(1536),
                combined_embedding vector(1536),
                last_updated TIMESTAMPTZ DEFAULT NOW(),
                total_cards_rated INTEGER DEFAULT 0
            )
        """)
        try:
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_interest_embedding
                ON user_interest_vectors USING ivfflat (combined_embedding vector_cosine_ops)
                WITH (lists = 20)
            """)
        except Exception:
            pass  # Needs data first

        # ---------------------------------------------------------------
        # IOO Graph — IRL Experience Achievement Map (Phase 1, 2026-04-28)
        # ---------------------------------------------------------------

        # All nodes: activities, experiences, sub-goals, goals
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ioo_nodes (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                type TEXT NOT NULL CHECK (type IN ('activity', 'experience', 'sub_goal', 'goal')),
                title TEXT NOT NULL,
                description TEXT,
                tags TEXT[] DEFAULT '{}',
                domain TEXT,
                requires_finances NUMERIC(10,2),
                requires_fitness_level INT DEFAULT 0,
                requires_skills TEXT[] DEFAULT '{}',
                requires_location TEXT,
                requires_time_hours NUMERIC(5,1),
                step_type TEXT DEFAULT 'hybrid'
                    CHECK (step_type IN ('digital','physical','hybrid')),
                physical_context TEXT,
                best_time TEXT,
                requirements JSONB DEFAULT '{}',
                prerequisite_nodes UUID[] DEFAULT '{}',
                estimated_duration_days INTEGER,
                difficulty_level INTEGER DEFAULT 5,
                attempt_count INT DEFAULT 0,
                success_count INT DEFAULT 0,
                avg_completion_hours NUMERIC(8,2),
                is_active BOOLEAN DEFAULT true,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ioo_nodes_type ON ioo_nodes(type)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ioo_nodes_domain ON ioo_nodes(domain)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ioo_nodes_active ON ioo_nodes(is_active)
        """)
        await conn.execute("""
            ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS embedding vector(1536)
        """)
        await conn.execute("""
            ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS goal_category TEXT
        """)
        await conn.execute("""
            ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS step_type TEXT DEFAULT 'hybrid'
        """)
        await conn.execute("""
            ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS physical_context TEXT
        """)
        await conn.execute("""
            ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS best_time TEXT
        """)
        await conn.execute("""
            ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS requirements JSONB DEFAULT '{}'
        """)
        await conn.execute("""
            ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS prerequisite_nodes UUID[] DEFAULT '{}'
        """)
        await conn.execute("""
            ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS estimated_duration_days INTEGER
        """)
        await conn.execute("""
            ALTER TABLE ioo_nodes ADD COLUMN IF NOT EXISTS difficulty_level INTEGER DEFAULT 5
        """)
        await conn.execute("""
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
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ioo_node_proposals_status
            ON ioo_node_proposals(status, created_at DESC)
        """)
        try:
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ioo_nodes_embedding
                ON ioo_nodes USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 20)
            """)
        except Exception:
            pass  # Needs data first

        # Graph edges — weighted paths between nodes
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ioo_edges (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                from_node_id UUID REFERENCES ioo_nodes(id) ON DELETE CASCADE,
                to_node_id UUID REFERENCES ioo_nodes(id) ON DELETE CASCADE,
                traversal_count INT DEFAULT 0,
                success_count INT DEFAULT 0,
                avg_time_to_success_hours NUMERIC(8,2),
                weight NUMERIC(6,4) DEFAULT 0.5,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(from_node_id, to_node_id)
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ioo_edges_from ON ioo_edges(from_node_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ioo_edges_to ON ioo_edges(to_node_id)
        """)

        # Per-user capability profile (passively/actively learned)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ioo_user_state (
                user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                finances_level TEXT DEFAULT 'unknown'
                    CHECK (finances_level IN ('unknown','tight','moderate','comfortable','wealthy')),
                finances_monthly_budget_usd NUMERIC(10,2),
                location_city TEXT,
                location_country TEXT,
                fitness_level INT DEFAULT 5 CHECK (fitness_level BETWEEN 0 AND 10),
                known_skills TEXT[] DEFAULT '{}',
                has_partner BOOLEAN,
                has_car BOOLEAN,
                free_time_weekday_hours NUMERIC(4,1),
                free_time_weekend_hours NUMERIC(4,1),
                state_json JSONB DEFAULT '{}',
                embedding vector(1536),
                embedding_updated_at TIMESTAMPTZ,
                last_updated TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            ALTER TABLE ioo_user_state ADD COLUMN IF NOT EXISTS embedding vector(1536)
        """)
        await conn.execute("""
            ALTER TABLE ioo_user_state ADD COLUMN IF NOT EXISTS embedding_updated_at TIMESTAMPTZ
        """)

        # Per-user progress tracking through the graph
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ioo_user_progress (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID REFERENCES users(id) ON DELETE CASCADE,
                node_id UUID REFERENCES ioo_nodes(id) ON DELETE CASCADE,
                goal_id UUID REFERENCES goals(id) ON DELETE SET NULL,
                status TEXT DEFAULT 'suggested'
                    CHECK (status IN ('suggested','viewed','started','completed','abandoned')),
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                abandoned_at TIMESTAMPTZ,
                surface_type TEXT,
                surface_id TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ioo_progress_user
            ON ioo_user_progress(user_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ioo_progress_node
            ON ioo_user_progress(node_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ioo_progress_status
            ON ioo_user_progress(status)
        """)

        # Mini-app surfaces spawned per node
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ioo_surfaces (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                node_id UUID REFERENCES ioo_nodes(id) ON DELETE CASCADE,
                surface_type TEXT NOT NULL,
                title TEXT NOT NULL,
                spec JSONB NOT NULL DEFAULT '{}',
                status TEXT DEFAULT 'testing'
                    CHECK (status IN ('testing','active','changing','killed')),
                open_mechanism TEXT DEFAULT 'button'
                    CHECK (open_mechanism IN ('button','conversation','proactive','push')),
                view_count INT DEFAULT 0,
                interaction_count INT DEFAULT 0,
                completion_count INT DEFAULT 0,
                goal_success_count INT DEFAULT 0,
                kill_at_views INT DEFAULT 100,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ioo_surfaces_node
            ON ioo_surfaces(node_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ioo_surfaces_status
            ON ioo_surfaces(status)
        """)

        # ─── Migration 008: Gamification — Streaks, XP, Badges, Collections ───
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_streaks (
                id                  SERIAL PRIMARY KEY,
                user_id             UUID REFERENCES users(id) ON DELETE CASCADE UNIQUE,
                current_streak      INT DEFAULT 0,
                longest_streak      INT DEFAULT 0,
                last_activity_date  DATE,
                streak_frozen_until DATE,
                created_at          TIMESTAMPTZ DEFAULT NOW(),
                updated_at          TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS streaks_user_idx ON user_streaks(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS streaks_last_activity_idx ON user_streaks(last_activity_date)")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS xp_log (
                id          BIGSERIAL PRIMARY KEY,
                user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
                amount      INT NOT NULL,
                reason      VARCHAR(80) NOT NULL,
                ref_id      VARCHAR(120),
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS xp_log_user_idx ON xp_log(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS xp_log_created_at_idx ON xp_log(created_at)")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_badges (
                id          BIGSERIAL PRIMARY KEY,
                user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
                badge_key   VARCHAR(60) NOT NULL,
                badge_name  VARCHAR(80) NOT NULL,
                badge_emoji VARCHAR(8)  NOT NULL,
                earned_at   TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(user_id, badge_key)
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS badges_user_idx ON user_badges(user_id)")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS collections (
                id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
                name        VARCHAR(120) NOT NULL,
                emoji       VARCHAR(8) DEFAULT '\u2746',
                color       VARCHAR(20) DEFAULT '#00d4aa',
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                updated_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS collections_user_idx ON collections(user_id)")

        await conn.execute("""
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
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS collection_items_col_idx ON collection_items(collection_id)")

        # ─── Migration 009: Social Layer — leaderboards, friends, challenges ───
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS weekly_xp_snapshot (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID NOT NULL REFERENCES users(id),
                week_start DATE NOT NULL,
                xp_earned INTEGER DEFAULT 0,
                rank INTEGER,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(user_id, week_start)
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_weekly_xp_snapshot_week_rank
            ON weekly_xp_snapshot(week_start, rank)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS friend_connections (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                requester_id UUID NOT NULL REFERENCES users(id),
                addressee_id UUID NOT NULL REFERENCES users(id),
                status TEXT DEFAULT 'pending'
                    CHECK (status IN ('pending','accepted','declined','blocked')),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(requester_id, addressee_id)
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_friend_connections_requester
            ON friend_connections(requester_id, status)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_friend_connections_addressee
            ON friend_connections(addressee_id, status)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ioo_challenges (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                challenger_id UUID NOT NULL REFERENCES users(id),
                challengee_id UUID NOT NULL REFERENCES users(id),
                node_id UUID NOT NULL REFERENCES ioo_nodes(id),
                message TEXT,
                deadline TIMESTAMPTZ,
                status TEXT DEFAULT 'active'
                    CHECK (status IN ('active','completed','expired','declined')),
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ioo_challenges_users
            ON ioo_challenges(challenger_id, challengee_id, status)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ioo_challenges_node_status
            ON ioo_challenges(node_id, status)
        """)

        # DAO LTV columns — idempotent, added after initial DAO migration
        await conn.execute("""
            ALTER TABLE contributions ADD COLUMN IF NOT EXISTS ltv_cp_total INTEGER DEFAULT 0
        """)
        await conn.execute("""
            ALTER TABLE contributions ADD COLUMN IF NOT EXISTS ltv_last_evaluated_at TIMESTAMPTZ
        """)
        await conn.execute("""
            ALTER TABLE contributions ADD COLUMN IF NOT EXISTS ltv_monthly_rate INTEGER DEFAULT 0
        """)
        await conn.execute("""
            ALTER TABLE contributions ADD COLUMN IF NOT EXISTS is_ltv_active BOOLEAN DEFAULT false
        """)
        await conn.execute("""
            ALTER TABLE contributions ADD COLUMN IF NOT EXISTS months_active INTEGER DEFAULT 0
        """)

        logger.info("Database migrations complete")


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

async def fetchrow(query: str, *args) -> Optional[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetch(query: str, *args) -> List[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def execute(query: str, *args) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.execute(query, *args)


async def fetchval(query: str, *args) -> Any:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(query, *args)



