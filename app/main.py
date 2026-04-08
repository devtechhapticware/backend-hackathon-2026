import logging
import secrets
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from sqlalchemy import select

load_dotenv()

from fastapi import FastAPI

from app.database import AsyncSessionLocal, check_db_connection, init_db
from app.models import Token
from app.agents import router as agents_router
from app.gateway import router as gateway_router

# Logging setup
logger = logging.getLogger("gateway")


# Token Seeding
async def seed_admin_token():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Token).where(Token.scope == "admin"))
        if result.scalar_one_or_none():
            return

        token_value = secrets.token_urlsafe(32)
        db.add(Token(token=token_value, scope="admin"))
        await db.commit()
        logger.warning("\n" + "=" * 60)
        logger.warning("ADMIN TOKEN IS GENERATED ONLY ONCE SAVE THIS IMMEDIATELY)")
        logger.warning("")
        logger.warning("   %s", token_value)
        logger.warning("")
        logger.warning("Use it as: Authorization: Bearer <token>")
        logger.warning("=" * 60 + "\n")


# Lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Handles application startup and shutdown lifecycle.
    Startup:
    - Verifies database connectivity
    - Initializes database tables

    Shutdown:
    - Reserved for future cleanup tasks
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
    logger.info("Startup complete. Gateway is ready.")
    yield

    # Shutdown
    logger.info("Shutting down Agent Gateway...")


# FastAPI App Instance
app = FastAPI(
    title="Agent Gateway",
    description="Central gateway for managing and routing agent services.",
    version="1.0.0",
    lifespan=lifespan, )

# Router
app.include_router(agents_router)
app.include_router(gateway_router)


# Endpoints
@app.get("/")
async def root():
    return {"message": "Agent Gateway running"}
