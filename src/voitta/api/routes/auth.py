"""Microsoft login (Azure AD OAuth2 Authorization Code flow)."""

import logging
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from ..deps import DB
from ...config import get_settings
from ...db.models import User

logger = logging.getLogger(__name__)
router = APIRouter()

LOGIN_SCOPES = "openid profile email User.Read"


def _redirect_uri() -> str:
    settings = get_settings()
    return f"{settings.base_url}/auth/microsoft/callback"


@router.get("/microsoft")
async def microsoft_login():
    """Redirect to Microsoft login page."""
    settings = get_settings()
    if not settings.ms_auth_enabled:
        raise HTTPException(status_code=404, detail="Microsoft login not configured")

    state = secrets.token_urlsafe(32)
    params = {
        "client_id": settings.ms_auth_client_id,
        "response_type": "code",
        "redirect_uri": _redirect_uri(),
        "response_mode": "query",
        "scope": LOGIN_SCOPES,
        "state": state,
    }
    url = (
        f"https://login.microsoftonline.com/{settings.ms_auth_tenant_id}/oauth2/v2.0/authorize?"
        + urlencode(params)
    )
    return RedirectResponse(url=url)


@router.get("/microsoft/callback")
async def microsoft_callback(
    db: DB,
    code: str = Query(...),
    state: str = Query(""),
):
    """Handle Microsoft OAuth2 callback — exchange code, get profile, login."""
    settings = get_settings()
    if not settings.ms_auth_enabled:
        raise HTTPException(status_code=404, detail="Microsoft login not configured")

    # Exchange authorization code for tokens
    token_url = (
        f"https://login.microsoftonline.com/{settings.ms_auth_tenant_id}/oauth2/v2.0/token"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            token_url,
            data={
                "client_id": settings.ms_auth_client_id,
                "client_secret": settings.ms_auth_client_secret,
                "code": code,
                "redirect_uri": _redirect_uri(),
                "grant_type": "authorization_code",
                "scope": LOGIN_SCOPES,
            },
        )
        if resp.status_code != 200:
            logger.error("Token exchange failed (%d): %s", resp.status_code, resp.text[:500])
            raise HTTPException(status_code=502, detail="Microsoft token exchange failed")
        tokens = resp.json()

        # Fetch user profile from Microsoft Graph
        access_token = tokens["access_token"]
        me_resp = await client.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if me_resp.status_code != 200:
            logger.error("Graph /me failed (%d): %s", me_resp.status_code, me_resp.text[:500])
            raise HTTPException(status_code=502, detail="Failed to fetch Microsoft profile")
        profile = me_resp.json()

    email = (profile.get("mail") or profile.get("userPrincipalName") or "").lower().strip()
    display_name = profile.get("displayName") or email.split("@")[0]

    if not email:
        raise HTTPException(status_code=400, detail="Could not determine email from Microsoft profile")

    logger.info("Microsoft login: email=%s, name=%s", email, display_name)

    # Find existing user by email, or create a new one
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user is None:
        user = User(name=display_name, email=email)
        db.add(user)
        await db.flush()
        logger.info("Created new user id=%d for %s", user.id, email)

    response = RedirectResponse(url="/browse", status_code=302)
    response.set_cookie(
        key="voitta_user_id",
        value=str(user.id),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 365,
    )
    return response
