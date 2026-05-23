"""
Google Calendar integration.
- OAuth token management (per-user refresh tokens stored in SQLite).
- FreeBusy queries for conflict detection.
- Event creation with attendees.
- .ics generation and Google Calendar "add event" deep-link.
"""
from __future__ import annotations

import os
import urllib.parse
from datetime import datetime, timedelta

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from ics import Calendar, Event

from app.schemas import BusyInterval
from app.timezones import to_doha as _to_doha
from app import store

_SCOPES = ["https://www.googleapis.com/auth/calendar"]
_TOKEN_URI = "https://oauth2.googleapis.com/token"


def _credentials(refresh_token: str) -> Credentials:
    # Read at call time (not module load time) so load_dotenv() is always in effect.
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError(
            "GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET missing from environment. "
            "Check your .env file."
        )
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=_TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=_SCOPES,
    )
    creds.refresh(Request())
    return creds


def freebusy(
    slack_id: str,
    window_start: datetime,
    window_end: datetime,
) -> list[BusyInterval]:
    """
    Return busy intervals from the user's Google Calendar over the given window.
    Returns an empty list if the user has not connected their calendar.
    """
    refresh_token = store.get_google_token(slack_id)
    if not refresh_token:
        return []

    try:
        creds = _credentials(refresh_token)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        body = {
            "timeMin": window_start.isoformat(),
            "timeMax": window_end.isoformat(),
            "items": [{"id": "primary"}],
        }
        result = service.freebusy().query(body=body).execute()
        busy_raw = result.get("calendars", {}).get("primary", {}).get("busy", [])
        # Convert to Doha tz so all BusyIntervals are on the same tz as config-based ones.
        # fromisoformat handles the 'Z' UTC suffix in Python 3.11+.
        return [
            BusyInterval(
                start=_to_doha(datetime.fromisoformat(b["start"])),
                end=_to_doha(datetime.fromisoformat(b["end"])),
                source="google_calendar",
                label="Existing event",
            )
            for b in busy_raw
        ]
    except Exception as exc:
        print(f"[gcal] FreeBusy error for {slack_id}: {exc}")
        return []


def create_event(
    slack_id: str,
    summary: str,
    start: datetime,
    end: datetime,
    attendees: list[str],
    description: str = "",
) -> str | None:
    """
    Create a Google Calendar event on the user's primary calendar.
    Returns the event HTML link, or None on failure.
    """
    refresh_token = store.get_google_token(slack_id)
    if not refresh_token:
        return None

    try:
        creds = _credentials(refresh_token)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        event = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Qatar"},
            "end":   {"dateTime": end.isoformat(),   "timeZone": "Asia/Qatar"},
            "attendees": [{"email": e} for e in attendees if e],
            "reminders": {"useDefault": True},
        }
        created = service.events().insert(
            calendarId="primary",
            body=event,
            sendUpdates="all",
        ).execute()
        return created.get("htmlLink")
    except Exception as exc:
        print(f"[gcal] create_event error for {slack_id}: {exc}")
        return None


def make_ics(
    summary: str,
    start: datetime,
    end: datetime,
    attendees: list[str],
    description: str = "",
) -> bytes:
    """Generate an .ics file as bytes."""
    cal = Calendar()
    ev = Event()
    ev.name = summary
    ev.begin = start.isoformat()
    ev.end = end.isoformat()
    ev.description = description
    for email in attendees:
        if email:
            ev.add_attendee(email)
    cal.events.add(ev)
    return cal.serialize().encode("utf-8")


def add_to_gcal_url(
    summary: str,
    start: datetime,
    end: datetime,
    description: str = "",
    attendees: list[str] | None = None,
) -> str:
    """
    Return a Google Calendar 'add event' URL the user can click to add it themselves.
    """
    fmt = "%Y%m%dT%H%M%S"
    params: dict[str, str] = {
        "action": "TEMPLATE",
        "text": summary,
        "dates": f"{start.strftime(fmt)}/{end.strftime(fmt)}",
        "details": description,
    }
    if attendees:
        params["add"] = ",".join(a for a in attendees if a)
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(params)
