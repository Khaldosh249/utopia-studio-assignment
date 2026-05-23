"""
Entrypoint: starts the Slack Socket Mode bot + the Google OAuth HTTP server in parallel.
"""
from __future__ import annotations

# load_dotenv() MUST run before any app module is imported,
# because module-level os.environ.get() calls in gcal.py etc. run at import time.
from dotenv import load_dotenv
load_dotenv()

import os
import threading

import uvicorn

from app import store
from app.slack_app import start_socket_mode
from app.oauth_server import app as oauth_app


def _start_oauth_server() -> None:
    port = int(os.environ.get("OAUTH_PORT", 8080))
    uvicorn.run(oauth_app, host="0.0.0.0", port=port, log_level="warning")


def main() -> None:
    store.init_db()
    print("✅ Database initialised")

    # OAuth HTTP server runs in a daemon thread — it exists only to receive the
    # Google callback; Slack itself uses Socket Mode (no HTTP server needed).
    oauth_thread = threading.Thread(target=_start_oauth_server, daemon=True)
    oauth_thread.start()
    print(f"✅ OAuth server listening on :{os.environ.get('OAUTH_PORT', 8080)}")

    print("✅ Starting Slack Socket Mode bot …")
    start_socket_mode()  # blocks until interrupted


if __name__ == "__main__":
    main()
