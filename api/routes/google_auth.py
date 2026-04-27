"""
Google OAuth Routes
===================
Handles Google Sign-In (OpenID Connect) and optional Drive integration.

Endpoints:
  GET  /api/auth/google/login             → redirect URL to Google consent screen
  GET  /api/auth/google/callback          → exchange code, create/update user, return JWT
  POST /api/auth/google/drive/connect     → request Drive scope for existing user
  DELETE /api/auth/google/drive/disconnect → revoke Drive access + delete tokens

Environment variables required:
  GOOGLE_CLIENT_ID      — from console.cloud.google.com
  GOOGLE_CLIENT_SECRET  — from console.cloud.google.com
  GOOGLE_REDIRECT_URI   — must match OAuth app config:
                          https://connectome-api-production.up.railway.app/api/auth/google/callback

Setup instructions:
  1. Go to console.cloud.google.com → APIs & Services → Credentials
  2. Create OAuth 2.0 Client ID (Web application)
  3. Add authorized redirect URI:
       https://connectome-api-production.up.railway.app/api/auth/google/callback
  4. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in Railway env vars
  5. Also add GOOGLE_REDIRECT_URI if different from default above
"""

import hashlib
import logging
import os
import secrets
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from api.middleware import create_access_token, get_current_user_id
from core.config import settings
from core.database import execute, fetch, fetchrow, fetchval

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth/google", tags=["google-auth"])

# ─── Constants ───────────────────────────────────────────────────────────────

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

BASIC_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
]

# Where the web app lives — after OAuth we redirect here with the JWT
FRONTEND_CALLBACK_URL = "https://avielcarlos.github.io/connectome-web/auth/callback"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get_google_client_id() -> str:
    v = settings.GOOGLE_CLIENT_ID
    if not v:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth not configured (GOOGLE_CLIENT_ID missing)",
        )
    return v


def _get_google_client_secret() -> str:
    v = settings.GOOGLE_CLIENT_SECRET
    if not v:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth not configured (GOOGLE_CLIENT_SECRET missing)",
        )
    return v


def _get_redirect_uri() -> str:
    return settings.GOOGLE_REDIRECT_URI or (
        "https://connectome-api-production.up.railway.app/api/auth/google/callback"
    )


def _build_auth_url(scopes: list[str], state: str) -> str:
    params = {
        "client_id": _get_google_client_id(),
        "redirect_uri": _get_redirect_uri(),
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "access_type": "offline",   # get refresh_token
        "prompt": "consent",         # always show consent to ensure refresh_token
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


async def _exchange_code(code: str) -> dict:
    """Exchange authorization code for access + refresh tokens."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": _get_google_client_id(),
                "client_secret": _get_google_client_secret(),
                "redirect_uri": _get_redirect_uri(),
                "grant_type": "authorization_code",
                "code": code,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code != 200:
        logger.error(f"Google token exchange failed: {resp.status_code} {resp.text}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to exchange Google authorization code",
        )
    return resp.json()


async def _get_user_info(access_token: str) -> dict:
    """Fetch Google user profile."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to fetch Google user info",
        )
    return resp.json()


async def _refresh_access_token(user_id: str) -> Optional[str]:
    """
    Refresh the OAuth access token using the stored refresh token.
    Returns new access_token on success, None on failure.
    On failure, marks drive_connected=False for the user.
    """
    row = await fetchrow(
        "SELECT refresh_token FROM google_oauth_tokens WHERE user_id = $1",
        UUID(user_id),
    )
    if not row or not row["refresh_token"]:
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": _get_google_client_id(),
                    "client_secret": _get_google_client_secret(),
                    "grant_type": "refresh_token",
                    "refresh_token": row["refresh_token"],
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if resp.status_code != 200:
            logger.warning(f"Token refresh failed for user {user_id}: {resp.text}")
            # Mark drive as disconnected — user will need to reconnect
            await execute(
                """
                UPDATE google_oauth_tokens
                SET drive_connected = FALSE, updated_at = NOW()
                WHERE user_id = $1
                """,
                UUID(user_id),
            )
            return None

        data = resp.json()
        new_token = data.get("access_token")
        expires_in = data.get("expires_in", 3600)
        expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        await execute(
            """
            UPDATE google_oauth_tokens
            SET access_token = $2, token_expiry = $3, updated_at = NOW()
            WHERE user_id = $1
            """,
            UUID(user_id),
            new_token,
            expiry,
        )
        return new_token

    except Exception as e:
        logger.error(f"Token refresh exception for user {user_id}: {e}")
        await execute(
            """
            UPDATE google_oauth_tokens
            SET drive_connected = FALSE, updated_at = NOW()
            WHERE user_id = $1
            """,
            UUID(user_id),
        )
        return None


async def get_valid_access_token(user_id: str) -> Optional[str]:
    """
    Return a valid (possibly refreshed) access token for the user.
    Public so drive_agent_v2 can use it.
    """
    row = await fetchrow(
        "SELECT access_token, refresh_token, token_expiry FROM google_oauth_tokens WHERE user_id = $1",
        UUID(user_id),
    )
    if not row:
        return None

    # Check if token is still valid (with 5-minute buffer)
    if row["token_expiry"]:
        expiry = row["token_expiry"]
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) < expiry - timedelta(minutes=5):
            return row["access_token"]

    # Token expired — refresh it
    return await _refresh_access_token(user_id)


async def _upsert_oauth_tokens(
    user_id: str,
    access_token: str,
    refresh_token: Optional[str],
    expires_in: int,
    scopes: list[str],
    drive_scopes_granted: bool = False,
) -> None:
    """Upsert OAuth tokens for a user."""
    expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    await execute(
        """
        INSERT INTO google_oauth_tokens
            (user_id, access_token, refresh_token, token_expiry, scopes, drive_connected, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            access_token  = EXCLUDED.access_token,
            refresh_token = COALESCE(EXCLUDED.refresh_token, google_oauth_tokens.refresh_token),
            token_expiry  = EXCLUDED.token_expiry,
            scopes        = EXCLUDED.scopes,
            drive_connected = CASE
                WHEN $6 THEN TRUE
                ELSE google_oauth_tokens.drive_connected
            END,
            updated_at    = NOW()
        """,
        UUID(user_id),
        access_token,
        refresh_token,
        expiry,
        scopes,
        drive_scopes_granted,
    )


# ─── Routes ──────────────────────────────────────────────────────────────────


@router.get("/login")
async def google_login(
    include_drive: bool = Query(
        default=False,
        description="Request Drive read access in addition to basic profile",
    ),
):
    """
    Step 1: Redirect user to Google consent screen.
    ?include_drive=true requests Drive read access immediately.
    """
    scopes = BASIC_SCOPES[:]
    if include_drive:
        scopes += DRIVE_SCOPES

    # State encodes what scopes were requested (so callback knows what to expect)
    state = secrets.token_urlsafe(32)
    if include_drive:
        state += ":drive"

    auth_url = _build_auth_url(scopes, state)
    return RedirectResponse(auth_url)


@router.get("/callback")
async def google_callback(
    code: str = Query(...),
    state: str = Query(""),
    error: Optional[str] = Query(default=None),
):
    """
    Step 2: Google redirects here after user consent.
    Exchange code → tokens → create/update user → issue JWT → redirect to frontend.
    """
    if error:
        # User denied consent — redirect to login page with error
        frontend_error = f"{FRONTEND_CALLBACK_URL}?error={urllib.parse.quote(error)}"
        return RedirectResponse(frontend_error)

    drive_requested = ":drive" in state

    try:
        # Exchange authorization code for tokens
        token_data = await _exchange_code(code)
    except HTTPException as e:
        return RedirectResponse(
            f"{FRONTEND_CALLBACK_URL}?error={urllib.parse.quote(e.detail)}"
        )

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 3600)
    granted_scope = token_data.get("scope", "")
    drive_granted = "drive.readonly" in granted_scope

    # Get user profile from Google
    try:
        user_info = await _get_user_info(access_token)
    except HTTPException as e:
        return RedirectResponse(
            f"{FRONTEND_CALLBACK_URL}?error={urllib.parse.quote(e.detail)}"
        )

    google_id = user_info.get("sub")
    email = user_info.get("email", "")
    name = user_info.get("name", "")
    picture = user_info.get("picture", "")

    # Find or create user
    # First try to find by google_id
    user_row = await fetchrow(
        "SELECT id FROM users WHERE google_id = $1", google_id
    )

    if not user_row:
        # Try by email (they may have registered with email/password before)
        user_row = await fetchrow(
            "SELECT id FROM users WHERE email = $1", email
        )

    if user_row:
        user_id = str(user_row["id"])
        # Update google_id and profile if missing
        await execute(
            """
            UPDATE users
            SET google_id = COALESCE(google_id, $2),
                auth_provider = CASE WHEN google_id IS NULL THEN 'google' ELSE auth_provider END,
                last_active = NOW()
            WHERE id = $1
            """,
            UUID(user_id),
            google_id,
        )
    else:
        # Create new user from Google profile
        import json as _json
        profile = {
            "display_name": name,
            "avatar_url": picture,
        }
        user_row = await fetchrow(
            """
            INSERT INTO users (email, google_id, auth_provider, profile, last_active)
            VALUES ($1, $2, 'google', $3, NOW())
            RETURNING id
            """,
            email,
            google_id,
            _json.dumps(profile),
        )
        user_id = str(user_row["id"])
        logger.info(f"New Google user created: {user_id[:8]} ({email})")

    # Store OAuth tokens
    scopes = granted_scope.split() if granted_scope else BASIC_SCOPES
    await _upsert_oauth_tokens(
        user_id=user_id,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        scopes=scopes,
        drive_scopes_granted=drive_granted,
    )

    # Issue our app JWT
    jwt_token = create_access_token(user_id)

    logger.info(
        f"Google OAuth success: user={user_id[:8]} drive_granted={drive_granted}"
    )

    # Redirect to frontend with JWT
    redirect_url = f"{FRONTEND_CALLBACK_URL}?token={urllib.parse.quote(jwt_token)}"
    return RedirectResponse(redirect_url)


class DriveConnectRequest(BaseModel):
    redirect_after: bool = True  # if True, return a redirect URL; if False, return JSON


@router.post("/drive/connect")
async def drive_connect(
    user_id: str = Depends(get_current_user_id),
):
    """
    Step 3 (optional): Request Drive read scope for an already-logged-in user.
    Returns a redirect URL for the Google consent screen with Drive scope.
    The callback will then update their token record with the Drive access.
    """
    # Build auth URL with Drive scope included
    state = secrets.token_urlsafe(32) + ":drive"
    scopes = BASIC_SCOPES + DRIVE_SCOPES
    auth_url = _build_auth_url(scopes, state)

    return {"ok": True, "auth_url": auth_url, "message": "Redirect user to auth_url to grant Drive access"}


@router.delete("/drive/disconnect")
async def drive_disconnect(
    user_id: str = Depends(get_current_user_id),
):
    """
    Revoke Drive access: delete OAuth tokens and remove indexed Drive documents.
    The user stays logged in (their Connectome JWT is not affected).
    """
    # Get token for revocation
    row = await fetchrow(
        "SELECT access_token, refresh_token FROM google_oauth_tokens WHERE user_id = $1",
        UUID(user_id),
    )

    if row:
        # Revoke with Google (best-effort)
        token_to_revoke = row["refresh_token"] or row["access_token"]
        if token_to_revoke:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        GOOGLE_REVOKE_URL,
                        params={"token": token_to_revoke},
                    )
            except Exception as e:
                logger.warning(f"Google revoke call failed (ignoring): {e}")

        # Delete tokens from DB
        await execute(
            "DELETE FROM google_oauth_tokens WHERE user_id = $1",
            UUID(user_id),
        )

    # Delete indexed Drive documents for this user
    await execute(
        "DELETE FROM drive_documents WHERE owner_user_id = $1::text",
        user_id,
    )

    logger.info(f"Drive disconnected for user {user_id[:8]}")
    return {
        "ok": True,
        "message": "Google Drive access revoked and indexed documents removed",
    }
