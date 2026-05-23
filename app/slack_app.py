"""
Slack Bolt Socket Mode bot.
Handles: /schedule, /prefs, interactive buttons, Connect-Calendar DM.
"""
from __future__ import annotations

import json
import os
import tempfile

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from app import store, gcal, invite as invite_mod
from app.parser import parse_request
from app.scheduler import schedule
from app.schemas import ScheduleResult, ProposedSlot, MeetingRequest
from app.timezones import DOHA_TZ
from app.oauth_server import build_auth_url
from dotenv import load_dotenv

load_dotenv()

app = App(token=os.environ["SLACK_BOT_TOKEN"])

# Proposal state keyed by (channel, ts) to support button interactions
_pending_results: dict[str, tuple[ScheduleResult, MeetingRequest]] = {}

# Per-user active proposal context for conversational follow-ups
# slack_id -> (result, request, key)
_active_context: dict[str, tuple[ScheduleResult, MeetingRequest, str]] = {}


# ── /schedule ────────────────────────────────────────────────────────────────

@app.command("/schedule")
def handle_schedule(ack, command, client: WebClient, say):
    ack()
    slack_id = command["user_id"]
    text = command.get("text", "").strip()
    channel = command["channel_id"]

    if not text:
        say("Please describe the meeting you want to schedule. Example:\n"
            "> `/schedule 30-min investor sync with Ahmed (ahmed@x.com, Europe/London) next Tuesday afternoon`")
        return

    # Ensure user exists in DB
    user_info = client.users_info(user=slack_id)["user"]
    store.upsert_user(
        slack_id=slack_id,
        name=user_info.get("real_name", ""),
        email=user_info.get("profile", {}).get("email", ""),
        home_tz=user_info.get("tz", "Asia/Qatar"),
    )

    # Post the request visibly so it's not swallowed by Slack's slash command handling
    client.chat_postMessage(
        channel=channel,
        text=f"📝 *<@{slack_id}>:* `/schedule {text}`",
    )

    loading_resp = client.chat_postMessage(channel=channel, text="⏳ Analysing your request...")
    ts = loading_resp["ts"]

    try:
        request = parse_request(text, slack_id=slack_id)
        prefs = store.get_preferences(slack_id)

        # Fetch real busy times (empty if calendar not connected)
        gcal_busy = gcal.freebusy(
            slack_id,
            window_start=_rough_window_start(request),
            window_end=_rough_window_end(request),
        )

        result = schedule(request, slack_id, gcal_busy=gcal_busy, prefs=prefs)

        if result.status == "no_slot":
            msg = "❌ " + "\n".join(result.warnings or ["No available slot found."])
            client.chat_update(channel=channel, ts=ts, text=msg)
            return

        key = f"{channel}:{ts}"
        _pending_results[key] = (result, request)
        _active_context[slack_id] = (result, request, key)

        log_id = store.log_meeting(slack_id, {
            "request_text": text,
            "best_slot": result.best_slot.start_doha.isoformat() if result.best_slot else "picker",
        })

        if result.show_picker:
            blocks = _slot_picker_blocks(result, request, key, log_id)
            client.chat_update(channel=channel, ts=ts, text="⚠️ Conflict — pick a slot:", blocks=blocks)
        else:
            result.invite_draft = invite_mod.build_draft(result.best_slot, request)
            blocks = _proposal_blocks(result, request, key, log_id)
            client.chat_update(channel=channel, ts=ts, text="📅 Here's a proposed slot:", blocks=blocks)

    except Exception as exc:
        client.chat_update(channel=channel, ts=ts, text=f"❌ Error: {exc}")
        raise


def _rough_window_start(request: MeetingRequest):
    from app.timezones import resolve_window
    start, _ = resolve_window(request.preferred_day, request.relative_week, request.preferred_time_range)
    from datetime import timedelta
    return start.replace(hour=0, minute=0, second=0)


def _rough_window_end(request: MeetingRequest):
    from app.timezones import resolve_window
    _, end = resolve_window(request.preferred_day, request.relative_week, request.preferred_time_range)
    return end.replace(hour=23, minute=59, second=59)


# ── DM handler — single unified listener to avoid double-firing ───────────────
# Using @app.message() (not @app.event("message")) so Bolt's message routing
# handles deduplication. All DM routing lives here.

@app.message()
def handle_dm(message, client: WebClient, say):
    # Ignore non-DMs, bot messages, and system subtypes (join, leave, etc.)
    if message.get("channel_type") != "im":
        return
    if message.get("bot_id") or message.get("subtype"):
        return

    slack_id = message.get("user")
    if not slack_id:
        return
    text = message.get("text", "").strip()
    if not text:
        return

    # ── Route: connect calendar ──────────────────────────────────────────────
    if "connect calendar" in text.lower():
        store.upsert_user(slack_id=slack_id)
        auth_url = build_auth_url(slack_id)
        say(
            text="Connect your Google Calendar",
            blocks=[{
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "Click the button to connect your Google Calendar.\n"
                        "You'll see a *'Google hasn't verified this app'* warning — "
                        "click *Advanced → Continue* to proceed (expected for beta apps)."
                    ),
                },
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔗 Connect Google Calendar"},
                    "url": auth_url,
                    "action_id": "oauth_link",
                },
            }],
        )
        return

    # ── Route: scheduling request (new or follow-up refinement) ─────────────
    user_info = client.users_info(user=slack_id)["user"]
    store.upsert_user(
        slack_id=slack_id,
        name=user_info.get("real_name", ""),
        email=user_info.get("profile", {}).get("email", ""),
        home_tz=user_info.get("tz", "Asia/Qatar"),
    )

    # Check for an active proposal to treat this as a conversational refinement
    existing = _active_context.get(slack_id)
    existing_request = existing[1] if existing else None

    say("⏳ Analysing your request...")

    try:
        request = parse_request(text, slack_id=slack_id, context=existing_request)
        prefs = store.get_preferences(slack_id)
        gcal_busy = gcal.freebusy(
            slack_id,
            window_start=_rough_window_start(request),
            window_end=_rough_window_end(request),
        )
        result = schedule(request, slack_id, gcal_busy=gcal_busy, prefs=prefs)

        if result.status == "no_slot":
            say("❌ " + "\n".join(result.warnings or ["No available slot found."]))
            return

        key = f"{message['channel']}:dm"
        _pending_results[key] = (result, request)
        _active_context[slack_id] = (result, request, key)

        if result.show_picker:
            blocks = _slot_picker_blocks(result, request, key, None)
            say(text="⚠️ Conflict — pick a slot:", blocks=blocks)
        else:
            result.invite_draft = invite_mod.build_draft(result.best_slot, request)
            blocks = _proposal_blocks(result, request, key, None)
            say(text="📅 Here's a proposed slot:", blocks=blocks)

    except Exception as exc:
        say(f"❌ Error: {exc}")
        raise


# ── Button handlers ───────────────────────────────────────────────────────────

@app.action("confirm_gcal")
def handle_confirm_gcal(ack, body, client: WebClient):
    ack()
    slack_id = body["user"]["id"]
    action = body["actions"][0]
    payload = json.loads(action["value"])
    key = payload["key"]
    log_id = payload.get("log_id")
    channel = body["container"]["channel_id"]
    ts = body["container"]["message_ts"]

    result, request = _pending_results.get(key, (None, None))
    if not result or not result.best_slot:
        client.chat_postEphemeral(channel=channel, user=slack_id, text="Session expired. Please re-run `/schedule`.")
        return

    slot = result.best_slot
    emails = [p.email for p in request.participants if p.email]
    summary = f"{request.meeting_type.replace('_', ' ').title()} — Utopia Studio"
    event_link = gcal.create_event(
        slack_id=slack_id,
        summary=summary,
        start=slot.start_doha,
        end=slot.end_doha,
        attendees=emails,
        description=result.invite_draft or "",
    )

    if event_link:
        store.update_meeting_status(log_id, "confirmed_gcal") if log_id else None
        status = f"✅ Added to Google Calendar — <{event_link}|Open event>"
        blocks = _post_confirm_blocks(result, request, key, log_id, status)
        client.chat_update(channel=channel, ts=ts, text=status, blocks=blocks)
    else:
        client.chat_postEphemeral(
            channel=channel, user=slack_id,
            text="❌ Failed to create event. Is your Google Calendar connected? DM me `connect calendar`.",
        )


@app.action("confirm_ics")
def handle_confirm_ics(ack, body, client: WebClient):
    ack()
    slack_id = body["user"]["id"]
    action = body["actions"][0]
    payload = json.loads(action["value"])
    key = payload["key"]
    log_id = payload.get("log_id")
    channel = body["container"]["channel_id"]
    ts = body["container"]["message_ts"]

    result, request = _pending_results.get(key, (None, None))
    if not result or not result.best_slot:
        client.chat_postEphemeral(channel=channel, user=slack_id, text="Session expired.")
        return

    slot = result.best_slot
    emails = [p.email for p in request.participants if p.email]
    summary = f"{request.meeting_type.replace('_', ' ').title()} — Utopia Studio"

    ics_bytes = gcal.make_ics(
        summary=summary,
        start=slot.start_doha,
        end=slot.end_doha,
        attendees=emails,
        description=result.invite_draft or "",
    )
    add_url = gcal.add_to_gcal_url(
        summary=summary,
        start=slot.start_doha,
        end=slot.end_doha,
        description=result.invite_draft or "",
        attendees=emails,
    )

    with tempfile.NamedTemporaryFile(suffix=".ics", delete=False) as f:
        f.write(ics_bytes)
        tmp_path = f.name

    client.files_upload_v2(
        channel=channel,
        file=tmp_path,
        filename="invite.ics",
        initial_comment=f"📎 .ics file attached. Or <{add_url}|add directly to Google Calendar>.",
    )
    if log_id:
        store.update_meeting_status(log_id, "draft_sent")
    # Keep _pending_results so the user can still add to GCal from the original message
    status = "📎 .ics sent above"
    blocks = _post_confirm_blocks(result, request, key, log_id, status)
    client.chat_update(channel=channel, ts=ts, text=status, blocks=blocks)


@app.action("show_alternatives")
def handle_alternatives(ack, body, client: WebClient):
    ack()
    slack_id = body["user"]["id"]
    payload = json.loads(body["actions"][0]["value"])
    key = payload["key"]
    channel = body["container"]["channel_id"]

    result, request = _pending_results.get(key, (None, None))
    if not result or not result.alternatives:
        client.chat_postEphemeral(channel=channel, user=slack_id, text="No alternative slots available.")
        return

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Alternative slots — choose one:*"}},
        {"type": "divider"},
    ]

    for i, alt in enumerate(result.alternatives):
        start = alt.start_doha
        end = alt.end_doha
        duration_min = int((end - start).total_seconds() // 60)

        # Build description with local times
        local_line = "  |  ".join(
            f"{n}: {t}" for n, t in alt.participant_local_times.items()
        )
        # Proximity / fellow warnings from reasoning
        warnings = [r for r in alt.reasoning if r.startswith("⚠️")]

        desc = f"*{start.strftime('%A %d %b')} · {start.strftime('%H:%M')}–{end.strftime('%H:%M')} Doha* ({duration_min} min)"
        if local_line:
            desc += f"\n_{local_line}_"
        for w in warnings:
            desc += f"\n{w}"

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": desc},
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": f"Choose #{i + 1}"},
                "action_id": "pick_alternative",
                "value": json.dumps({"key": key, "alt_index": i}),
            },
        })

    client.chat_postMessage(channel=channel, blocks=blocks, text="Alternative slots")


@app.action("pick_alternative")
def handle_pick_alternative(ack, body, client: WebClient):
    ack()
    slack_id = body["user"]["id"]
    payload = json.loads(body["actions"][0]["value"])
    key = payload["key"]
    alt_index = int(payload["alt_index"])
    channel = body["container"]["channel_id"]

    result, request = _pending_results.get(key, (None, None))
    if not result or alt_index >= len(result.alternatives):
        client.chat_postEphemeral(channel=channel, user=slack_id, text="That slot is no longer available.")
        return

    # Swap chosen slot into best_slot and clear the picker flag + stale conflict notes
    chosen = result.alternatives[alt_index]
    result.best_slot = chosen
    result.alternatives = [s for i, s in enumerate(result.alternatives) if i != alt_index]
    result.show_picker = False
    result.explicit_time_note = None
    result.conflict_details = []
    result.invite_draft = invite_mod.build_draft(chosen, request)
    _pending_results[key] = (result, request)

    # Update active context so follow-up DMs reflect the chosen slot
    if slack_id in _active_context:
        _active_context[slack_id] = (result, request, key)

    blocks = _proposal_blocks(result, request, key, None)
    client.chat_postMessage(channel=channel, text="📅 Here's your chosen slot:", blocks=blocks)


@app.action("cancel_schedule")
def handle_cancel(ack, body, client: WebClient):
    ack()
    slack_id = body["user"]["id"]
    channel = body["container"]["channel_id"]
    ts = body["container"]["message_ts"]
    _active_context.pop(slack_id, None)
    client.chat_update(channel=channel, ts=ts, text="❌ Scheduling cancelled.", blocks=[])


# ── /settings ─────────────────────────────────────────────────────────────────

@app.command("/settings")
def handle_prefs(ack, command, client: WebClient):
    ack()
    slack_id = command["user_id"]
    prefs = store.get_preferences(slack_id)

    client.views_open(
        trigger_id=command["trigger_id"],
        view=_prefs_modal(prefs),
    )


@app.view("save_prefs")
def handle_save_prefs(ack, body, view, client: WebClient):
    ack()
    slack_id = body["user"]["id"]
    vals = view["state"]["values"]

    def get_val(block_id: str, action_id: str, field: str = "value") -> str | None:
        try:
            return vals[block_id][action_id].get(field)
        except (KeyError, TypeError):
            return None

    def get_selected(block_id: str, action_id: str) -> str | None:
        try:
            opt = vals[block_id][action_id].get("selected_option")
            return opt["value"] if opt else None
        except (KeyError, TypeError):
            return None

    no_mtg_raw = get_val("no_meeting_days", "no_meeting_days_input") or ""
    try:
        no_mtg = [int(x.strip()) for x in no_mtg_raw.split(",") if x.strip().isdigit()]
    except ValueError:
        no_mtg = []

    prefs = {
        "buffer_minutes":      int(get_val("buffer", "buffer_input") or 15),
        "batching_style":      get_selected("batching", "batching_select") or "none",
        "pref_window_start":   get_val("window_start", "window_start_input"),
        "pref_window_end":     get_val("window_end", "window_end_input"),
        "default_duration_mins": int(get_val("duration", "duration_input") or 45),
        "no_meeting_days":     no_mtg,
    }
    store.save_preferences(slack_id, prefs)


# ── Connect Google Calendar button ack ───────────────────────────────────────
# The OAuth flow is initiated inside handle_dm(); this just acks the button click.

@app.action("oauth_link")
def handle_oauth_link(ack):
    ack()


# ── Block Kit builders ────────────────────────────────────────────────────────

def _slot_picker_blocks(result: ScheduleResult, request: MeetingRequest, key: str, log_id) -> list[dict]:
    """
    Shown when the user's requested explicit_time is blocked.
    Lists all available slots as buttons — no auto-selection.
    """
    cancel_value = json.dumps({"key": key, "log_id": log_id})

    header_parts = []
    if result.explicit_time_note:
        header_parts.append(result.explicit_time_note)
    if result.conflict_details:
        lines = "\n".join(f"• {c}" for c in result.conflict_details)
        header_parts.append(f"*Conflicts in this window:*\n{lines}")
    header_parts.append("*Pick an available slot:*")

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n\n".join(header_parts)}},
        {"type": "divider"},
    ]

    for i, slot in enumerate(result.alternatives):
        start = slot.start_doha
        end = slot.end_doha
        duration_min = int((end - start).total_seconds() // 60)

        local_line = "  |  ".join(f"{n}: {t}" for n, t in slot.participant_local_times.items())
        desc = f"*{start.strftime('%H:%M')}–{end.strftime('%H:%M')} Doha* ({duration_min} min)"
        if local_line:
            desc += f"\n_{local_line}_"
        proximity = [r for r in slot.reasoning if r.startswith("⚠️")]
        for w in proximity:
            desc += f"\n{w}"

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": desc},
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": start.strftime("%H:%M")},
                "action_id": "pick_alternative",
                "value": json.dumps({"key": key, "alt_index": i}),
            },
        })

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "actions",
        "elements": [{
            "type": "button",
            "text": {"type": "plain_text", "text": "❌ Cancel"},
            "style": "danger",
            "action_id": "cancel_schedule",
            "value": cancel_value,
        }],
    })
    return blocks


def _post_confirm_blocks(
    result: ScheduleResult,
    request: MeetingRequest,
    key: str,
    log_id,
    status_note: str,
) -> list[dict]:
    """
    Shown after the user confirms (GCal or .ics).
    Keeps the slot details visible; replaces the full action row with only the
    .ics / GCal link button so the user can still export — but can no longer
    cancel or browse alternatives.
    """
    action_value = json.dumps({"key": key, "log_id": log_id})
    proposal = _proposal_blocks(result, request, key, log_id)
    # proposal[-1] is the actions block — replace it with a single .ics button
    blocks = proposal[:-1] + [
        {"type": "section", "text": {"type": "mrkdwn", "text": status_note}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "📎 Get .ics / link"},
                    "action_id": "confirm_ics",
                    "value": action_value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Add to Google Calendar"},
                    "action_id": "confirm_gcal",
                    "value": action_value,
                },
            ],
        },
    ]
    return blocks


def _proposal_blocks(result: ScheduleResult, request: MeetingRequest, key: str, log_id) -> list[dict]:
    slot = result.best_slot
    start = slot.start_doha
    end = slot.end_doha
    duration_min = int((end - start).total_seconds() // 60)

    action_value = json.dumps({"key": key, "log_id": log_id})

    local_times_text = "\n".join(
        f"• *{name}*: {t}" for name, t in slot.participant_local_times.items()
    )
    # Split reasoning into warnings and normal facts
    reasoning_warnings = [r for r in slot.reasoning if r.startswith("⚠️")]
    reasoning_facts    = [r for r in slot.reasoning if not r.startswith("⚠️")]
    reasoning_text = "\n".join(f"✓ {r}" for r in reasoning_facts) if reasoning_facts else ""
    proximity_text = "\n".join(reasoning_warnings) if reasoning_warnings else ""
    avoided_text = (
        "Avoids: " + ", ".join(slot.conflicts_avoided)
        if slot.conflicts_avoided else ""
    )
    warnings_text = "\n".join(result.warnings) if result.warnings else ""

    body_parts = [f"📅 *{start.strftime('%A, %d %B')} — {start.strftime('%H:%M')}–{end.strftime('%H:%M')} Doha (UTC+3)*"]
    body_parts.append(f"⏱ {duration_min} minutes · {request.meeting_type.replace('_', ' ').title()}")
    if local_times_text:
        body_parts.append(f"\n*Local times:*\n{local_times_text}")
    # Explicit-time note (e.g. "15:00 conflicts with QDB — moved to 15:30")
    if result.explicit_time_note:
        body_parts.append(f"\n{result.explicit_time_note}")

    # All meetings on the proposed day — only label as conflicts when an explicit time was blocked
    if result.conflict_details:
        conflict_lines = "\n".join(f"• {c}" for c in result.conflict_details)
        if result.explicit_time_note and "conflicts" in (result.explicit_time_note or ""):
            header = "*Conflicts in this window:*"
        else:
            day_label = start.strftime("%A %-d %b")
            header = f"*Meetings on {day_label}:*"
        body_parts.append(f"\n{header}\n{conflict_lines}")

    if avoided_text:
        body_parts.append(f"\n{avoided_text}")
    if reasoning_text:
        body_parts.append(f"\n*Why this slot:*\n{reasoning_text}")
    if proximity_text:
        body_parts.append(f"\n{proximity_text}")
    if warnings_text:
        body_parts.append(f"\n⚠️ {warnings_text}")
    if result.invite_draft:
        body_parts.append(f"\n*Invite draft:*\n```{result.invite_draft}```")
    body_parts.append("\n_💬 DM me to refine — e.g. 'try 3pm', 'make it 1 hour', 'add Sarah (sarah@x.com, Europe/London)'_")

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(body_parts)}},
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Add to Google Calendar"},
                    "style": "primary",
                    "action_id": "confirm_gcal",
                    "value": action_value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "📎 Get .ics / link"},
                    "action_id": "confirm_ics",
                    "value": action_value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔄 Show alternatives"},
                    "action_id": "show_alternatives",
                    "value": action_value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Cancel"},
                    "style": "danger",
                    "action_id": "cancel_schedule",
                    "value": action_value,
                },
            ],
        },
    ]
    return blocks


_BATCHING_OPTIONS = [
    {"text": {"type": "plain_text", "text": "No preference"}, "value": "none"},
    {"text": {"type": "plain_text", "text": "Batch (cluster together)"}, "value": "batch"},
    {"text": {"type": "plain_text", "text": "Spread (distribute)"}, "value": "spread"},
]
_BATCHING_BY_VALUE = {o["value"]: o for o in _BATCHING_OPTIONS}


def _prefs_modal(prefs: dict) -> dict:
    no_mtg = ",".join(str(d) for d in prefs.get("no_meeting_days", []))
    batching_value = prefs.get("batching_style", "none")
    # initial_option must be an exact copy of one of the options entries
    batching_initial = _BATCHING_BY_VALUE.get(batching_value, _BATCHING_OPTIONS[0])
    return {
        "type": "modal",
        "callback_id": "save_prefs",
        "title": {"type": "plain_text", "text": "Scheduling Settings"},
        "submit": {"type": "plain_text", "text": "Save"},
        "blocks": [
            {
                "type": "input", "block_id": "buffer",
                "label": {"type": "plain_text", "text": "Buffer between meetings (minutes)"},
                "element": {
                    "type": "plain_text_input", "action_id": "buffer_input",
                    "initial_value": str(prefs.get("buffer_minutes", 15)),
                },
            },
            {
                "type": "input", "block_id": "batching",
                "label": {"type": "plain_text", "text": "Meeting distribution style"},
                "element": {
                    "type": "static_select", "action_id": "batching_select",
                    "initial_option": batching_initial,
                    "options": _BATCHING_OPTIONS,
                },
            },
            {
                "type": "input", "block_id": "window_start",
                "label": {"type": "plain_text", "text": "Preferred meeting window start (HH:MM Doha)"},
                "element": {
                    "type": "plain_text_input", "action_id": "window_start_input",
                    "initial_value": prefs.get("pref_window_start") or "",
                    "placeholder": {"type": "plain_text", "text": "e.g. 10:00"},
                },
                "optional": True,
            },
            {
                "type": "input", "block_id": "window_end",
                "label": {"type": "plain_text", "text": "Preferred meeting window end (HH:MM Doha)"},
                "element": {
                    "type": "plain_text_input", "action_id": "window_end_input",
                    "initial_value": prefs.get("pref_window_end") or "",
                    "placeholder": {"type": "plain_text", "text": "e.g. 16:00"},
                },
                "optional": True,
            },
            {
                "type": "input", "block_id": "duration",
                "label": {"type": "plain_text", "text": "Default meeting duration (minutes)"},
                "element": {
                    "type": "plain_text_input", "action_id": "duration_input",
                    "initial_value": str(prefs.get("default_duration_mins", 45)),
                },
            },
            {
                "type": "input", "block_id": "no_meeting_days",
                "label": {"type": "plain_text", "text": "No-meeting days (0=Mon … 4=Fri, comma-separated)"},
                "element": {
                    "type": "plain_text_input", "action_id": "no_meeting_days_input",
                    "initial_value": no_mtg,
                    "placeholder": {"type": "plain_text", "text": "e.g. 4 for no-meeting Fridays"},
                },
                "optional": True,
            },
        ],
    }


def start_socket_mode():
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
