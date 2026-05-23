"""
Invite draft composer.
Generates polished invite text from a ProposedSlot + MeetingRequest.
Uses a template for speed; optionally calls Claude for prose polish.
"""
from __future__ import annotations

import os
from textwrap import dedent

import anthropic
from dotenv import load_dotenv

from app.schemas import MeetingRequest, ProposedSlot

load_dotenv()

_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
_MODEL = "claude-sonnet-4-6"

_POLISH_SYSTEM = (
    "You are a professional executive assistant at Utopia Studio, Doha. "
    "Rewrite the provided meeting invite draft to be concise, warm, and professional. "
    "Keep all factual details (times, names, purpose) exactly as given. "
    "Output only the polished invite text — no preamble, no markdown, no asterisks, "
    "no bullet symbols, no headers. Plain text only."
)


def build_draft(
    slot: ProposedSlot,
    request: MeetingRequest,
    polish: bool = True,
) -> str:
    """
    Return a ready-to-send invite draft.
    If polish=True, Claude refines the template for better prose.
    """
    raw = _template_draft(slot, request)
    if not polish:
        return raw

    try:
        resp = _client.messages.create(
            model=_MODEL,
            max_tokens=512,
            system=_POLISH_SYSTEM,
            messages=[{"role": "user", "content": raw}],
        )
        return resp.content[0].text.strip()
    except Exception:
        return raw  # fall back to template if LLM fails


def _template_draft(slot: ProposedSlot, request: MeetingRequest) -> str:
    start = slot.start_doha
    end = slot.end_doha
    duration_min = int((end - start).total_seconds() // 60)

    participants = ", ".join(p.name for p in request.participants) if request.participants else "team"
    purpose = request.notes or _default_purpose(request.meeting_type)

    local_lines = "\n".join(
        f"  • {name}: {local_time}"
        for name, local_time in slot.participant_local_times.items()
    )
    if local_lines:
        local_section = f"\nLocal times:\n{local_lines}\n"
    else:
        local_section = ""

    return dedent(f"""
        Hi everyone,

        Proposing {start.strftime('%A, %d %B')} at {start.strftime('%H:%M')} Doha time (UTC+3) for a {duration_min}-minute {_humanise_type(request.meeting_type)} with {participants}.
        {local_section}
        Purpose:
        {purpose}

        Please confirm if this time works, or let me know your availability.

        Best,
        Khalid
    """).strip()


def _humanise_type(meeting_type: str) -> str:
    return meeting_type.replace("_", " ")


def _default_purpose(meeting_type: str) -> str:
    defaults = {
        "investor_sync": "Review progress, discuss milestones, and align on next steps.",
        "pilot_review":  "Review pilot findings and agree on next phase targets.",
        "standup":       "Daily check-in on blockers and priorities.",
        "1:1":           "Catch-up and alignment.",
    }
    return defaults.get(meeting_type, "Discussion and alignment.")
