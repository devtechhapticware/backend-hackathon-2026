import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

load_dotenv()

from fastapi import FastAPI

from app.database import AsyncSessionLocal, check_db_connection, init_db
from app.models import Token
from app.agents import router as agents_router
from app.gateway import router as gateway_router
from app.health_monitor import health_monitor_loop
from app.logs import router as logs_router
from app.feedback import router as feedback_router
from app.dashboard import router as dashboard_router
from app.admin import router as admin_router

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(name)-15s]  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger("gateway")


# Admin Token Seeding
async def seed_admin_token():
    """
    Seed an admin token if none exists.
    The token is printed once in logs and must be saved by the Tech Lead.
    """
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Token).where(Token.scope == "admin"))
            if result.scalar_one_or_none():
                logger.info("Admin token already exists — skipping seeding.")
                return

            token_value = secrets.token_urlsafe(32)
            db.add(Token(token=token_value, scope="admin"))
            await db.commit()

            logger.warning("\n" + "=" * 60)
            logger.warning("INITIAL ADMIN TOKEN — SAVE THIS IMMEDIATELY")
            logger.warning("")
            logger.warning("   %s", token_value)
            logger.warning("")
            logger.warning("Use: Authorization: Bearer <token>")
            logger.warning("=" * 60 + "\n")

    except SQLAlchemyError as e:
        logger.critical(
            "Failed to seed admin token: %s\n"
            "Manually INSERT INTO tokens (token, scope) VALUES ('your_token', 'admin');",
            e
        )


# Lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifecycle manager.

    Startup:
      - Verify DB connection
      - Initialize tables
      - Seed admin token
      - Start health monitor

    Shutdown:
      - Stop background tasks cleanly
    """

    # Startup
    logger.info("Starting Agent Gateway...")
    try:
        await check_db_connection()

    except RuntimeError as e:
        logger.critical("Startup failed: cannot connect to database: %s", e)
        raise

    await init_db()
    await seed_admin_token()

    # Start health monitor
    monitor_task = asyncio.create_task(
        health_monitor_loop(),
        name="health_monitor",
    )

    logger.info("Startup complete. Gateway is ready.")
    yield

    # Shutdown
    logger.info("Shutting down Agent Gateway...")
    monitor_task.cancel()
    try:
        await monitor_task
    except asyncio.CancelledError:
        logger.info("Health monitor stopped cleanly.")


# FastAPI App
app = FastAPI(
    title="Agent Gateway",
    description="Central gateway for managing and routing agent services.",
    version="1.0.0",
    lifespan=lifespan,
)

# Allow all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(gateway_router)
app.include_router(agents_router)
app.include_router(logs_router)
app.include_router(feedback_router)
app.include_router(dashboard_router)
app.include_router(admin_router)


# Public Endpoints
@app.get("/", tags=["Meta"], include_in_schema=False)
async def root():
    """Basic liveness endpoint."""
    return {"message": "Agent Gateway running",
            "docs": "/docs",
            "health": "/health",
            }


@app.get("/health", tags=["Meta"], summary="Gateway liveness check")
async def health():
    """Gateway health check (no auth)."""
    return {"status": "online"}
