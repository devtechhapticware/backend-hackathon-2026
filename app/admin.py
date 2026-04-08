"""
Token lifecycle management — create, list, revoke.

All endpoints require scope='admin'. Student teams never touch these.

Token design:
  - Tokens are opaque random strings (secrets.token_urlsafe(32) = 43 chars).
  - They are stored in plaintext in the DB — acceptable for a hackathon environment.
    In production you would store hashed tokens.
  - Revocation is instant: delete the row and the token is rejected on the next call.
  - scope='run' for student teams, scope='admin' for the Tech Lead.
"""

import logging
import secrets
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.database import get_db
from app.models import Token
from app.schemas import TokenCreateRequest, TokenResponse

logger = logging.getLogger("admin")
router = APIRouter(prefix="/admin", tags=["Admin"])


@router.post(
    "/tokens",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a new Bearer token and assign it to a team",
)
async def create_token(
    body: TokenCreateRequest,
    db: AsyncSession = Depends(get_db),
    _: Token = Depends(require_admin),
) -> TokenResponse:
    """
    Generates a cryptographically secure token.

    Workflow for the Tech Lead:
      1. Call this endpoint with scope='run' for each student team.
      2. Give the returned `token` string to the team.
      3. The team includes it as 'Authorization: Bearer <token>' in every call.

    Use scope='admin' only for creating additional Tech Lead accounts.

    The token string uses URL-safe base64 encoding (no +, /, = characters)
    so it can be safely passed in Authorization headers.
    """
    try:
        raw_token = secrets.token_urlsafe(32)
        token = Token(token=raw_token, scope=body.scope)
        db.add(token)
        await db.commit()
        await db.refresh(token)

        logger.info("Token created with scope='%s'", body.scope)
        return token

    except IntegrityError:
        # Astronomically unlikely (32 bytes of randomness) but handled properly.
        await db.rollback()
        logger.error("Token collision on insert — this should never happen.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Token generation collision. Please retry.",
        )
    except SQLAlchemyError as e:
        await db.rollback()
        logger.error("DB error creating token: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error creating token. Please retry.",
        ) from e


@router.get(
    "/tokens",
    response_model=List[TokenResponse],
    summary="List all issued tokens",
)
async def list_tokens(
    db: AsyncSession = Depends(get_db),
    _: Token = Depends(require_admin),
) -> List[TokenResponse]:
    """Lists all tokens, ordered by creation time. Used to audit which teams have access."""
    try:
        result = await db.execute(select(Token).order_by(Token.created_at.asc()))
        return result.scalars().all()
    except SQLAlchemyError as e:
        logger.error("DB error listing tokens: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error retrieving tokens.",
        ) from e


@router.delete(
    "/tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a token by its DB id",
)
async def revoke_token(
    token_id: int = Path(..., description="The integer id of the token to revoke"),
    db: AsyncSession = Depends(get_db),
    _: Token = Depends(require_admin),
) -> None:
    """
    Revokes a token by its database id (not the token string itself).
    Using the id avoids URL encoding issues and protects the token value from
    appearing in server access logs.

    After revocation, any caller using that token receives 401 immediately.
    """
    try:
        result = await db.execute(select(Token).where(Token.id == token_id))
        token = result.scalar_one_or_none()

        if token is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No token found with id={token_id}.",
            )

        scope = token.scope
        await db.delete(token)
        await db.commit()
        logger.warning("Token id=%d (scope='%s') was revoked by an admin.", token_id, scope)

    except HTTPException:
        raise
    except SQLAlchemyError as e:
        await db.rollback()
        logger.error("DB error revoking token id=%d: %s", token_id, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error revoking token. Please retry.",
        ) from e
