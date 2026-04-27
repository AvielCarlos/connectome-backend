"""
Connectome API Middleware
JWT auth + request timing + CORS.
"""

import time
import logging
from typing import Optional
from uuid import UUID

from fastapi import Request, HTTPException, status, Depends
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
import bcrypt

from core.config import settings
from core.database import fetchrow

logger = logging.getLogger(__name__)

# OAuth2 bearer token scheme
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/users/login")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def create_access_token(user_id: str) -> str:
    from datetime import datetime, timedelta, timezone
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {"sub": user_id, "exp": expire}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_token(token: str) -> Optional[str]:
    """Decode a JWT and return user_id (sub), or None if invalid."""
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        return payload.get("sub")
    except JWTError:
        return None


async def get_current_user_id(token: str = Depends(oauth2_scheme)) -> str:
    """FastAPI dependency: extract and validate user_id from bearer token."""
    user_id = decode_token(token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_id


async def require_premium(
    user_id: str = Depends(get_current_user_id),
) -> str:
    """FastAPI dependency: require premium subscription."""
    row = await fetchrow(
        "SELECT subscription_tier FROM users WHERE id = $1", UUID(user_id)
    )
    if not row or row["subscription_tier"] != "premium":
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Premium subscription required",
        )
    return user_id


# ---------------------------------------------------------------------------
# Request timing middleware
# ---------------------------------------------------------------------------

async def timing_middleware(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = (time.monotonic() - start) * 1000
    response.headers["X-Process-Time-Ms"] = f"{duration_ms:.1f}"
    if duration_ms > 2000:
        logger.warning(f"Slow request: {request.method} {request.url.path} {duration_ms:.0f}ms")
    return response
