"""Register or stop the Gmail Pub/Sub watch.

A watch lasts at most 7 days, so it must be refreshed (the app exposes
POST /refresh-watch for a daily scheduler).

    python -m message_gateway.channels.gmail_watch        # register
    python -m message_gateway.channels.gmail_watch stop   # stop

Requires GOOGLE_SERVICE_ACCOUNT_JSON, GMAIL_USER and GMAIL_PUBSUB_TOPIC
(projects/<project>/topics/<topic>); GMAIL_WATCH_LABEL_IDS is optional
(comma-separated, default INBOX).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any

from ..config import ConfigError, Settings
from ..google_auth import GMAIL_SCOPES, service_account_credentials


def label_ids(settings: Settings) -> list[str]:
    ids = [i.strip() for i in settings.gmail_watch_label_ids.split(",") if i.strip()]
    return ids or ["INBOX"]


def _service(settings: Settings) -> Any:
    from googleapiclient.discovery import build

    creds = service_account_credentials(
        settings.google_service_account, GMAIL_SCOPES, subject=settings.gmail_user
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def register_watch(settings: Settings | None = None, *, service: Any = None) -> dict[str, Any]:
    """(Re-)register the watch; returns the new historyId and expiry."""
    settings = settings or Settings.from_env()
    settings.require("gmail_user", "google_service_account", "gmail_pubsub_topic")
    svc = service or _service(settings)
    body = {
        "topicName": settings.gmail_pubsub_topic,
        "labelIds": label_ids(settings),
        "labelFilterAction": "include",
    }
    resp = svc.users().watch(userId="me", body=body).execute()
    exp_ms = int(resp.get("expiration", 0))
    exp = datetime.fromtimestamp(exp_ms / 1000, tz=UTC) if exp_ms else None
    return {
        "history_id": resp.get("historyId", ""),
        "expiration_iso": exp.isoformat() if exp else None,
    }


def stop_watch(settings: Settings | None = None, *, service: Any = None) -> dict[str, Any]:
    settings = settings or Settings.from_env()
    settings.require("gmail_user", "google_service_account")
    svc = service or _service(settings)
    svc.users().stop(userId="me").execute()
    return {"stopped": True}


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(override=True)
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0].lower() if args else "watch"
    try:
        if cmd in {"stop", "halt"}:
            stop_watch()
            print("Watch stopped.")
        else:
            info = register_watch()
            print("Watch registered.")
            print(f"History ID: {info['history_id']}")
            if info["expiration_iso"]:
                print(f"Expires (UTC): {info['expiration_iso']}")
                print("Refresh daily (max watch lifetime is 7 days).")
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
