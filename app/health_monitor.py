"""
Background health monitor.

Periodically pings all registered agents' /health endpoints and updates:
  - agents.status (online/offline)
  - health_checks audit table

Runs as a background asyncio task started at application startup.
"""

import asyncio
import logging
import os
from datetime import datetime

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from app.database import AsyncSessionLocal
from app.models import Agent, HealthCheck

logger = logging.getLogger("health_monitor")

HEALTH_CHECK_INTERVAL: int = int(os.getenv("HEALTH_CHECK_INTERVAL", "60"))  # seconds between full cycles
HEALTH_TIMEOUT: float = float(os.getenv("HEALTH_TIMEOUT", "5"))  # per-agent ping timeout


async def _ping_one_agent(
        client: httpx.AsyncClient,
        agent_name: str,
        endpoint: str,
) -> tuple[str, str]:
    """
    Ping an agent's /health endpoint.

    Returns:
        (agent_name, status) where status ∈ {"online", "offline"}.

    HTTP 200 → "online"
    Any non-200, failure (timeout, connection error, unexpected exception) → "offline"
    """
    try:
        resp = await client.get(
            f"{endpoint}/health",
            timeout=HEALTH_TIMEOUT,
        )
        return agent_name, ("online" if resp.status_code == 200 else "offline")
    except httpx.TimeoutException:
        logger.debug("Health ping timed out for agent '%s'", agent_name)
        return agent_name, "offline"
    except httpx.ConnectError:
        logger.debug("Health ping connection refused for agent '%s'", agent_name)
        return agent_name, "offline"
    except Exception as exc:
        # Catch-all so no matter what httpx does, we return "offline" cleanly.
        logger.warning("Unexpected error pinging agent '%s': %s", agent_name, exc)
        return agent_name, "offline"


async def _run_health_cycle() -> None:
    """
    One full health check cycle across all registered agents.

    Steps:
      1. Open a DB session and load all agents.
      2. Ping all agents concurrently (return_exceptions=True isolates failures).
      3. For each result:
         a. Log status changes at WARNING level.
         b. Update agents.status in the DB.
         c. Write a health_checks row for the audit trail.
      4. Commit all writes in a single transaction.
      5. On DB failure: log the error but do NOT raise — the loop must continue.
    """
    try:
        async with AsyncSessionLocal() as db:
            # Step 1: load agents
            result = await db.execute(select(Agent))
            agents = result.scalars().all()

            if not agents:
                logger.debug("No agents registered yet — skipping health cycle.")
                return

            # Step 2: concurrent pings
            async with httpx.AsyncClient() as client:
                ping_tasks = [
                    _ping_one_agent(client, agent.name, agent.endpoint)
                    for agent in agents
                ]
                raw_results = await asyncio.gather(*ping_tasks, return_exceptions=True)

            # Step 3: process results and write DB updates
            now = datetime.utcnow()
            for agent, raw in zip(agents, raw_results):
                # If somehow an exception slipped through gather, treat as offline
                if isinstance(raw, Exception):
                    logger.error(
                        "Unexpected exception in gather for agent '%s': %s",
                        agent.name, raw,
                    )
                    new_status = "offline"
                else:
                    _, new_status = raw

                # Log transitions to track outages
                if agent.status != new_status:
                    logger.warning(
                        "Agent '%s' status changed: %s → %s",
                        agent.name, agent.status, new_status,
                    )

                # Update the agents row
                await db.execute(
                    update(Agent)
                    .where(Agent.id == agent.id)
                    .values(status=new_status)
                )

                # Append a health_checks audit row
                db.add(
                    HealthCheck(
                        agent_name=agent.name,
                        status=new_status,
                        checked_at=now,
                    )
                )

            # Step 4: single commit for all updates + audit rows
            await db.commit()
            online = sum(1 for r in raw_results if not isinstance(r, Exception) and r[1] == "online")
            logger.info(
                "Health cycle complete: %d/%d agents online.", online, len(agents)
            )

    except SQLAlchemyError as exc:
        # DB failure during the cycle — log it and move on.
        # The monitor loop MUST continue so the next cycle can attempt recovery.
        logger.error("DB error during health cycle (will retry next cycle): %s", exc)
    except Exception as exc:
        logger.error("Unexpected error in health cycle: %s", exc)


async def health_monitor_loop() -> None:
    """
    Infinite asyncio loop that continuously runs health cycles.

    Called once via asyncio.create_task() in main.py's lifespan.
    Runs until the task is canceled on app shutdown.

    Structure:
      while True:
          run a full health cycle (all agents, concurrent)
          sleep HEALTH_CHECK_INTERVAL seconds
          repeat

    The sleep comes AFTER the cycle, not before, so checks begin immediately
    at startup rather than waiting 60 seconds for the first result.
    """
    logger.info(
        "Health monitor started — interval=%ds, per-agent timeout=%ds",
        HEALTH_CHECK_INTERVAL,
        HEALTH_TIMEOUT,
    )
    while True:
        await _run_health_cycle()
        try:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
        except asyncio.CancelledError:
            # Shutdown — app is stopping
            logger.info("Health monitor received shutdown signal.")
            raise
