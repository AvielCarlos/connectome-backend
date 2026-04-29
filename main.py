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
from ora.brain import init_brain, get_brain
from ora.agents.self_healing import SelfHealingAgent
from ora.agents.model_evolution import ModelEvolutionAgent
from api.middleware import timing_middleware
from api.routes import users, screens, feedback, goals, monetization, sessions, notifications, ground_truth, admin
try:
    from api.routes import payments as payments_routes
    _payments_available = True
except Exception as _payments_err:
    payments_routes = None
    _payments_available = False
    logging.warning(f"Payments module unavailable: {_payments_err}")
from api.routes import ora_chat
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
from api.routes import ora_health as ora_health_routes
from api.routes import google_auth as google_auth_routes
from api.routes import integrations as integrations_routes
from api.routes import events as events_routes
from api.routes import ora_autonomy as ora_autonomy_routes
from api.routes import onboarding as onboarding_routes
from api.routes import surfaces as surfaces_routes
from api.routes import gamification as gamification_routes
try:
    from api.routes import ioo as ioo_routes
    _ioo_available = True
except Exception as _ioo_err:
    ioo_routes = None
    _ioo_available = False
    logging.warning(f"IOO routes unavailable: {_ioo_err}")
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

    # Run DB migrations
    await run_migrations()
    logger.info("✅ Database migrations complete")

    # Initialize Redis
    await get_redis()
    logger.info("✅ Redis connected")

    # Initialize Ora brain
    await init_brain()
    logger.info("✅ Ora brain initialized")

    # Start notification worker
    start_notification_worker()
    logger.info("✅ Notification worker started")

    # Start SelfHealingAgent
    self_healer = SelfHealingAgent()
    app.state.self_healer = self_healer
    import asyncio
    asyncio.create_task(self_healer.start_watching())
    logger.info("✅ SelfHealingAgent watching")

    # Start Ora daily self-check background task
    asyncio.create_task(_daily_self_check_loop())
    logger.info("✅ Ora daily self-check loop started")

    # Start ModelEvolutionAgent weekly loop
    from ora.brain import get_brain as _get_brain
    _ora_brain = _get_brain()
    model_evolution_agent = ModelEvolutionAgent(_ora_brain._openai)
    app.state.model_evolution = model_evolution_agent
    asyncio.create_task(model_evolution_agent.start_weekly_check_loop())
    logger.info("✅ ModelEvolutionAgent weekly loop started")

    # Start DaoAgent background loops
    from ora.agents.dao_agent import DaoAgent
    dao_agent = DaoAgent(_ora_brain._openai)
    app.state.dao_agent = dao_agent
    asyncio.create_task(dao_agent.run_daily_evaluation_loop())
    asyncio.create_task(dao_agent.run_weekly_leaderboard_loop())
    asyncio.create_task(dao_agent.run_monthly_ltv_loop())
    logger.info("✅ DaoAgent evaluation + leaderboard + LTV loops started")
    # Initialize EventDiscoveryAgent (lazy — syncs cities on demand)
    logger.info("✅ EventDiscoveryAgent ready (city syncs on demand)")

    # Start MetaAgent periodic self-improvement loop (every 6 hours)
    from ora.agents.meta_agent import MetaAgent
    meta_agent = MetaAgent(_ora_brain._openai)
    app.state.meta_agent = meta_agent
    asyncio.create_task(_meta_agent_loop(meta_agent))
    logger.info("✅ MetaAgent self-improvement loop started")

    # Initialize PricingAgent — Ora manages her own pricing
    try:
        from ora.agents.pricing_agent import get_pricing_agent
        pricing_agent = get_pricing_agent(getattr(_ora_brain, '_openai', None))
        app.state.pricing_agent = pricing_agent
        asyncio.create_task(_pricing_agent_loop(pricing_agent))
    except Exception as _pe:
        logging.warning(f"PricingAgent init failed (non-critical): {_pe}")
    logger.info("✅ PricingAgent initialized — Ora owns her monetization")

    logger.info("🚀 Connectome is live")
    yield

    # Shutdown
    logger.info("🛑 Connectome shutting down...")
    stop_notification_worker()
    await close_pool()
    await close_redis()
    logger.info("👋 Shutdown complete")


async def _ora_error_recovery_middleware(request, call_next):
    """
    Track errors on /api/ora/* endpoints.
    If an endpoint fails 3+ times in 60s, send a Telegram alert to Avi.
    """
    response = await call_next(request)
    path = request.url.path

    if path.startswith("/api/ora/") and response.status_code >= 500:
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            error_key = f"ora:errors:{path.replace('/', '_')}"
            count = await r.incr(error_key)
            if count == 1:
                await r.expire(error_key, 60)  # reset window every 60s
            if count >= 3:
                import httpx as _httpx, os as _os
                token = _os.environ.get("ORA_TELEGRAM_TOKEN", "")
                if not token:
                    try:
                        with open("/Users/avielcarlos/.openclaw/secrets/telegram-bot-token.txt") as f:
                            token = f.read().strip()
                    except Exception:
                        pass
                if token:
                    from datetime import datetime as _dt, timezone as _tz
                    msg = (
                        f"⚠️ Ora Error Alert\n\n"
                        f"Endpoint {path} has failed {count}x in the last 60s.\n"
                        f"Status: {response.status_code}\n"
                        f"Time: {_dt.now(_tz.utc).isoformat()}"
                    )
                    try:
                        async with _httpx.AsyncClient(timeout=10) as client:
                            await client.post(
                                f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": 5716959016, "text": msg},
                            )
                        logger.warning(f"Ora error alert sent for {path} (count={count})")
                        await r.delete(error_key)
                    except Exception as _te:
                        logger.debug(f"Error alert send failed: {_te}")
        except Exception as _e:
            logger.debug(f"Error recovery middleware failed: {_e}")

    return response


# Rate limiter — keyed by IP address
limiter = Limiter(key_func=get_remote_address, default_limits=[])

# Create FastAPI app
app = FastAPI(
    title="Connectome API",
    description="Living AI.OS for human fulfilment. Powered by Ora.",
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

# Ora error recovery middleware — tracks /api/ora/* failures in Redis
app.add_middleware(BaseHTTPMiddleware, dispatch=_ora_error_recovery_middleware)

# Mount routes
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
app.include_router(ora_chat.router)
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
app.include_router(ora_health_routes.router)
app.include_router(google_auth_routes.router)
app.include_router(integrations_routes.router)
app.include_router(ora_autonomy_routes.router)
app.include_router(onboarding_routes.router)
app.include_router(surfaces_routes.router)
app.include_router(gamification_routes.router)
if _services_available and services_routes:
    app.include_router(services_routes.router)
if _executive_available and executive_routes:
    app.include_router(executive_routes.router)
if _cgo_available and cgo_routes:
    app.include_router(cgo_routes.router)
if _ioo_available and ioo_routes:
    app.include_router(ioo_routes.router)
try:
    from api.routes.github_webhook import router as github_webhook_router
    app.include_router(github_webhook_router)
except Exception:
    pass


@app.get("/api/schema")
async def openapi_schema():
    """Return the OpenAPI JSON schema — lets Ora introspect her own API."""
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
        "brain": "Ora",
    }


@app.get("/")
async def root():
    return {
        "app": "Connectome",
        "brain": "Ora",
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
                from ora.brain import get_brain
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


async def _daily_self_check_loop():
    """Run Ora's self-check once every 24 hours."""
    import asyncio as _asyncio
    while True:
        try:
            await _asyncio.sleep(24 * 3600)
            brain = get_brain()
            result = await brain.consciousness.self_check()
            if not result.get("aligned"):
                logger.warning(
                    f"Ora self-check found issues: {result.get('issues', [])}"
                )
        except Exception as e:
            logger.error(f"Ora daily self-check failed: {e}")


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
