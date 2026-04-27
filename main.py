"""
Connectome Backend — FastAPI Application
Entry point. Mounts all routes and manages lifespan.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from core.config import settings
from core.database import run_migrations, close_pool
from core.redis_client import get_redis, close_redis
from ora.brain import init_brain
from ora.agents.self_healing import SelfHealingAgent
from ora.agents.model_evolution import ModelEvolutionAgent
from api.middleware import timing_middleware
from api.routes import users, screens, feedback, goals, monetization, sessions, notifications, ground_truth, admin
from api.routes import ora_chat
from api.routes import discovery as discovery_routes
from api.routes import ab_testing as ab_testing_routes
from api.routes import explore as explore_routes
from api.routes import journal as journal_routes
from api.routes import feature_lab as feature_lab_routes
from api.routes import mood as mood_routes
from api.routes import dao as dao_routes
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

    logger.info("🚀 Connectome is live")
    yield

    # Shutdown
    logger.info("🛑 Connectome shutting down...")
    stop_notification_worker()
    await close_pool()
    await close_redis()
    logger.info("👋 Shutdown complete")


# Create FastAPI app
app = FastAPI(
    title="Connectome API",
    description="Living AI.OS for human fulfilment. Powered by Ora.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
)

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

# Mount routes
app.include_router(users.router)
app.include_router(screens.router)
app.include_router(feedback.router)
app.include_router(goals.router)
app.include_router(monetization.router)
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
