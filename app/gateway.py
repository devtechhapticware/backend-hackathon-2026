"""
Execution Gateway (/run)

Central routing layer of the platform.

Responsibilities:
  - Resolve agent by name
  - Forward payload to agent `/run` endpoint
  - Handle timeouts, connection errors, and bad responses
  - Enforce response contract (agent_name, result, status)
  - Log input, output, and latency asynchronously

Flow:
  1. Authenticate request
  2. Lookup agent (must be online)
  3. Forward request
  4. Measure latency
  5. Validate response
  6. Log execution (non-blocking)
  7. Return standardized response
"""

import asyncio
import logging
import os
import time
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_token
from app.database import AsyncSessionLocal, get_db
from app.models import Agent, AgentLog, Token
from app.schemas import RunRequest, RunResponse

logger = logging.getLogger("gateway")
router = APIRouter(tags=["Gateway"])
REQUEST_TIMEOUT: int = int(os.getenv("AGENT_REQUEST_TIMEOUT", "30"))


# Logging helper
async def _persist_log(
        agent_name: str,
        input_data: dict,
        output_data: dict,
        latency_ms: float,
) -> None:
    """
    Writes one row to agent_logs using its OWN session.

    Why a separate session?
    The request-scoped `db` session (from get_db) may be in a failed state
    after an HTTP exception is raised — attempting to write to it would
    silently swallow the insert. Using a fresh session from AsyncSessionLocal
    guarantees the log is always committed regardless of what happened in
    the request's main session.

    This is fire-and-forget — called with asyncio.create_task() so a log
    write failure never blocks or changes the response returned to the caller.
    """
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                AgentLog(
                    agent_name=agent_name,
                    input=input_data,
                    output=output_data,
                    latency_ms=round(latency_ms, 2),
                )
            )
            await db.commit()
    except SQLAlchemyError as exc:
        # A log failure is bad but must NOT affect the caller.
        # Log it loudly so the Tech Lead can investigate.
        logger.error(
            "FAILED to persist agent_log for agent='%s': %s",
            agent_name, exc,
        )
    except Exception as exc:
        logger.error(
            "Unexpected error persisting agent_log for agent='%s': %s",
            agent_name, exc,
        )


# Main endpoint
@router.post(
    "/run",
    response_model=RunResponse,
    summary="Route a payload to a registered agent",
    responses={
        200: {"description": "Agent executed successfully"},
        401: {"description": "Missing or invalid Bearer token"},
        404: {"description": "Agent name not registered"},
        422: {"description": "Request body failed schema validation"},
        503: {"description": "Agent is registered but currently offline"},
        502: {"description": "Agent returned a non-standard response or HTTP error"},
        504: {"description": "Agent did not respond within the timeout"},
    },
)
async def run_agent(
        agent_name: str = Query(
            ...,
            description="Registered slug of the target agent, e.g. 'lead_scorer'",
        ),
        body: RunRequest = ...,
        db: AsyncSession = Depends(get_db),
        _token: Token = Depends(verify_token),
) -> RunResponse:
    """
    The core gateway endpoint. Routes `payload` to the named agent and returns
    its response, enriched with a `run_id` and `latency_ms`.

    Every call is logged in `agent_logs` regardless of success or failure.
    """

    # Step 1: resolve agent
    try:
        result = await db.execute(
            select(Agent).where(Agent.name == agent_name)
        )
        agent = result.scalar_one_or_none()
    except SQLAlchemyError as e:
        logger.error("DB error looking up agent '%s': %s", agent_name, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gateway database temporarily unavailable. Please retry.",
        ) from e

    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No agent registered with name '{agent_name}'. "
                f"Check the name or the correct slug."
            ),
        )

    # Step 2: check agent is online
    if agent.status != "online":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Agent '{agent_name}' is currently offline. "
                f"The health monitor updates status every {os.getenv('HEALTH_CHECK_INTERVAL', '60')}s. "
                f"Check that your service is running and /health returns 200."
            ),
        )

    # Step 3: forward request to agent service
    run_id = str(uuid.uuid4())
    outbound = {"payload": body.payload}
    start = time.perf_counter()
    elapsed_ms: float = 0.0
    response_data: dict[str, Any] | None = None

    try:
        async with httpx.AsyncClient() as client:
            http_response = await client.post(
                f"{agent.endpoint}/run",
                json=outbound,
                timeout=REQUEST_TIMEOUT,
            )

        elapsed_ms = (time.perf_counter() - start) * 1000

        # Step 4: check HTTP-level success BEFORE reading body
        http_response.raise_for_status()
        response_data = http_response.json()

    except httpx.TimeoutException:
        elapsed_ms = (time.perf_counter() - start) * 1000
        error_out = {"error": f"Agent timed out after {REQUEST_TIMEOUT}s"}
        asyncio.create_task(_persist_log(agent_name, outbound, error_out, elapsed_ms))
        logger.warning("Agent '%s' timed out after %ds", agent_name, REQUEST_TIMEOUT)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=(
                f"Agent '{agent_name}' did not respond within {REQUEST_TIMEOUT} seconds. "
                f"Check if the service is overloaded or the LLM call is too slow."
            ),
        )

    except httpx.HTTPStatusError as e:
        elapsed_ms = (time.perf_counter() - start) * 1000
        error_out = {"error": f"HTTP {e.response.status_code} from agent"}
        asyncio.create_task(_persist_log(agent_name, outbound, error_out, elapsed_ms))
        logger.warning(
            "Agent '%s' returned HTTP %d", agent_name, e.response.status_code
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Agent '{agent_name}' returned HTTP {e.response.status_code}. "
                f"Ensure your /run endpoint handles errors internally and returns "
                f"status='error' in the response body rather than raising HTTP errors."
            ),
        )

    except httpx.ConnectError:
        elapsed_ms = (time.perf_counter() - start) * 1000
        error_out = {"error": "Connection refused — service unreachable"}
        asyncio.create_task(_persist_log(agent_name, outbound, error_out, elapsed_ms))
        logger.warning("Agent '%s' connection refused", agent_name)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Cannot connect to agent '{agent_name}' at {agent.endpoint}. "
                f"Is the service running? Did the IP/port change? "
                f"Use PATCH /agents/{agent_name}/endpoint to update if needed."
            ),
        )

    except Exception as e:
        elapsed_ms = (time.perf_counter() - start) * 1000
        error_out = {"error": str(e)}
        asyncio.create_task(_persist_log(agent_name, outbound, error_out, elapsed_ms))
        logger.exception("Unexpected error calling agent '%s': %s", agent_name, e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Unexpected error reaching agent '{agent_name}': {e}",
        )

    # Step 5: validate the response contract
    missing = [f for f in ("agent_name", "result", "status") if f not in response_data]
    if missing:
        error_out = {
            "error": f"Contract violation — missing fields: {missing}",
            "raw_response": response_data,
        }
        asyncio.create_task(_persist_log(agent_name, outbound, error_out, elapsed_ms))
        logger.error(
            "Agent '%s' contract violation — missing: %s", agent_name, missing
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Agent '{agent_name}' response is missing required fields: {missing}. "
                f"Your /run must return exactly: "
                f'{{"agent_name": "...", "result": {{}}, "status": "success|error"}}'
            ),
        )

    # Step 6: persist log
    asyncio.create_task(_persist_log(agent_name, outbound, response_data, elapsed_ms))

    logger.info(
        "run OK agent=%s status=%s latency=%.1fms run_id=%s",
        agent_name, response_data["status"], elapsed_ms, run_id,
    )

    # Step 7: return response
    return RunResponse(
        agent_name=response_data["agent_name"],
        result=response_data["result"],
        status=response_data["status"],
        run_id=run_id,
        latency_ms=round(elapsed_ms, 2),
    )
