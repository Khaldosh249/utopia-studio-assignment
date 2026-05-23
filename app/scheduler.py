"""
Deterministic scheduling engine.
- Generates candidate slots in a time window (30-min step).
- Gathers busy intervals from studio config + Google FreeBusy (for connected users).
- Filters by conflicts and tz working-hour overlap.
- Scores and ranks candidates, returning best slot + alternatives + reasoning.
"""
from __future__ import annotations

import yaml
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.schemas import BusyInterval, MeetingRequest, ProposedSlot, ScheduleResult
from app.timezones import (
    DOHA_TZ,
    in_working_hours,
    intervals_overlap,
    local_time_label,
    resolve_window,
)

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "studio.yaml"


def _load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


STEP = timedelta(minutes=30)
MAX_ALTERNATIVES = 3


def schedule(
    request: MeetingRequest,
    organizer_slack_id: str,
    gcal_busy: list[BusyInterval] | None = None,
    prefs: dict | None = None,
) -> ScheduleResult:
    """
    Main entry point. Returns a ScheduleResult with best_slot and alternatives.

    gcal_busy: pre-fetched FreeBusy intervals for connected users (may be None/empty).
    prefs:     organiser's saved preferences dict from store.get_preferences().
    """
    config = _load_config()
    prefs = prefs or {}
    duration = timedelta(minutes=_resolve_duration(request, prefs))
    buffer_mins = prefs.get("buffer_minutes", config.get("default_buffer_minutes", 0))

    # 1. Resolve the date window
    window_start, window_end = resolve_window(
        preferred_day=request.preferred_day,
        relative_week=request.relative_week,
        time_range=request.preferred_time_range,
    )
    # Respect no-meeting days preference
    no_mtg = prefs.get("no_meeting_days", [])
    if window_start.weekday() in no_mtg:
        return ScheduleResult(
            status="no_slot",
            warnings=[
                f"You've blocked {window_start.strftime('%A')} as a no-meeting day. "
                "Try another day or update your /prefs."
            ],
        )

    # 2. Collect busy intervals
    busy: list[BusyInterval] = list(gcal_busy or [])
    busy += _config_busy_intervals(config, window_start.date(), buffer_mins)

    # 2a. Build human-readable conflict summary for the requested window
    conflict_details = _conflict_details_in_window(window_start, window_end, busy)

    # 2b. If the user named a specific time, check whether it is free
    explicit_time_note = _check_explicit_time(
        request.explicit_time, window_start, duration, busy
    )

    # 3. Generate + filter candidates
    candidates: list[datetime] = []
    slot_start = window_start
    while slot_start + duration <= window_end:
        slot_end = slot_start + duration
        if not _has_conflict(slot_start, slot_end, busy, buffer_mins):
            if _all_in_working_hours(slot_start, slot_end, request, config):
                candidates.append(slot_start)
        slot_start += STEP

    if not candidates:
        no_slot_warnings = [
            f"No free slot found in {request.preferred_time_range} on "
            f"{window_start.strftime('%A %d %b')} (Doha time). "
            "Try a different day or time range."
        ]
        if conflict_details:
            no_slot_warnings.append(
                "*Conflicts blocking that window:*\n" + "\n".join(f"• {c}" for c in conflict_details)
            )
        return ScheduleResult(
            status="no_slot",
            warnings=no_slot_warnings,
            conflict_details=conflict_details,
            explicit_time_note=explicit_time_note,
        )

    # 4. Score candidates
    scored = sorted(candidates, key=lambda s: _score(s, duration, request, prefs, config, busy))

    # 5. Determine if we should auto-propose or show a picker
    #    Show picker when the user named a specific time and that time is blocked.
    show_picker = bool(
        request.explicit_time
        and explicit_time_note
        and "conflicts" in explicit_time_note
    )

    warnings: list[str] = _collect_warnings(request, config)

    if show_picker:
        # Don't auto-pick — hand all candidates back as picker options (up to 8)
        MAX_PICKER = 8
        all_slots = [_build_slot(s, duration, request, config, busy) for s in scored[:MAX_PICKER]]
        return ScheduleResult(
            status="success",
            best_slot=None,          # no auto-selection; user will pick
            alternatives=all_slots,
            warnings=warnings,
            conflict_details=conflict_details,
            explicit_time_note=explicit_time_note,
            show_picker=True,
        )

    # Normal flow — auto-propose the best slot
    slots = [_build_slot(s, duration, request, config, busy) for s in scored[:1 + MAX_ALTERNATIVES]]
    return ScheduleResult(
        status="success",
        best_slot=slots[0],
        alternatives=slots[1:],
        warnings=warnings,
        conflict_details=conflict_details,
        explicit_time_note=explicit_time_note,
        show_picker=False,
    )


def _resolve_duration(request: MeetingRequest, prefs: dict) -> int:
    return request.duration_minutes or prefs.get("default_duration_mins", 45)


def _config_busy_intervals(config: dict, day: "date", buffer_mins: int) -> list[BusyInterval]:
    """Expand recurring studio meetings into BusyIntervals for `day`."""
    intervals: list[BusyInterval] = []
    day_idx = day.weekday()

    for mtg in config.get("recurring_meetings", []):
        mtg_day = mtg.get("day")
        if mtg_day is not None and mtg_day != day_idx:
            continue  # wrong weekday
        if mtg_day is None and day_idx >= 5:
            continue  # "daily" means Mon–Fri only

        h, m = map(int, mtg["start"].split(":"))
        start = datetime(day.year, day.month, day.day, h, m, tzinfo=DOHA_TZ)
        end = start + timedelta(minutes=mtg["duration_minutes"])
        intervals.append(BusyInterval(
            start=start - timedelta(minutes=buffer_mins),
            end=end + timedelta(minutes=buffer_mins),
            source="studio_config",
            label=mtg["name"],
        ))
    return intervals


def _has_conflict(
    slot_start: datetime,
    slot_end: datetime,
    busy: list[BusyInterval],
    buffer_mins: int,
) -> bool:
    return any(
        intervals_overlap(slot_start, slot_end, b.start, b.end)
        for b in busy
    )


def _all_in_working_hours(
    slot_start: datetime,
    slot_end: datetime,
    request: MeetingRequest,
    config: dict,
) -> bool:
    """Check working-hours overlap for all participants that have a timezone."""
    for p in request.participants:
        if not p.timezone:
            continue

        role = p.role or "external"

        # Fellows: only Mon–Wed
        if role == "fellow":
            allowed_days = config["role_rules"]["fellow"]["available_days"]
            if slot_start.weekday() not in allowed_days:
                return False
            ws = config["role_rules"]["fellow"]["work_start"]
            we = config["role_rules"]["fellow"]["work_end"]
            tz = config["role_rules"]["fellow"]["timezone"]
        else:
            # Use their explicit timezone; find matching partner band for work hours
            ws, we = _work_hours_for_tz(p.timezone, config)
            tz = p.timezone

        if not in_working_hours(slot_start, slot_end, tz, ws, we):
            return False

    return True


def _work_hours_for_tz(iana_tz: str, config: dict) -> tuple[str, str]:
    """Return (work_start, work_end) for a given IANA tz, using partner bands as a lookup."""
    for band in config.get("partner_tz_bands", []):
        if band["iana"] == iana_tz:
            return band["work_start"], band["work_end"]
    return config.get("default_work_start", "08:00"), config.get("default_work_end", "20:00")


def _score(
    slot_start: datetime,
    duration: timedelta,
    request: MeetingRequest,
    prefs: dict,
    config: dict,
    busy: list[BusyInterval],
) -> float:
    """Lower score = better. Primary: conflicts (hard); secondary: preferences."""
    score = 0.0

    # Prefer user's saved window
    pw_start = prefs.get("pref_window_start")
    pw_end = prefs.get("pref_window_end")
    if pw_start and pw_end:
        from datetime import time as dtime
        h1, m1 = map(int, pw_start.split(":"))
        h2, m2 = map(int, pw_end.split(":"))
        if not (dtime(h1, m1) <= slot_start.time() <= dtime(h2, m2)):
            score += 2.0

    # Batching vs. spread
    batching = prefs.get("batching_style", "none")
    if batching in ("batch", "spread"):
        proximity = _minutes_to_nearest_busy(slot_start, duration, busy)
        if batching == "batch":
            score += proximity / 60.0  # closer = better (lower score)
        else:
            score -= proximity / 60.0  # farther = better

    # Earlier in the window = slightly preferred as tiebreak
    score += slot_start.hour / 24.0

    return score


def _minutes_to_nearest_busy(slot_start: datetime, duration: timedelta, busy: list[BusyInterval]) -> float:
    slot_end = slot_start + duration
    min_dist = float("inf")
    for b in busy:
        dist = min(
            abs((slot_start - b.end).total_seconds()),
            abs((b.start - slot_end).total_seconds()),
        )
        min_dist = min(min_dist, dist)
    return min_dist / 60.0 if min_dist != float("inf") else 999.0


def _build_slot(
    slot_start: datetime,
    duration: timedelta,
    request: MeetingRequest,
    config: dict,
    busy: list[BusyInterval],
) -> ProposedSlot:
    slot_end = slot_start + duration

    # Local times for each participant
    local_times: dict[str, str] = {}
    for p in request.participants:
        tz = p.timezone or config["studio_timezone"]
        local_times[p.name] = local_time_label(slot_start, tz)

    # Reasoning facts
    reasoning: list[str] = []
    conflicts_avoided: list[str] = []

    # All recurring studio meetings today — list as context + check proximity
    all_day_busy = _config_busy_intervals(config, slot_start.date(), 0)
    for b in all_day_busy:
        label = b.label or b.source
        conflicts_avoided.append(label)

        # Proximity warning: < 30 min gap between this slot and the meeting
        gap_before = (b.start - slot_end).total_seconds() / 60    # mins until b starts after us
        gap_after  = (slot_start - b.end).total_seconds() / 60    # mins since b ended before us

        if 0 < gap_before <= 30:
            reasoning.append(
                f"⚠️ Only {int(gap_before)} min before *{label}* at {b.start.strftime('%H:%M')}"
            )
        elif 0 < gap_after <= 30:
            reasoning.append(
                f"⚠️ Only {int(gap_after)} min after *{label}* ending {b.end.strftime('%H:%M')}"
            )

    # Also check proximity against real calendar busy intervals passed in
    for b in busy:
        if b.source != "studio_config":
            gap_before = (b.start - slot_end).total_seconds() / 60
            gap_after  = (slot_start - b.end).total_seconds() / 60
            label = b.label or "existing event"
            if 0 < gap_before <= 30:
                reasoning.append(f"⚠️ Only {int(gap_before)} min before {label} at {b.start.strftime('%H:%M')}")
            elif 0 < gap_after <= 30:
                reasoning.append(f"⚠️ Only {int(gap_after)} min after {label} ending {b.end.strftime('%H:%M')}")

    # Fellow availability note
    for p in request.participants:
        if p.role == "fellow":
            reasoning.append(f"Within {p.name}'s Mon–Wed availability")
        if p.timezone:
            reasoning.append(f"{slot_start.strftime('%H:%M')} Doha = {local_time_label(slot_start, p.timezone)} for {p.name}")

    return ProposedSlot(
        start_doha=slot_start,
        end_doha=slot_end,
        participant_local_times=local_times,
        reasoning=reasoning,
        conflicts_avoided=conflicts_avoided,
    )


def _conflict_details_in_window(
    window_start: datetime,
    window_end: datetime,
    busy: list[BusyInterval],
) -> list[str]:
    """Return human-readable descriptions of busy intervals that overlap the window."""
    details = []
    for b in busy:
        if intervals_overlap(window_start, window_end, b.start, b.end):
            label = b.label or b.source
            details.append(f"{b.start.strftime('%H:%M')}–{b.end.strftime('%H:%M')} *{label}*")
    return details


def _check_explicit_time(
    explicit_time: str | None,
    window_start: datetime,
    duration: timedelta,
    busy: list[BusyInterval],
) -> str | None:
    """
    If the user named a specific time, return a note explaining whether it's free or blocked.
    Returns None if no explicit time was requested.
    """
    if not explicit_time:
        return None
    try:
        h, m = map(int, explicit_time.split(":"))
        req_start = window_start.replace(hour=h, minute=m, second=0, microsecond=0)
        req_end = req_start + duration
    except (ValueError, AttributeError):
        return None

    blockers = [b for b in busy if intervals_overlap(req_start, req_end, b.start, b.end)]
    if not blockers:
        return f"✅ Your requested time *{explicit_time}* is free"

    b = blockers[0]
    label = b.label or b.source
    return (
        f"⚠️ *{explicit_time}* conflicts with *{label}* "
        f"({b.start.strftime('%H:%M')}–{b.end.strftime('%H:%M')})"
    )


def _collect_warnings(request: MeetingRequest, config: dict) -> list[str]:
    warnings: list[str] = []
    for p in request.participants:
        if p.role == "fellow" and request.preferred_day:
            day_idx = {
                "monday": 0, "tuesday": 1, "wednesday": 2,
                "thursday": 3, "friday": 4,
            }.get(request.preferred_day.lower(), -1)
            allowed = config["role_rules"]["fellow"]["available_days"]
            if day_idx not in allowed:
                warnings.append(
                    f"⚠️ {p.name} (fellow) is only available Mon–Wed. "
                    f"{request.preferred_day} is outside their window."
                )
        if not p.email:
            warnings.append(f"⚠️ No email on file for {p.name} — they won't receive a calendar invite.")
    return warnings
