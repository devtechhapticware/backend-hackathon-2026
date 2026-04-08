"""
routers/dashboard.py
====================
Single GET endpoint that gives the Tech Lead (and anyone with a token)
a quick read on the state of the entire platform:
  - How many agents are registered
  - How many are currently online vs offline
  - Total number of /run calls processed
  - Average latency across all calls
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_token
from app.database import get_db
from app.models import Agent, AgentLog, Token
from app.schemas import DashboardResponse

logger = logging.getLogger("dashboard")
router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get(
    "/",
    response_model=DashboardResponse,
    summary="System overview — agent count, run count, average latency",
)
async def dashboard(
    db: AsyncSession = Depends(get_db),
    _: Token = Depends(verify_token),
) -> DashboardResponse:
    """
    Aggregates key platform metrics in a single query round-trip.
    All four values come from the gateway's own tables so this is fast
    regardless of how many student services are registered.
    """
    try:
        total_agents = await db.scalar(
            select(func.count()).select_from(Agent)
        ) or 0

        online_agents = await db.scalar(
            select(func.count()).select_from(Agent).where(Agent.status == "online")
        ) or 0

        total_runs = await db.scalar(
            select(func.count()).select_from(AgentLog)
        ) or 0

        # avg() returns None if there are no rows; handle that explicitly
        raw_avg = await db.scalar(
            select(func.avg(AgentLog.latency_ms)).select_from(AgentLog)
        )
        avg_latency = round(float(raw_avg), 2) if raw_avg is not None else None

        return DashboardResponse(
            total_agents=total_agents,
            online_agents=online_agents,
            offline_agents=total_agents - online_agents,
            total_runs=total_runs,
            avg_latency_ms=avg_latency,
        )

    except SQLAlchemyError as exc:
        logger.error("DB error fetching dashboard stats: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error fetching dashboard. Please retry.",
        ) from exc
