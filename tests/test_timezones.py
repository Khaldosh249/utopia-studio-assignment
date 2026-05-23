"""
Unit tests for the deterministic timezone and scheduling modules.
No external API calls — fully offline.
"""
from __future__ import annotations

import pytest
from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.timezones import (
    DOHA_TZ,
    from_doha,
    in_working_hours,
    intervals_overlap,
    local_time_label,
    resolve_window,
    to_doha,
)


# ── Timezone conversion ────────────────────────────────────────────────────────

def test_doha_is_utc_plus_3():
    dt = datetime(2024, 6, 15, 12, 0, tzinfo=DOHA_TZ)
    utc = dt.astimezone(ZoneInfo("UTC"))
    assert utc.hour == 9  # 12:00 Doha = 09:00 UTC


def test_london_dst_summer():
    """Europe/London is UTC+1 in summer (BST)."""
    slot_doha = datetime(2024, 7, 15, 15, 0, tzinfo=DOHA_TZ)  # 15:00 Doha = 13:00 BST
    local = from_doha(slot_doha, "Europe/London")
    assert local.hour == 13
    assert local.strftime("%Z") == "BST"


def test_london_dst_winter():
    """Europe/London is UTC+0 in winter (GMT)."""
    slot_doha = datetime(2024, 1, 15, 15, 0, tzinfo=DOHA_TZ)  # 15:00 Doha = 12:00 GMT
    local = from_doha(slot_doha, "Europe/London")
    assert local.hour == 12
    assert local.strftime("%Z") == "GMT"


def test_singapore_no_dst():
    """Asia/Singapore is permanently UTC+8."""
    slot_doha = datetime(2024, 7, 15, 10, 0, tzinfo=DOHA_TZ)  # 10:00 Doha = 15:00 SGT
    local = from_doha(slot_doha, "Asia/Singapore")
    assert local.hour == 15


def test_doha_no_dst():
    """Asia/Qatar has no DST — always UTC+3."""
    summer = datetime(2024, 7, 1, 12, 0, tzinfo=DOHA_TZ)
    winter = datetime(2024, 1, 1, 12, 0, tzinfo=DOHA_TZ)
    assert to_doha(summer.astimezone(ZoneInfo("UTC"))).hour == 12
    assert to_doha(winter.astimezone(ZoneInfo("UTC"))).hour == 12


def test_local_time_label_includes_tz_abbr():
    slot_doha = datetime(2024, 1, 15, 14, 0, tzinfo=DOHA_TZ)
    label = local_time_label(slot_doha, "Europe/London")
    assert "GMT" in label
    assert "14:00" not in label  # label should show London time, not Doha
    assert "11:00" in label  # 14:00 Doha = 11:00 GMT (UTC+3 - UTC+0)


# ── resolve_window ─────────────────────────────────────────────────────────────

def _monday_ref() -> datetime:
    """Return a fixed Monday morning in Doha for reproducible tests."""
    return datetime(2024, 5, 6, 9, 0, tzinfo=DOHA_TZ)  # Monday 6 May 2024


def test_resolve_this_tuesday_afternoon():
    start, end = resolve_window("Tuesday", "this", "afternoon", now=_monday_ref())
    assert start.weekday() == 1  # Tuesday
    assert start.hour == 12
    assert end.hour == 17
    assert start.date() == _monday_ref().date().replace(day=7)


def test_resolve_next_tuesday_afternoon():
    start, end = resolve_window("Tuesday", "next", "afternoon", now=_monday_ref())
    assert start.weekday() == 1  # Tuesday
    # "next" from Monday 6 May → next week's Tuesday = 14 May
    assert start.day == 14


def test_resolve_next_monday_from_monday():
    """'Next Monday' from a Monday = 7 days ahead."""
    start, end = resolve_window("Monday", "next", "morning", now=_monday_ref())
    assert start.weekday() == 0
    assert start.day == 13  # 6 May + 7 = 13 May


def test_resolve_morning_band():
    start, end = resolve_window("Wednesday", "this", "morning", now=_monday_ref())
    assert start.hour == 8
    assert end.hour == 12


def test_resolve_no_day_defaults_to_tomorrow():
    start, end = resolve_window(None, None, "any", now=_monday_ref())
    # tomorrow from Monday = Tuesday
    assert start.weekday() == 1


def test_resolve_same_weekday_bumps_to_next_week():
    """Requesting 'Monday' when today IS Monday should give next Monday."""
    start, end = resolve_window("Monday", None, "any", now=_monday_ref())
    assert start.day == 13  # next Monday


# ── in_working_hours ───────────────────────────────────────────────────────────

def test_slot_inside_working_hours():
    slot_start = datetime(2024, 6, 15, 14, 0, tzinfo=DOHA_TZ)  # 14:00 Doha = 11:00 GMT
    slot_end   = datetime(2024, 6, 15, 14, 30, tzinfo=DOHA_TZ)
    assert in_working_hours(slot_start, slot_end, "Europe/London", "09:00", "18:00")


def test_slot_outside_working_hours_too_early():
    # 08:00 Doha = 05:00 London (summer) — before 09:00 London
    slot_start = datetime(2024, 6, 15, 8, 0, tzinfo=DOHA_TZ)
    slot_end   = datetime(2024, 6, 15, 8, 30, tzinfo=DOHA_TZ)
    assert not in_working_hours(slot_start, slot_end, "Europe/London", "09:00", "18:00")


def test_slot_outside_working_hours_too_late():
    # 20:00 Doha = 17:00 London (summer) = exactly end — should fail (end exclusive)
    slot_start = datetime(2024, 6, 15, 20, 0, tzinfo=DOHA_TZ)
    slot_end   = datetime(2024, 6, 15, 20, 30, tzinfo=DOHA_TZ)
    assert not in_working_hours(slot_start, slot_end, "Europe/London", "09:00", "18:00")


# ── intervals_overlap ──────────────────────────────────────────────────────────

def _dt(h: int, m: int = 0) -> datetime:
    return datetime(2024, 6, 15, h, m, tzinfo=DOHA_TZ)


def test_no_overlap():
    assert not intervals_overlap(_dt(9), _dt(10), _dt(11), _dt(12))


def test_overlap_touching():
    assert not intervals_overlap(_dt(9), _dt(10), _dt(10), _dt(11))


def test_overlap_partial():
    assert intervals_overlap(_dt(9), _dt(11), _dt(10), _dt(12))


def test_overlap_contained():
    assert intervals_overlap(_dt(9), _dt(13), _dt(10), _dt(12))


def test_overlap_with_buffer():
    # [9:00–10:00] and [10:10–11:00] don't overlap, but with 15-min buffer they do
    assert intervals_overlap(_dt(9), _dt(10), _dt(10, 10), _dt(11), buffer_minutes=15)


# ── Config-based conflict detection ───────────────────────────────────────────

def test_qdb_weekly_conflict():
    """A slot at 14:00 Monday should conflict with QDB Weekly."""
    from app.scheduler import _config_busy_intervals
    import yaml
    from pathlib import Path
    config = yaml.safe_load(open(Path(__file__).parent.parent / "config" / "studio.yaml"))
    monday = datetime(2024, 5, 6, tzinfo=DOHA_TZ).date()
    busy = _config_busy_intervals(config, monday, buffer_mins=0)

    qdb_labels = [b.label for b in busy]
    assert "QDB Weekly" in qdb_labels

    qdb = next(b for b in busy if b.label == "QDB Weekly")
    slot_start = datetime(2024, 5, 6, 14, 0, tzinfo=DOHA_TZ)
    slot_end   = datetime(2024, 5, 6, 14, 30, tzinfo=DOHA_TZ)
    from app.timezones import intervals_overlap
    assert intervals_overlap(slot_start, slot_end, qdb.start, qdb.end)


def test_standup_daily_conflict():
    """Standup (daily 13:00) should appear on every weekday."""
    from app.scheduler import _config_busy_intervals
    import yaml
    from pathlib import Path
    config = yaml.safe_load(open(Path(__file__).parent.parent / "config" / "studio.yaml"))
    tuesday = datetime(2024, 5, 7, tzinfo=DOHA_TZ).date()
    busy = _config_busy_intervals(config, tuesday, buffer_mins=0)
    labels = [b.label for b in busy]
    assert "Studio Stand-up" in labels


def test_standup_not_on_weekend():
    """Standup should NOT appear on Saturday."""
    from app.scheduler import _config_busy_intervals
    import yaml
    from pathlib import Path
    config = yaml.safe_load(open(Path(__file__).parent.parent / "config" / "studio.yaml"))
    saturday = datetime(2024, 5, 11, tzinfo=DOHA_TZ).date()
    busy = _config_busy_intervals(config, saturday, buffer_mins=0)
    labels = [b.label for b in busy]
    assert "Studio Stand-up" not in labels


def test_pods_not_on_tuesday():
    """PODS Pipeline is Wednesday-only — should not appear on Tuesday."""
    from app.scheduler import _config_busy_intervals
    import yaml
    from pathlib import Path
    config = yaml.safe_load(open(Path(__file__).parent.parent / "config" / "studio.yaml"))
    tuesday = datetime(2024, 5, 7, tzinfo=DOHA_TZ).date()
    busy = _config_busy_intervals(config, tuesday, buffer_mins=0)
    labels = [b.label for b in busy]
    assert "PODS Pipeline Review" not in labels
