"""SQLite persistence — users, preferences, people cache, meetings log."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "utopia.sqlite"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


@contextmanager
def get_db():
    c = _conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_db() -> None:
    with get_db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                slack_id      TEXT PRIMARY KEY,
                name          TEXT,
                email         TEXT,
                home_tz       TEXT DEFAULT 'Asia/Qatar',
                google_refresh_token TEXT,
                created_at    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS preferences (
                slack_id              TEXT PRIMARY KEY,
                buffer_minutes        INTEGER DEFAULT 15,
                batching_style        TEXT DEFAULT 'none',
                pref_window_start     TEXT,
                pref_window_end       TEXT,
                default_duration_mins INTEGER DEFAULT 45,
                no_meeting_days       TEXT DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS people (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                email      TEXT,
                timezone   TEXT,
                role       TEXT,
                last_seen  TEXT DEFAULT (datetime('now')),
                UNIQUE(name)
            );

            CREATE TABLE IF NOT EXISTS meetings_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                organizer_id    TEXT,
                payload_json    TEXT,
                status          TEXT DEFAULT 'proposed',
                created_at      TEXT DEFAULT (datetime('now'))
            );
        """)


# ── users ─────────────────────────────────────────────────────────────────────

def upsert_user(slack_id: str, name: str = "", email: str = "", home_tz: str = "Asia/Qatar") -> None:
    with get_db() as c:
        c.execute("""
            INSERT INTO users (slack_id, name, email, home_tz)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(slack_id) DO UPDATE SET
                name  = COALESCE(excluded.name,  name),
                email = COALESCE(NULLIF(excluded.email, ''), email),
                home_tz = COALESCE(NULLIF(excluded.home_tz, ''), home_tz)
        """, (slack_id, name, email, home_tz))


def get_user(slack_id: str) -> sqlite3.Row | None:
    with get_db() as c:
        return c.execute("SELECT * FROM users WHERE slack_id = ?", (slack_id,)).fetchone()


def save_google_token(slack_id: str, refresh_token: str) -> None:
    with get_db() as c:
        c.execute("""
            INSERT INTO users (slack_id, google_refresh_token)
            VALUES (?, ?)
            ON CONFLICT(slack_id) DO UPDATE SET google_refresh_token = excluded.google_refresh_token
        """, (slack_id, refresh_token))


def get_google_token(slack_id: str) -> str | None:
    user = get_user(slack_id)
    return user["google_refresh_token"] if user else None


# ── preferences ───────────────────────────────────────────────────────────────

def get_preferences(slack_id: str) -> dict:
    with get_db() as c:
        row = c.execute("SELECT * FROM preferences WHERE slack_id = ?", (slack_id,)).fetchone()
    if row is None:
        return {
            "slack_id": slack_id,
            "buffer_minutes": 15,
            "batching_style": "none",
            "pref_window_start": None,
            "pref_window_end": None,
            "default_duration_mins": 45,
            "no_meeting_days": [],
        }
    d = dict(row)
    d["no_meeting_days"] = json.loads(d.get("no_meeting_days") or "[]")
    return d


def save_preferences(slack_id: str, prefs: dict) -> None:
    no_mtg = json.dumps(prefs.get("no_meeting_days", []))
    with get_db() as c:
        c.execute("""
            INSERT INTO preferences
                (slack_id, buffer_minutes, batching_style, pref_window_start,
                 pref_window_end, default_duration_mins, no_meeting_days)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slack_id) DO UPDATE SET
                buffer_minutes        = excluded.buffer_minutes,
                batching_style        = excluded.batching_style,
                pref_window_start     = excluded.pref_window_start,
                pref_window_end       = excluded.pref_window_end,
                default_duration_mins = excluded.default_duration_mins,
                no_meeting_days       = excluded.no_meeting_days
        """, (
            slack_id,
            prefs.get("buffer_minutes", 15),
            prefs.get("batching_style", "none"),
            prefs.get("pref_window_start"),
            prefs.get("pref_window_end"),
            prefs.get("default_duration_mins", 45),
            no_mtg,
        ))


# ── people cache ──────────────────────────────────────────────────────────────

def upsert_person(name: str, email: str | None = None, timezone: str | None = None,
                  role: str | None = None) -> None:
    with get_db() as c:
        c.execute("""
            INSERT INTO people (name, email, timezone, role, last_seen)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(name) DO UPDATE SET
                email    = COALESCE(excluded.email,    email),
                timezone = COALESCE(excluded.timezone, timezone),
                role     = COALESCE(excluded.role,     role),
                last_seen = excluded.last_seen
        """, (name, email, timezone, role))


def find_person(name: str) -> sqlite3.Row | None:
    with get_db() as c:
        return c.execute(
            "SELECT * FROM people WHERE lower(name) LIKE lower(?)", (f"%{name}%",)
        ).fetchone()


def all_people() -> list[sqlite3.Row]:
    with get_db() as c:
        return c.execute("SELECT * FROM people ORDER BY last_seen DESC").fetchall()


# ── meetings log ──────────────────────────────────────────────────────────────

def log_meeting(organizer_id: str, payload: dict, status: str = "proposed") -> int:
    with get_db() as c:
        cur = c.execute("""
            INSERT INTO meetings_log (organizer_id, payload_json, status)
            VALUES (?, ?, ?)
        """, (organizer_id, json.dumps(payload), status))
        return cur.lastrowid


def update_meeting_status(meeting_id: int, status: str) -> None:
    with get_db() as c:
        c.execute("UPDATE meetings_log SET status = ? WHERE id = ?", (status, meeting_id))
