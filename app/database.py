"""
Async SQLAlchemy engine and session factory for the Agent Gateway.

Responsibilities:
  - Normalise DATABASE_URL regardless of which postgres:// variant arrives
  - Provide a FastAPI-compatible async session dependency (get_db)
  - Provide check_db_connection() — called at startup to fail fast if DB is unreachable
  - Provide init_db() — creates gateway-owned tables on every clean startup
"""

import logging
import os
from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import declarative_base

logger = logging.getLogger("database")

# URL normalisation
# asyncpg requires the "postgresql+asyncpg://" scheme.
# The env var may arrive as:
#   postgresql://...        (standard psycopg2 style)
#   postgres://...          (Heroku / DigitalOcean shorthand)
#   postgresql+asyncpg://   (already correct — pass through)

_RAW_URL: str = os.getenv("DATABASE_URL")
if not _RAW_URL:
    raise RuntimeError("DATABASE_URL is required")


def _normalise_url(url: str) -> str:
    """
    Convert any Postgres URL variant to the asyncpg driver format.
    Raises ValueError clearly if the scheme is unrecognised so the
    developer sees the problem immediately instead of a cryptic driver error.
    """
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    raise ValueError(
        f"DATABASE_URL has an unrecognised scheme. "
        f"Expected postgresql:// or postgres://, got: {url[:40]!r}"
    )


DATABASE_URL: str = _normalise_url(_RAW_URL)

# Engine
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=10,  # keep 10 connections alive — one per concurrent request
    max_overflow=20,  # allow up to 20 extra when 18 teams hit at the same time
    pool_pre_ping=True,  # send a lightweight ping before use; auto-reconnects stale connections
    pool_recycle=1800,  # recycle every 30 min to prevent server-side timeout disconnects
)

# Session
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False)

Base = declarative_base()


# FastAPI dependency
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Yields a fresh AsyncSession for each HTTP request and guarantees cleanup.

    Flow:
      - Session is opened from the pool.
      - On any SQLAlchemy error inside a route, we rollback to leave the DB clean.
      - Session is automatically closed (returned to pool) by async context manager.

    Usage in any router:
        async def endpoint(db: AsyncSession = Depends(get_db)):
            result = await db.execute(select(Agent))
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session

        except SQLAlchemyError as exc:
            await session.rollback()
            logger.error("DB session error — rolled back: %s", exc)
            raise


# Startup helpers
async def check_db_connection() -> None:
    """
    Runs a trivial SELECT 1 against the live DB.
    Called once during app lifespan startup.

    If the DB is unreachable, raises RuntimeError immediately so the server
    refuses to start rather than starting in a broken state that silently
    fails on every request.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("Database connection verified successfully.")

    except OperationalError as exc:
        raise RuntimeError(
            "Cannot reach the database. "
            "Verify DATABASE_URL, DB server status, and network/SSL settings.\n"
            f"Driver error: {exc}") from exc


async def init_db() -> None:
    """
    Creates all gateway-owned tables using CREATE TABLE IF NOT EXISTS semantics.
    Safe to call on every restart — never drops or modifies existing tables.

    Tables created (gateway layer only):
      - agents          → registered student services
      - agent_logs      → every /run call with input, output, latency
      - tokens          → bearer tokens for authentication
      - agent_feedback  → thumbs-up/down per run
      - health_checks   → timestamped ping results per agent
    """
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Gateway tables initialised (CREATE IF NOT EXISTS).")

    except SQLAlchemyError as exc:
        logger.critical("Failed to initialise gateway tables: %s", exc)
        raise
