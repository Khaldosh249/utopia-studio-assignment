"""
Minimal FastAPI server for Google OAuth callback only.
Slack itself uses Socket Mode and needs no HTTP server.
Run alongside the Slack bot via threading in main.py.
"""
from __future__ import annotations

import os
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import store

app = FastAPI(docs_url=None, redoc_url=None)

_SCOPES = "https://www.googleapis.com/auth/calendar"
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"

# In-memory pending state: {state_token -> slack_id}
_pending: dict[str, str] = {}


def build_auth_url(slack_id: str) -> str:
    # Read at call time so load_dotenv() is always in effect.
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI", "http://localhost:8080/oauth/callback")
    state = secrets.token_urlsafe(16)
    _pending[state] = slack_id
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": _SCOPES,
        "access_type": "offline",
        "prompt": "consent",  # always ask, so we always get a refresh_token
        "state": state,
    }
    return _AUTH_URL + "?" + urlencode(params)


@app.get("/oauth/callback")
async def oauth_callback(code: str, state: str, request: Request) -> HTMLResponse:
    slack_id = _pending.pop(state, None)
    if not slack_id:
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI", "http://localhost:8080/oauth/callback")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to exchange token")

    token_data = resp.json()
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=502, detail="No refresh_token in response")

    store.save_google_token(slack_id, refresh_token)

    return HTMLResponse(
        "<h2>✅ Google Calendar connected!</h2>"
        "<p>You can close this tab and return to Slack.</p>"
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
