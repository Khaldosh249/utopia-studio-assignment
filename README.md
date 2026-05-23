# Utopia Studio Scheduling Operator

An AI-powered Slack bot that takes messy natural-language meeting requests and proposes conflict-free time slots in Doha time (UTC+3), with Google Calendar integration and per-user scheduling preferences.

---

## How to run

### Prerequisites

- Python 3.11+
- A Slack app in **Socket Mode** with slash commands `/schedule` and `/settings` enabled
- Google Calendar OAuth 2.0 credentials (Desktop client)
- An Anthropic API key

### Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill in credentials
cp .env.example .env   # then edit .env

# 3. Run
./run.sh
# or directly:
/usr/bin/python3 main.py
```

### `.env` variables required

```
ANTHROPIC_API_KEY=...
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=http://localhost:8080/oauth/callback
```

### Google OAuth callback (for connecting calendars)

The bot starts a local FastAPI server on port 8080 for the OAuth redirect. For multi-user setups, expose it with ngrok:

```bash
ngrok http 8080
# then set GOOGLE_REDIRECT_URI to your ngrok URL + /oauth/callback
```

---

## Prompts used

### 1. Natural-language parser — `app/parser.py`

**Role:** Claude-as-structured-output. Extracts scheduling intent from free-form text; does no date math.

**System prompt:**
```
You are a scheduling assistant for Utopia Studio, a venture studio based in Doha, Qatar (UTC+3, Asia/Qatar, no DST).

Your ONLY job is to extract structured scheduling intent from a natural-language request. You do NOT calculate times, convert timezones, or resolve relative dates — that is handled by separate deterministic code.

Extract the following fields and call the `extract_meeting_request` tool:

meeting_type: A short label for the kind of meeting (e.g. "investor_sync", "pilot_review", "standup", "1:1").
duration_minutes: Integer minutes. If not stated, return null.
participants: A list of people mentioned. For each person extract name, email, timezone (IANA), and role (fellow/partner/team/external).
preferred_day: Weekday name e.g. "Monday". Null if not stated.
relative_week: "this" or "next". Null if unclear.
preferred_time_range: One of morning/afternoon/evening/any.
explicit_time: Exact time in 24h HH:MM when the user names a specific time. Null if not stated.
recurring: true only if explicitly requested.
notes: Brief context for the invite.

Important rules:
- Do NOT invent emails or timezones not stated in the request.
- Always prefer returning null over guessing.
- You must call the tool — do not reply in plain text.
```

**Model:** `claude-sonnet-4-6`
**Mode:** Tool use (`extract_meeting_request`), forced via `tool_choice`. Prompt caching applied to the system prompt (`cache_control: ephemeral`).

---

### 2. Follow-up refinement — `app/parser.py`

When the user DMs a refinement ("try 3pm", "make it 1 hour"), the existing proposal is prepended to the user message:

```
Current meeting proposal to refine:
- Type: investor_sync
- Duration: 45 minutes
- Participants: Ahmed (ahmed@x.com, Europe/London, partner)
- Day: Tuesday (next week)
- Time preference: afternoon
- Notes: none

User follow-up: "try 3pm"

Call extract_meeting_request with the complete updated request.
Keep all existing fields exactly unless the user explicitly changes them.
```

---

### 3. Invite draft polish — `app/invite.py`

**System prompt:**
```
You are a professional executive assistant at Utopia Studio, Doha.
Rewrite the provided meeting invite draft to be concise, warm, and professional.
Keep all factual details (times, names, purpose) exactly as given.
Output only the polished invite text — no preamble, no markdown, no asterisks,
no bullet symbols, no headers. Plain text only.
```

**Model:** `claude-sonnet-4-6`
**Input:** Template-generated draft with slot time, participants, local times, and purpose.
**Fallback:** Returns raw template if the API call fails.

---

## Tools and APIs called

### Anthropic API

| Call | Where | Purpose |
|------|-------|---------|
| `messages.create` (tool use) | `app/parser.py` | Parse NL request → `MeetingRequest` struct |
| `messages.create` (tool use) | `app/parser.py` | Merge follow-up refinement into existing request |
| `messages.create` (text) | `app/invite.py` | Polish invite draft into professional prose |

### Slack API (via `slack_bolt` + `slack_sdk`)

| Call | Where | Purpose |
|------|-------|---------|
| `chat.postMessage` | `slack_app.py` | Post proposals, alternatives, status updates |
| `chat.update` | `slack_app.py` | Update loading placeholder with final result |
| `chat.postEphemeral` | `slack_app.py` | Show errors visible only to the user |
| `files.uploadV2` | `slack_app.py` | Upload `.ics` file to thread |
| `views.open` | `slack_app.py` | Open `/settings` modal |
| `users.info` | `slack_app.py` | Fetch user's name, email, home timezone |

### Google Calendar API (via `google-api-python-client`)

| Call | Where | Purpose |
|------|-------|---------|
| `freebusy().query()` | `app/gcal.py` | Fetch busy intervals for conflict detection |
| `events().insert()` | `app/gcal.py` | Create calendar event with attendees |
| OAuth 2.0 token refresh | `app/gcal.py` | Refresh per-user access tokens |

### Other

| Tool | Purpose |
|------|---------|
| `zoneinfo` (stdlib) | All timezone math — DST-aware IANA tz conversion |
| `sqlite3` (stdlib) | Per-user memory: preferences, participant cache, meetings log |
| `ics` library | Generate `.ics` files for calendar export |
| FastAPI + uvicorn | Minimal OAuth callback server on port 8080 |

---

## Architecture

```
User (Slack)
    │
    ▼
slack_app.py          ← Bolt Socket Mode handlers, Block Kit UI
    │
    ├─ parser.py       ← Claude: NL → MeetingRequest (structured output)
    │
    ├─ scheduler.py    ← Deterministic: candidate slots, conflict detection, scoring
    │    ├─ timezones.py   ← zoneinfo: resolve dates, tz overlap, working hours
    │    └─ gcal.py        ← Google FreeBusy (if calendar connected)
    │
    ├─ invite.py       ← Claude: template → polished plain-text invite draft
    │
    ├─ store.py        ← SQLite: users, preferences, people cache, meetings log
    │
    └─ oauth_server.py ← FastAPI: /oauth/start + /oauth/callback (Google OAuth)

config/studio.yaml     ← Recurring meetings, role rules, partner tz bands
```

**Core design principle:** Claude only parses language. All date arithmetic, timezone conversion, conflict detection, and slot scoring are deterministic Python (`zoneinfo`), making scheduling results reproducible and DST-correct.
