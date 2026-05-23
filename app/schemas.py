from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Participant(BaseModel):
    name: str
    email: str | None = None
    timezone: str | None = None  # IANA tz name e.g. "Europe/London"
    role: Literal["fellow", "partner", "team", "external"] | None = None


class MeetingRequest(BaseModel):
    meeting_type: str = Field(description="e.g. investor_sync, standup, pilot_review")
    duration_minutes: int | None = Field(None, description="None means use organiser default")
    participants: list[Participant] = Field(default_factory=list)
    preferred_day: str | None = Field(None, description="Raw phrase e.g. 'Tuesday', 'Monday'")
    relative_week: Literal["this", "next"] | None = None
    preferred_time_range: Literal["morning", "afternoon", "evening", "any"] = "any"
    explicit_time: str | None = Field(
        None,
        description="Specific time requested in 24h HH:MM format, e.g. '15:00' for '3pm'. "
                    "Only set when the user names an exact time.",
    )
    recurring: bool = False
    notes: str | None = None


class BusyInterval(BaseModel):
    start: datetime
    end: datetime
    source: str  # "google_calendar" | "studio_config" | "recurring"
    label: str | None = None


class ProposedSlot(BaseModel):
    start_doha: datetime
    end_doha: datetime
    participant_local_times: dict[str, str] = Field(
        default_factory=dict,
        description="name -> 'HH:MM LocalTZ (Day)'",
    )
    reasoning: list[str] = Field(default_factory=list)
    conflicts_avoided: list[str] = Field(default_factory=list)


class ScheduleResult(BaseModel):
    status: Literal["success", "no_slot", "partial"]
    best_slot: ProposedSlot | None = None
    alternatives: list[ProposedSlot] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    invite_draft: str | None = None
    # Conflict / overlap details shown to the user
    conflict_details: list[str] = Field(
        default_factory=list,
        description="Busy intervals found in the requested window, e.g. '13:00–13:30 blocked by Studio Stand-up'",
    )
    explicit_time_note: str | None = Field(
        None,
        description="Explanation when the user asked for a specific time, e.g. '15:00 conflicts with X'",
    )
    # When True, the requested explicit_time was blocked — show a picker instead of auto-proposing
    show_picker: bool = False


class UserPreferences(BaseModel):
    slack_id: str
    buffer_minutes: int = 15
    batching_style: Literal["batch", "spread", "none"] = "none"
    pref_window_start: str | None = None  # "HH:MM" Doha
    pref_window_end: str | None = None    # "HH:MM" Doha
    default_duration_minutes: int = 45
    no_meeting_days: list[int] = Field(default_factory=list)  # weekday indices 0=Mon
