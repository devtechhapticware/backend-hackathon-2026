"""
Agent registry endpoints — how student teams connect their services to the gateway.

Endpoints:
  POST   /agents/register          — student team registers their service
  GET    /agents/                  — list all registered services
  PATCH  /agents/{name}/endpoint   — update service URL (body, not query param)
  DELETE /agents/{name}            — deregister (admin only)
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin, verify_token
from app.database import get_db
from app.models import Agent, Token
from app.schemas import AgentRegisterRequest, AgentResponse, AgentUpdateEndpointRequest

logger = logging.getLogger("agents")
router = APIRouter(prefix="/agents", tags=["Agent Registry"])


@router.post(
    "/register",
    response_model=AgentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new student service with the gateway",
)
async def register_agent(
        body: AgentRegisterRequest,
        db: AsyncSession = Depends(get_db),
        _: Token = Depends(verify_token),
) -> AgentResponse:
    """
    Called once by each student team when their service is running and reachable.

    Validation (done by Pydantic before this function runs):
      - name must be lowercase alphanumeric + underscores
      - endpoint must be a valid http:// or https:// URL

    After registration:
      - The health monitor will ping /health within the next 60 seconds.
      - The gateway will route /run calls to this agent.
    """
    try:
        # Check for duplicate name before attempting insert to give a clearer error
        existing = await db.execute(select(Agent).where(Agent.name == body.name))
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(f"An agent named '{body.name}' is already registered. "
                        f"Use PATCH /agents/{body.name}/endpoint to update the URL, "
                        f"or choose a different name."
                        ),
            )

        agent = Agent(
            name=body.name,
            category=body.category,
            endpoint=body.endpoint,
            status="online",  # assume online; health monitor will correct within one cycle
        )
        db.add(agent)
        await db.commit()
        await db.refresh(agent)

        logger.info(
            "Agent registered: name='%s' category='%s' endpoint='%s'",
            agent.name, agent.category, agent.endpoint,
        )
        return agent

    except HTTPException:
        raise  # re-raise our own HTTPExceptions unchanged
    except IntegrityError:
        # Race condition: another request registered the same name between our SELECT and INSERT. Treat same as the duplicate check above.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Agent '{body.name}' was just registered by a concurrent request.",
        )
    except SQLAlchemyError as e:
        await db.rollback()
        logger.error("DB error registering agent '%s': %s", body.name, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error during registration. Please retry.",
        ) from e


@router.get(
    "/",
    response_model=List[AgentResponse],
    summary="List all registered agents and their current status",
)
async def list_agents(
        db: AsyncSession = Depends(get_db),
        _: Token = Depends(verify_token),
) -> List[AgentResponse]:
    """
    Returns all registered services ordered by registration time.
    The `status` field reflects the most recent health-monitor result.
    """
    try:
        result = await db.execute(
            select(Agent).order_by(Agent.created_at.asc())
        )
        return list(result.scalars().all())
    except SQLAlchemyError as e:
        logger.error("DB error listing agents: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error retrieving agent list. Please retry.",
        ) from e


@router.patch(
    "/{name}/endpoint",
    response_model=AgentResponse,
    summary="Update the base URL of a registered agent",
)
async def update_endpoint(
        name: str = Path(..., description="The registered agent slug"),
        body: AgentUpdateEndpointRequest = ...,
        db: AsyncSession = Depends(get_db),
        _: Token = Depends(verify_token),
) -> AgentResponse:
    """
    Use this when your service restarts on a different port or gets a new IP.
    The endpoint URL is in the request body (not a query param) so URLs with
    special characters (&, ?, =) are handled correctly.

    After updating, the health monitor will ping the new URL within 60 seconds
    and update the status accordingly.
    """
    try:
        result = await db.execute(select(Agent).where(Agent.name == name))
        agent = result.scalar_one_or_none()

        if agent is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No agent registered with name '{name}'.",
            )

        old_endpoint = agent.endpoint
        agent.endpoint = body.endpoint
        agent.status = "online"  # reset optimistically; health monitor will correct
        await db.commit()
        await db.refresh(agent)

        logger.info(
            "Agent '%s' endpoint updated: '%s' → '%s'",
            name, old_endpoint, body.endpoint,
        )
        return agent

    except HTTPException:
        raise
    except SQLAlchemyError as e:
        await db.rollback()
        logger.error("DB error updating endpoint for agent '%s': %s", name, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error updating endpoint. Please retry.",
        ) from e


@router.delete("/{name}",
               status_code=status.HTTP_204_NO_CONTENT,
               summary="Deregister an agent — removes it from the gateway (admin only)",
)
async def deregister_agent(
        name: str = Path(..., description="The registered agent slug"),
        db: AsyncSession = Depends(get_db),
        _: Token = Depends(require_admin),
) -> None:
    """
    Permanently removes an agent from the registry.
    Past logs in agent_logs and health_checks are preserved for audit purposes.
    Admin scope required.
    """
    try:
        result = await db.execute(select(Agent).where(Agent.name == name))
        agent = result.scalar_one_or_none()

        if agent is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No agent registered with name '{name}'.", )

        await db.delete(agent)
        await db.commit()
        logger.warning("Agent '%s' was deregistered by an admin.", name)

    except HTTPException:
        raise
    except SQLAlchemyError as e:
        await db.rollback()
        logger.error("DB error deregistering agent '%s': %s", name, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error during deregistration. Please retry.", ) from e
