import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI

from app.database import check_db_connection, init_db

# Logging setup
logger = logging.getLogger("gateway")


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


# Endpoints
@app.get("/")
async def root():
    return {"message": "Agent Gateway running"}
