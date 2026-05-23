"""
Deterministic timezone helpers — all time math lives here.
The LLM only extracts intent; this module does the arithmetic.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DOHA_TZ = ZoneInfo("Asia/Qatar")

_TIME_RANGE_BOUNDS: dict[str, tuple[time, time]] = {
    "morning":   (time(8, 0),  time(12, 0)),
    "afternoon": (time(12, 0), time(17, 0)),
    "evening":   (time(17, 0), time(20, 0)),
    "any":       (time(8, 0),  time(20, 0)),
}

_WEEKDAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2,
    "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}


def now_doha() -> datetime:
    return datetime.now(DOHA_TZ)


def to_doha(dt: datetime) -> datetime:
    """Convert any aware datetime to Asia/Qatar."""
    return dt.astimezone(DOHA_TZ)


def from_doha(dt: datetime, tz: str) -> datetime:
    """Convert a Doha-timezone datetime to any IANA tz string."""
    return dt.astimezone(ZoneInfo(tz))


def local_time_label(slot_doha: datetime, tz: str) -> str:
    """Return a human-readable local time label, e.g. '15:00 BST (Tue)'."""
    local = from_doha(slot_doha, tz)
    abbr = local.strftime("%Z")
    day = local.strftime("%a")
    return f"{local.strftime('%H:%M')} {abbr} ({day})"


def resolve_window(
    preferred_day: str | None,
    relative_week: str | None,
    time_range: str,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """
    Resolve a natural-language day + relative week + time-range band into a
    concrete (window_start, window_end) in Doha time.

    Rules:
    - If preferred_day is None, defaults to tomorrow.
    - relative_week "next" means the NEXT calendar week's occurrence of that day.
    - All output is tz-aware in Asia/Qatar.
    """
    ref = now or now_doha()
    ref_date = ref.date()

    band_start, band_end = _TIME_RANGE_BOUNDS.get(time_range, _TIME_RANGE_BOUNDS["any"])

    if preferred_day is None:
        target_date = ref_date + timedelta(days=1)
    else:
        day_idx = _WEEKDAY_MAP.get(preferred_day.lower().strip())
        if day_idx is None:
            # fallback: tomorrow
            target_date = ref_date + timedelta(days=1)
        else:
            target_date = _next_weekday(ref_date, day_idx, relative_week == "next")

    window_start = datetime.combine(target_date, band_start, tzinfo=DOHA_TZ)
    window_end = datetime.combine(target_date, band_end, tzinfo=DOHA_TZ)
    return window_start, window_end


def _next_weekday(ref: date, weekday: int, force_next_week: bool) -> date:
    """
    Return the nearest future date matching `weekday`.
    If force_next_week is True, skip ahead to next calendar week's Monday first.
    """
    if force_next_week:
        # advance to next Monday
        days_to_next_mon = (7 - ref.weekday()) % 7
        if days_to_next_mon == 0:
            days_to_next_mon = 7
        ref = ref + timedelta(days=days_to_next_mon)
        # now find `weekday` from that Monday
        return ref + timedelta(days=(weekday - ref.weekday()) % 7)
    else:
        days_ahead = (weekday - ref.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7  # "Tuesday" when today is Tuesday means next Tuesday
        return ref + timedelta(days=days_ahead)


def in_working_hours(
    slot_start_doha: datetime,
    slot_end_doha: datetime,
    person_tz: str,
    work_start: str = "09:00",
    work_end: str = "18:00",
) -> bool:
    """
    Return True if the entire slot falls within the person's working hours
    (expressed in their local timezone).
    """
    tz = ZoneInfo(person_tz)
    local_start = slot_start_doha.astimezone(tz)
    local_end = slot_end_doha.astimezone(tz)

    ws = time(*map(int, work_start.split(":")))
    we = time(*map(int, work_end.split(":")))

    return local_start.time() >= ws and local_end.time() <= we


def intervals_overlap(
    a_start: datetime, a_end: datetime,
    b_start: datetime, b_end: datetime,
    buffer_minutes: int = 0,
) -> bool:
    """Return True if [a_start, a_end) overlaps [b_start-buf, b_end+buf)."""
    buf = timedelta(minutes=buffer_minutes)
    return a_start < b_end + buf and a_end > b_start - buf
