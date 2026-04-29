"""GitHub OAuth connection routes for Connectome users."""

import logging
import urllib.parse
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from api.middleware import decode_token
from core.config import settings
from core.database import execute, fetchrow

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth/github", tags=["github-auth"])

GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
FRONTEND_CALLBACK_URL = "https://avielcarlos.github.io/connectome-web/auth/github-callback"


async def _ensure_schema() -> None:
    await execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS github_username TEXT")
    await execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS github_avatar_url TEXT")
    await execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS github_connected BOOLEAN DEFAULT FALSE")
    await execute("CREATE INDEX IF NOT EXISTS idx_users_github ON users(github_username)")
    await execute("ALTER TABLE contributors ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id)")
    await execute("CREATE INDEX IF NOT EXISTS idx_contributors_user_id ON contributors(user_id)")


def _require_client_id() -> str:
    if not settings.GITHUB_CLIENT_ID:
        raise HTTPException(status_code=503, detail="GitHub OAuth not configured (GITHUB_CLIENT_ID missing)")
    return settings.GITHUB_CLIENT_ID


def _require_client_secret() -> str:
    if not settings.GITHUB_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="GitHub OAuth not configured (GITHUB_CLIENT_SECRET missing)")
    return settings.GITHUB_CLIENT_SECRET


@router.get("/login")
async def github_login(token: str = Query(..., description="Current Connectome JWT")):
    """Redirect the authenticated Connectome user to GitHub OAuth."""
    user_id = decode_token(token)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    await _ensure_schema()
    params = {
        "client_id": _require_client_id(),
        "redirect_uri": settings.GITHUB_REDIRECT_URI,
        "scope": "user:email read:user",
        "state": token,
        "allow_signup": "true",
    }
    return RedirectResponse(f"{GITHUB_AUTH_URL}?{urllib.parse.urlencode(params)}")


@router.get("/callback")
async def github_callback(code: str = Query(...), state: str = Query(...)):
    """Exchange GitHub code, store profile fields, and redirect to frontend."""
    user_id = decode_token(state)
    if not user_id:
        return RedirectResponse(f"{FRONTEND_CALLBACK_URL}?github_connected=false&error=invalid_state")

    await _ensure_schema()
    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            GITHUB_TOKEN_URL,
            json={
                "client_id": _require_client_id(),
                "client_secret": _require_client_secret(),
                "code": code,
                "redirect_uri": settings.GITHUB_REDIRECT_URI,
            },
            headers={"Accept": "application/json"},
        )
    if token_resp.status_code != 200:
        logger.error("GitHub token exchange failed: %s %s", token_resp.status_code, token_resp.text)
        return RedirectResponse(f"{FRONTEND_CALLBACK_URL}?github_connected=false&error=token_exchange")

    access_token = token_resp.json().get("access_token")
    if not access_token:
        return RedirectResponse(f"{FRONTEND_CALLBACK_URL}?github_connected=false&error=no_token")

    async with httpx.AsyncClient(timeout=10) as client:
        user_resp = await client.get(
            GITHUB_USER_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
        )
    if user_resp.status_code != 200:
        logger.error("GitHub profile fetch failed: %s %s", user_resp.status_code, user_resp.text)
        return RedirectResponse(f"{FRONTEND_CALLBACK_URL}?github_connected=false&error=profile_fetch")

    gh = user_resp.json()
    username = (gh.get("login") or "").lower().strip()
    avatar_url = gh.get("avatar_url")
    if not username:
        return RedirectResponse(f"{FRONTEND_CALLBACK_URL}?github_connected=false&error=no_username")

    uid = UUID(user_id)
    await execute(
        """
        UPDATE users
        SET github_username = $2, github_avatar_url = $3, github_connected = TRUE
        WHERE id = $1
        """,
        uid, username, avatar_url,
    )

    user_row = await fetchrow("SELECT email, profile FROM users WHERE id = $1", uid)
    profile = user_row["profile"] if user_row else {}
    if not isinstance(profile, dict):
        profile = {}
    display_name = profile.get("display_name") or gh.get("name") or username
    email = user_row["email"] if user_row else None

    await execute(
        """
        INSERT INTO contributors (github_username, display_name, email, avatar_url, user_id)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (github_username) DO UPDATE SET
            display_name = COALESCE(EXCLUDED.display_name, contributors.display_name),
            email = COALESCE(EXCLUDED.email, contributors.email),
            avatar_url = COALESCE(EXCLUDED.avatar_url, contributors.avatar_url),
            user_id = EXCLUDED.user_id
        """,
        username, display_name, email, avatar_url, uid,
    )

    return RedirectResponse(
        f"{FRONTEND_CALLBACK_URL}?github_connected=true&github_username={urllib.parse.quote(username)}"
    )
