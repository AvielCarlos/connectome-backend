"""
Social Auth API — Google, Facebook, and Apple token exchange.

Each endpoint verifies the provider token, upserts a local user, and returns the
standard Connectome JWT.
"""

import logging
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, status
from jose import JWTError, jwt
from pydantic import BaseModel

from api.middleware import create_access_token
from core.config import settings
from core.database import execute, fetchrow
from core.models import TokenResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth/social", tags=["social-auth"])


class GoogleSocialLogin(BaseModel):
    id_token: str


class FacebookSocialLogin(BaseModel):
    access_token: str


class AppleSocialLogin(BaseModel):
    identity_token: str
    name: Optional[str] = None


async def _find_or_create_social_user(
    *,
    provider: str,
    provider_id: str,
    email: Optional[str],
    name: Optional[str],
    avatar_url: Optional[str],
) -> str:
    if email:
        email = email.lower()

    row = await fetchrow(
        """
        SELECT id FROM users
        WHERE (auth_provider = $1 AND provider_id = $2)
           OR ($3::text IS NOT NULL AND lower(email) = $3)
        ORDER BY CASE WHEN auth_provider = $1 AND provider_id = $2 THEN 0 ELSE 1 END
        LIMIT 1
        """,
        provider,
        provider_id,
        email,
    )
    if row:
        await execute(
            """
            UPDATE users
            SET auth_provider = $1,
                provider_id = $2,
                avatar_url = COALESCE($3, avatar_url),
                display_name = COALESCE($4, display_name, profile->>'display_name'),
                last_active = NOW()
            WHERE id = $5
            """,
            provider,
            provider_id,
            avatar_url,
            name,
            row["id"],
        )
        return str(row["id"])

    new_row = await fetchrow(
        """
        INSERT INTO users (email, auth_provider, provider_id, avatar_url, display_name, profile, last_active)
        VALUES ($1, $2, $3, $4, $5, jsonb_build_object('display_name', $5), NOW())
        RETURNING id
        """,
        email,
        provider,
        provider_id,
        avatar_url,
        name,
    )
    return str(new_row["id"])


def _token_response(user_id: str) -> TokenResponse:
    return TokenResponse(access_token=create_access_token(user_id), user_id=user_id)


@router.post("/google", response_model=TokenResponse)
async def google_social_login(body: GoogleSocialLogin):
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": body.id_token},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Google token")
    data = resp.json()

    configured_aud = getattr(settings, "GOOGLE_CLIENT_ID", "")
    if not configured_aud:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google social login not configured")
    if data.get("aud") != configured_aud:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Google token audience mismatch")

    provider_id = data.get("sub")
    if not provider_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Google token missing subject")

    user_id = await _find_or_create_social_user(
        provider="google",
        provider_id=provider_id,
        email=data.get("email"),
        name=data.get("name"),
        avatar_url=data.get("picture"),
    )
    return _token_response(user_id)


@router.post("/facebook", response_model=TokenResponse)
async def facebook_social_login(body: FacebookSocialLogin):
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://graph.facebook.com/me",
            params={
                "fields": "id,name,email,picture",
                "access_token": body.access_token,
            },
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Facebook token")
    data = resp.json()

    provider_id = data.get("id")
    if not provider_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Facebook token missing id")

    configured_app_id = getattr(settings, "FACEBOOK_APP_ID", "")
    if configured_app_id:
        async with httpx.AsyncClient(timeout=10) as client:
            app_resp = await client.get(
                "https://graph.facebook.com/app",
                params={"access_token": body.access_token},
            )
        if app_resp.status_code != 200 or str(app_resp.json().get("id")) != str(configured_app_id):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Facebook token app mismatch")

    picture = data.get("picture") or {}
    avatar_url = None
    if isinstance(picture, dict):
        avatar_url = (picture.get("data") or {}).get("url")

    user_id = await _find_or_create_social_user(
        provider="facebook",
        provider_id=provider_id,
        email=data.get("email"),
        name=data.get("name"),
        avatar_url=avatar_url,
    )
    return _token_response(user_id)


@router.post("/apple", response_model=TokenResponse)
async def apple_social_login(body: AppleSocialLogin):
    try:
        header = jwt.get_unverified_header(body.identity_token)
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Apple token")

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get("https://appleid.apple.com/auth/keys")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Could not fetch Apple public keys")

    key = next((k for k in resp.json().get("keys", []) if k.get("kid") == header.get("kid")), None)
    if not key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Apple signing key not found")

    apple_client_id = getattr(settings, "APPLE_CLIENT_ID", "") or getattr(settings, "IOS_BUNDLE_ID", "")
    if not apple_client_id:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Apple social login not configured")
    decode_kwargs = {
        "algorithms": ["RS256"],
        "issuer": "https://appleid.apple.com",
        "audience": apple_client_id,
    }

    try:
        claims = jwt.decode(body.identity_token, key, **decode_kwargs)
    except JWTError as exc:
        logger.info("Apple token verification failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Apple token")

    provider_id = claims.get("sub")
    if not provider_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Apple token missing subject")

    user_id = await _find_or_create_social_user(
        provider="apple",
        provider_id=provider_id,
        email=claims.get("email"),
        name=body.name,
        avatar_url=None,
    )
    return _token_response(user_id)
