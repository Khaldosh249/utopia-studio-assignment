"""
Claude-powered natural-language -> MeetingRequest parser.
The LLM extracts intent only; all time math happens in timezones.py.
Prompt caching is applied to the (large, static) system prompt.
"""
from __future__ import annotations

import json
import os

import anthropic
from dotenv import load_dotenv

from app.schemas import MeetingRequest, Participant
from app import store

load_dotenv()

_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
_MODEL = "claude-sonnet-4-6"

_SYSTEM_PROMPT = """You are a scheduling assistant for Utopia Studio, a venture studio based in Doha, Qatar (UTC+3, Asia/Qatar, no DST).

Your ONLY job is to extract structured scheduling intent from a natural-language request. You do NOT calculate times, convert timezones, or resolve relative dates — that is handled by separate deterministic code.

Extract the following fields and call the `extract_meeting_request` tool:

meeting_type: A short label for the kind of meeting (e.g. "investor_sync", "pilot_review", "standup", "1:1").

duration_minutes: Integer minutes. If not stated, return null (code will use the organiser's saved default).

participants: A list of people mentioned. For each person extract:
  - name (required)
  - email (only if explicitly stated, else null)
  - timezone (IANA name if explicitly stated or strongly implied by their location, else null; e.g. "Europe/London" for London, "Asia/Singapore" for Singapore, "Europe/Paris" for Paris/France)
  - role ("fellow", "partner", "team", "external") — infer from context if possible, else null

preferred_day: The weekday name as a plain string e.g. "Monday", "Tuesday". If a date is stated return the weekday name. If none stated return null.

relative_week: "this" if the person says "this week", "next" if they say "next week", null if unclear or not stated.

preferred_time_range: One of "morning" (08:00–12:00 Doha), "afternoon" (12:00–17:00 Doha), "evening" (17:00–20:00 Doha), "any" if not stated.

recurring: true only if the person explicitly requests a recurring/repeating meeting.

notes: Any important context to include in the invite (purpose, agenda items, etc.). Keep it brief.

Important rules:
- Do NOT invent emails or timezones not stated in the request.
- If a person is described by a team name ("Radical Asia team"), use that as the name.
- Always prefer returning null over guessing.
- You must call the tool — do not reply in plain text."""

_TOOL = {
    "name": "extract_meeting_request",
    "description": "Extract structured scheduling intent from a natural-language request.",
    "input_schema": {
        "type": "object",
        "required": ["meeting_type", "participants", "preferred_time_range", "recurring"],
        "properties": {
            "meeting_type": {"type": "string"},
            "duration_minutes": {"type": ["integer", "null"]},
            "participants": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name":     {"type": "string"},
                        "email":    {"type": ["string", "null"]},
                        "timezone": {"type": ["string", "null"]},
                        "role":     {
                            "type": ["string", "null"],
                            "enum": ["fellow", "partner", "team", "external", None],
                        },
                    },
                },
            },
            "preferred_day":        {"type": ["string", "null"]},
            "relative_week":        {"type": ["string", "null"], "enum": ["this", "next", None]},
            "preferred_time_range": {
                "type": "string",
                "enum": ["morning", "afternoon", "evening", "any"],
            },
            "explicit_time": {
                "type": ["string", "null"],
                "description": "Exact time in 24h HH:MM when the user names a specific time like '3pm', 'at 14:30', 'noon'. Null if not stated.",
            },
            "recurring": {"type": "boolean"},
            "notes":     {"type": ["string", "null"]},
        },
    },
}


def parse_request(
    user_text: str,
    slack_id: str | None = None,
    context: "MeetingRequest | None" = None,
) -> MeetingRequest:
    """
    Parse a natural-language scheduling request into a MeetingRequest.
    If context is provided (an existing proposal), treat user_text as a follow-up
    refinement and merge changes into the existing request.
    Looks up unknown participants in the people cache and upserts newly named ones.
    """
    if context:
        participant_summary = ", ".join(
            f"{p.name} ({p.email or 'no email'}, {p.timezone or 'no tz'}, {p.role or 'unknown role'})"
            for p in context.participants
        ) or "none"
        user_content = (
            f"Current meeting proposal to refine:\n"
            f"- Type: {context.meeting_type}\n"
            f"- Duration: {context.duration_minutes or 'default'} minutes\n"
            f"- Participants: {participant_summary}\n"
            f"- Day: {context.preferred_day or 'not specified'}"
            + (f" ({context.relative_week} week)" if context.relative_week else "") + "\n"
            f"- Time preference: {context.preferred_time_range}\n"
            f"- Notes: {context.notes or 'none'}\n\n"
            f"User follow-up: \"{user_text}\"\n\n"
            "Call extract_meeting_request with the complete updated request. "
            "Keep all existing fields exactly unless the user explicitly changes them."
        )
    else:
        user_content = user_text

    response = _client.messages.create(
        model=_MODEL,
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # prompt caching
            }
        ],
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "extract_meeting_request"},
        messages=[{"role": "user", "content": user_content}],
    )

    tool_use = next(b for b in response.content if b.type == "tool_use")
    raw: dict = tool_use.input

    participants: list[Participant] = []
    for p in raw.get("participants", []):
        participant = Participant(
            name=p["name"],
            email=p.get("email"),
            timezone=p.get("timezone"),
            role=p.get("role"),
        )
        # Enrich from cache if any fields are missing
        cached = store.find_person(p["name"])
        if cached:
            if not participant.email:
                participant.email = cached["email"]
            if not participant.timezone:
                participant.timezone = cached["timezone"]
            if not participant.role:
                participant.role = cached["role"]

        # Persist / update the people cache
        store.upsert_person(
            name=participant.name,
            email=participant.email,
            timezone=participant.timezone,
            role=participant.role,
        )
        participants.append(participant)

    return MeetingRequest(
        meeting_type=raw.get("meeting_type", "meeting"),
        duration_minutes=raw.get("duration_minutes"),
        participants=participants,
        preferred_day=raw.get("preferred_day"),
        relative_week=raw.get("relative_week"),
        preferred_time_range=raw.get("preferred_time_range", "any"),
        explicit_time=raw.get("explicit_time"),
        recurring=raw.get("recurring", False),
        notes=raw.get("notes"),
    )
