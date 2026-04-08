"""
FastAPI security dependencies for Bearer token verification.

Two dependency functions are exported:
  verify_token  — validates any token (scope = "run" or "admin")
  require_admin — additionally enforces scope = "admin"

Both functions are used via FastAPI's Depends() system, which means they run
automatically before the route handler and raise HTTPException if auth fails.
The route handler never executes if auth fails.

Design notes:
  - Tokens are stored in the `tokens` table (not JWTs) — this means tokens can
    be revoked instantly by deleting the DB row. JWTs would require a blocklist.
  - DB failures return 503 (service unavailable), not 500, to distinguish
    "gateway broken" from "caller unauthenticated".
"""

import logging
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Token

logger = logging.getLogger("auth")

# HTTPBearer extracts the token from the "Authorization: Bearer <token>" header.
# auto_error=False means we handle the missing-header case ourselves with a clearer message instead of default.
security = HTTPBearer(auto_error=False)


async def verify_token(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
                       db: AsyncSession = Depends(get_db), ) -> Token:
    """
    Validates a Bearer token against the `tokens` table.

    Steps:
      1. Check the Authorization header is present at all.
      2. Query tokens table for a matching token string.
      3. Return the Token ORM object if found (used by require_admin to check scope).
      4. Raise 401 if missing or not found.
      5. Raise 503 if the DB itself is unavailable.

    All downstream routes that need auth use:
        token: Token = Depends(verify_token)
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing. Include 'Authorization: Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        result = await db.execute(select(Token).where(Token.token == credentials.credentials))
        token = result.scalar_one_or_none()
    except SQLAlchemyError as exc:
        logger.error("DB error during token verification: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth service temporarily unavailable. Please retry.",
        ) from exc

    if token is None:
        logger.warning(
            "Rejected token (first 8 chars): %s...",
            credentials.credentials[:8] if len(credentials.credentials) >= 8 else "??", )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked token.",
            headers={"WWW-Authenticate": "Bearer"}, )
    return token


async def require_admin(token: Token = Depends(verify_token), ) -> Token:
    """
    Extends verify_token by additionally asserting scope = "admin".

    Used on endpoints that should only be accessible to the Tech Lead:
      - Token creation / revocation
      - Agent deregistration

    Returns the Token object so the route handler can inspect it if needed.
    Raises 403 (not 401) because the caller IS authenticated — just not authorised.
    """
    if token.scope != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=("This endpoint requires admin scope. "
                    "Contact the Tech Lead to get an admin token."),
        )
    return token
