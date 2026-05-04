"""
Connectome Backend — FastAPI Application
Entry point. Mounts all routes and manages lifespan.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from core.config import settings
from core.database import run_migrations, close_pool
from core.redis_client import get_redis, close_redis
from aura.brain import init_brain, get_brain
from aura.agents.self_healing import SelfHealingAgent
from aura.agents.model_evolution import ModelEvolutionAgent
from api.middleware import timing_middleware
from api.routes import users, screens, feedback, goals, monetization, sessions, notifications, ground_truth, admin
try:
    from api.routes import payments as payments_routes
    _payments_available = True
except Exception as _payments_err:
    payments_routes = None
    _payments_available = False
    logging.warning(f"Payments module unavailable: {_payments_err}")
from api.routes import aura_chat
from api.routes import discovery as discovery_routes
from api.routes import ab_testing as ab_testing_routes
from api.routes import explore as explore_routes
from api.routes import journal as journal_routes
from api.routes import feature_lab as feature_lab_routes
from api.routes import mood as mood_routes
from api.routes import dao as dao_routes
try:
    from api.routes.dao_rewards import router as dao_rewards_router
except Exception:
    dao_rewards_router = None
from api.routes import world as world_routes
from api.routes import suggestions as suggestions_routes
from api.routes import drive as drive_routes
from api.routes import system as system_routes
from api.routes import aura_health as aura_health_routes
from api.routes import google_auth as google_auth_routes
from api.routes import github_oauth as github_oauth_routes
from api.routes import integrations as integrations_routes
from api.routes import events as events_routes
from api.routes import aura_autonomy as aura_autonomy_routes
from api.routes import council as council_routes
from api.routes import onboarding as onboarding_routes
from api.routes import surfaces as surfaces_routes
from api.routes import gamification as gamification_routes
from api.routes import perks as perks_routes
from api.routes import leaderboard as leaderboard_routes
from api.routes import friends as friends_routes
from api.routes import social_auth as social_auth_routes
from api.routes import knowledge as knowledge_routes
try:
    from api.routes import ioo as ioo_routes
    _ioo_available = True
except Exception as _ioo_err:
    ioo_routes = None
    _ioo_available = False
    logging.warning(f"IOO routes unavailable: {_ioo_err}")
try:
    from api.routes import ioo_execution as ioo_execution_routes
    _ioo_execution_available = True
except Exception as _ioo_execution_err:
    ioo_execution_routes = None
    _ioo_execution_available = False
    logging.warning(f"IOO execution routes unavailable: {_ioo_execution_err}")
try:
    from api.routes import services as services_routes
    _services_available = True
except Exception as _services_err:
    services_routes = None
    _services_available = False
    logging.warning(f"Services module unavailable: {_services_err}")
try:
    from api.routes import executive as executive_routes
    _executive_available = True
except Exception as _executive_err:
    executive_routes = None
    _executive_available = False
    logging.warning(f"Executive routes unavailable: {_executive_err}")
try:
    from api.routes import cgo as cgo_routes
    _cgo_available = True
except Exception as _cgo_err:
    cgo_routes = None
    _cgo_available = False
    logging.warning(f"CGO routes unavailable: {_cgo_err}")
from core.notification_worker import start_notification_worker, stop_notification_worker

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown tasks."""
    logger.info("🧠 Connectome starting up...")
    settings.validate_production_safety()

    # Run DB migrations
    await run_migrations()
    logger.info("✅ Database migrations complete")

    # Initialize Redis
    await get_redis()
    logger.info("✅ Redis connected")

    # Initialize Aura brain
    await init_brain()
    logger.info("✅ Aura brain initialized")

    # Start notification worker
    start_notification_worker()
    logger.info("✅ Notification worker started")

    # Start SelfHealingAgent
    self_healer = SelfHealingAgent()
    app.state.self_healer = self_healer
    import asyncio
    asyncio.create_task(self_healer.start_watching())
    logger.info("✅ SelfHealingAgent watching")

    # Start Aura daily self-check background task
    asyncio.create_task(_daily_self_check_loop())
    logger.info("✅ Aura daily self-check loop started")

    # Start Aura brain backup freshness loops (hourly identity backup + monitor)
    try:
        from aura.agents.backup_freshness import start_backup_freshness_loops
        start_backup_freshness_loops(app)
    except Exception as _backup_e:
        logger.warning(f"Aura backup freshness loops failed to start: {_backup_e}")

    # Start ModelEvolutionAgent weekly loop
    from aura.brain import get_brain as _get_brain
    _aura_brain = _get_brain()
    model_evolution_agent = ModelEvolutionAgent(_aura_brain._openai)
    app.state.model_evolution = model_evolution_agent
    asyncio.create_task(model_evolution_agent.start_weekly_check_loop())
    logger.info("✅ ModelEvolutionAgent weekly loop started")

    # Start DaoAgent background loops
    from aura.agents.dao_agent import DaoAgent
    dao_agent = DaoAgent(_aura_brain._openai)
    app.state.dao_agent = dao_agent
    asyncio.create_task(dao_agent.run_daily_evaluation_loop())
    asyncio.create_task(dao_agent.run_weekly_leaderboard_loop())
    asyncio.create_task(dao_agent.run_monthly_ltv_loop())
    logger.info("✅ DaoAgent evaluation + leaderboard + LTV loops started")

    # Start suggestion integration + CP award automation
    asyncio.create_task(_suggestion_integration_loop())
    logger.info("✅ Suggestion integration + CP award automation loop started")
    # Initialize EventDiscoveryAgent (lazy — syncs cities on demand)
    logger.info("✅ EventDiscoveryAgent ready (city syncs on demand)")

    # Start MetaAgent periodic self-improvement loop (every 6 hours)
    from aura.agents.meta_agent import MetaAgent
    meta_agent = MetaAgent(_aura_brain._openai)
    app.state.meta_agent = meta_agent
    asyncio.create_task(_meta_agent_loop(meta_agent))
    logger.info("✅ MetaAgent self-improvement loop started")

    # Initialize PricingAgent — Aura manages her own pricing
    try:
        from aura.agents.pricing_agent import get_pricing_agent
        pricing_agent = get_pricing_agent(getattr(_aura_brain, '_openai', None))
        app.state.pricing_agent = pricing_agent
        asyncio.create_task(_pricing_agent_loop(pricing_agent))
    except Exception as _pe:
        logging.warning(f"PricingAgent init failed (non-critical): {_pe}")
    logger.info("✅ PricingAgent initialized — Aura owns her monetization")

    logger.info("🚀 Connectome is live")
    yield

    # Shutdown
    logger.info("🛑 Connectome shutting down...")
    stop_notification_worker()
    await close_pool()
    await close_redis()
    logger.info("👋 Shutdown complete")


async def _aura_error_recovery_middleware(request, call_next):
    """
    Track errors on /api/aura/* endpoints.
    If an endpoint fails 3+ times in 60s, send a Telegram alert to Avi.
    """
    response = await call_next(request)
    path = request.url.path

    if path.startswith("/api/aura/") and response.status_code >= 500:
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            error_key = f"aura:errors:{path.replace('/', '_')}"
            count = await r.incr(error_key)
            if count == 1:
                await r.expire(error_key, 60)  # reset window every 60s
            if count >= 3:
                from datetime import datetime as _dt, timezone as _tz
                from core.telegram import send_telegram_message
                msg = (
                    f"⚠️ Aura Error Alert\n\n"
                    f"Endpoint {path} has failed {count}x in the last 60s.\n"
                    f"Status: {response.status_code}\n"
                    f"Time: {_dt.now(_tz.utc).isoformat()}"
                )
                if await send_telegram_message(msg):
                    logger.warning(f"Aura error alert sent for {path} (count={count})")
                    await r.delete(error_key)
        except Exception as _e:
            logger.debug(f"Error recovery middleware failed: {_e}")

    return response


# Rate limiter — keyed by IP address
limiter = Limiter(key_func=get_remote_address, default_limits=[])

# Create FastAPI app
app = FastAPI(
    title="Connectome API",
    description="Living AI.OS for human fulfilment. Powered by Aura.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Attach limiter state and error handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request timing
app.add_middleware(BaseHTTPMiddleware, dispatch=timing_middleware)

# Aura error recovery middleware — tracks /api/aura/* failures in Redis
app.add_middleware(BaseHTTPMiddleware, dispatch=_aura_error_recovery_middleware)

# Mount routes

# ─── Legacy /api/ora/* → /api/aura/* alias (deprecation window) ───────────────
# All canonical routes are now /api/aura/*. Keep /api/ora/* alive via in-process
# rewrite so already-deployed frontends and external integrations don't break
# during the rename rollout. Remove this middleware in Wave 5 cleanup.
@app.middleware("http")
async def legacy_ora_alias(request, call_next):
    if request.url.path.startswith("/api/ora/"):
        # Rewrite the scope path so the routed handler is the canonical /api/aura/* version
        new_path = "/api/aura/" + request.url.path[len("/api/ora/"):]
        request.scope["path"] = new_path
        request.scope["raw_path"] = new_path.encode("utf-8")
    return await call_next(request)

app.include_router(users.router)
app.include_router(screens.router)
app.include_router(feedback.router)
app.include_router(goals.router)
app.include_router(monetization.router)
if _payments_available and payments_routes:
    app.include_router(payments_routes.router)
app.include_router(sessions.router)
app.include_router(notifications.router)
app.include_router(ground_truth.router)
app.include_router(admin.router)
app.include_router(aura_chat.router)
app.include_router(discovery_routes.router)
app.include_router(ab_testing_routes.router)
app.include_router(explore_routes.router)
app.include_router(journal_routes.router)
app.include_router(feature_lab_routes.router)
app.include_router(mood_routes.router)
app.include_router(dao_routes.router)
if dao_rewards_router:
    app.include_router(dao_rewards_router)
app.include_router(world_routes.router)
app.include_router(suggestions_routes.router)
app.include_router(events_routes.router)
app.include_router(drive_routes.router)
app.include_router(system_routes.router)
app.include_router(aura_health_routes.router)
app.include_router(google_auth_routes.router)
app.include_router(github_oauth_routes.router)
app.include_router(integrations_routes.router)
app.include_router(aura_autonomy_routes.router)
app.include_router(council_routes.router)
app.include_router(onboarding_routes.router)
app.include_router(surfaces_routes.router)
app.include_router(gamification_routes.router)
app.include_router(perks_routes.router)
app.include_router(leaderboard_routes.router)
app.include_router(friends_routes.router)
app.include_router(social_auth_routes.router)
app.include_router(knowledge_routes.router)
if _services_available and services_routes:
    app.include_router(services_routes.router)
if _executive_available and executive_routes:
    app.include_router(executive_routes.router)
if _cgo_available and cgo_routes:
    app.include_router(cgo_routes.router)
if _ioo_available and ioo_routes:
    app.include_router(ioo_routes.router)
if _ioo_execution_available and ioo_execution_routes:
    app.include_router(ioo_execution_routes.router)
try:
    from api.routes.github_webhook import router as github_webhook_router
    app.include_router(github_webhook_router)
except Exception:
    pass


@app.get("/api/schema")
async def openapi_schema():
    """Return the OpenAPI JSON schema — lets Aura introspect her own API."""
    return app.openapi()


@app.get("/health")
async def health_check():
    """Health check for load balancers and monitoring."""
    from core.database import fetchval
    from core.redis_client import get_redis

    db_ok = False
    redis_ok = False

    try:
        result = await fetchval("SELECT 1")
        db_ok = result == 1
    except Exception as e:
        logger.warning(f"DB health check failed: {e}")

    try:
        r = await get_redis()
        await r.ping()
        redis_ok = True
    except Exception as e:
        logger.warning(f"Redis health check failed: {e}")

    return {
        "status": "ok" if (db_ok and redis_ok) else "degraded",
        "database": "ok" if db_ok else "error",
        "redis": "ok" if redis_ok else "error",
        "version": "1.0.0",
        "brain": "Aura",
    }


@app.get("/")
async def root():
    return {
        "app": "Connectome",
        "brain": "Aura",
        "tagline": "Living AI.OS for human fulfilment",
        "docs": "/docs",
    }


async def _meta_agent_loop(meta_agent):
    """Run MetaAgent self-improvement analysis every 6 hours."""
    import asyncio as _asyncio
    # Initial delay: 5 minutes after startup
    await _asyncio.sleep(300)
    while True:
        try:
            report = await meta_agent.generate_report()
            logger.info(
                f"MetaAgent: report generated — "
                f"top={report.get('top_engaging_card_types', [])}, "
                f"improvements={len(report.get('suggested_improvements', []))}"
            )
            # Apply meta report to brain weights
            try:
                from aura.brain import get_brain
                brain = get_brain()
                await brain.apply_meta_report(report)
            except Exception as _e:
                logger.debug(f"Brain weight update from meta report skipped: {_e}")
        except Exception as e:
            logger.error(f"MetaAgent loop failed: {e}")
        await _asyncio.sleep(6 * 3600)


async def _pricing_agent_loop(pricing_agent):
    """Run PricingAgent tier analysis once every 24 hours."""
    import asyncio as _asyncio
    # Initial delay: 10 minutes after startup (let MetaAgent run first)
    await _asyncio.sleep(600)
    while True:
        try:
            proposals = await pricing_agent.propose_tier_adjustment()
            logger.info(f"PricingAgent: {len(proposals)} tier proposals generated")
        except Exception as e:
            logger.error(f"PricingAgent loop failed: {e}")
        await _asyncio.sleep(24 * 3600)


async def _suggestion_integration_loop():
    """Periodically promote app feedback into actionable suggestions and award adoption CP."""
    import asyncio as _asyncio
    initial_delay_seconds = 5 * 60
    while True:
        try:
            await _asyncio.sleep(initial_delay_seconds)
            initial_delay_seconds = 6 * 3600
            from api.routes.suggestions import SuggestionAutomationRun, process_suggestion_automation

            result = await process_suggestion_automation(SuggestionAutomationRun(limit=100))
            logger.info(
                "SuggestionIntegration: imported=%s queued=%s awards=%s",
                result.get("imported_count", 0),
                result.get("queued_count", 0),
                result.get("awards_count", 0),
            )
        except Exception as e:
            logger.error(f"Suggestion integration loop failed: {e}")


async def _daily_self_check_loop():
    """Run Aura's self-check once every 24 hours."""
    import asyncio as _asyncio
    while True:
        try:
            await _asyncio.sleep(24 * 3600)
            brain = get_brain()
            result = await brain.consciousness.self_check()
            if not result.get("aligned"):
                logger.warning(
                    f"Aura self-check found issues: {result.get('issues', [])}"
                )
        except Exception as e:
            logger.error(f"Aura daily self-check failed: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=not settings.is_production,
        log_level=settings.LOG_LEVEL.lower(),
    )




# deploy-trigger: 2026-04-27T20:44:59Z
