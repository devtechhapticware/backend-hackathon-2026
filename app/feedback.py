"""
Feedback API for rating agent executions.

Allows callers to submit thumbs-up (1) or thumbs-down (0)
linked to a specific run via run_id.

Used to evaluate agent performance over time.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_token
from app.database import get_db
from app.models import Agent, AgentFeedback, Token
from app.schemas import FeedbackRequest, FeedbackResponse

logger = logging.getLogger("feedback")
router = APIRouter(prefix="/feedback", tags=["Feedback"])


@router.post(
    "/{agent_name}",
    response_model=FeedbackResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit thumbs-up (1) or thumbs-down (0) for a completed run",
)
async def submit_feedback(
        agent_name: str = Path(..., description="The agent slug the run was for"),
        body: FeedbackRequest = ...,
        db: AsyncSession = Depends(get_db),
        _: Token = Depends(verify_token),
) -> FeedbackResponse:
    """
    Submit feedback for a specific agent run.
        - agent_name: target agent
        - run_id: identifies execution (from /run)
        - rating: 0 (bad) or 1 (good)
        - comment: optional notes
    """
    try:
        # Verify the agent exists so feedback isn't submitted for a ghost name
        agent_result = await db.execute(
            select(Agent).where(Agent.name == agent_name)
        )
        if agent_result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No agent registered with name '{agent_name}'.",
            )

        fb = AgentFeedback(
            agent_name=agent_name,
            run_id=body.run_id,
            rating=body.rating,
            comment=body.comment,
        )
        db.add(fb)
        await db.commit()
        await db.refresh(fb)

        logger.info(
            "Feedback submitted: agent='%s' run_id='%s' rating=%d",
            agent_name, body.run_id, body.rating,
        )
        return fb

    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.error("DB error submitting feedback for agent '%s': %s", agent_name, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error submitting feedback. Please retry.",
        ) from exc
