"""
Read-only access to execution logs.

Provides visibility into:
  - agent usage
  - request/response data
  - latency and failures

Useful for monitoring, debugging, and performance analysis.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_token
from app.database import get_db
from app.models import AgentLog, Token
from app.schemas import LogEntry

logger = logging.getLogger("logs")
router = APIRouter(prefix="/logs", tags=["Logs"])


@router.get(
    "/",
    response_model=List[LogEntry],
    summary="Retrieve run logs, newest first. Optionally filter by agent.",
)
async def get_logs(
    agent_name: Optional[str] = Query(
        None,
        description="Filter to a specific agent slug, e.g. 'lead_scorer'",
    ),
    limit: int = Query(
        50,
        ge=1,
        le=200,
        description="Number of log entries to return (max 200)",
    ),
    db: AsyncSession = Depends(get_db),
    _: Token = Depends(verify_token),
) -> List[LogEntry]:
    """
    Returns the most recent run logs, ordered newest-first.

    If `agent_name` is provided, only logs for that agent are returned.
    If omitted, logs for all agents are returned (up to `limit`).
    """
    try:
        query = (
            select(AgentLog)
            .order_by(AgentLog.created_at.desc())
            .limit(limit)
        )
        if agent_name:
            query = query.where(AgentLog.agent_name == agent_name)

        result = await db.execute(query)
        return result.scalars().all()

    except SQLAlchemyError as exc:
        logger.error("DB error fetching logs (agent_name=%s): %s", agent_name, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error retrieving logs. Please retry.",
        ) from exc
