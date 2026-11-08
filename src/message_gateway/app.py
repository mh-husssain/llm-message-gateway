"""FastAPI app and inbox drain for the Gmail channel.

    uvicorn --factory message_gateway.app:create_app --port 8080
    python -m message_gateway.app            # serve on :8080
    python -m message_gateway.app drain      # process the inbox once
    python -m message_gateway.app cron       # one project-reminder tick
    python -m message_gateway.app cron-fast  # one independent-reminder tick

Endpoints:
    GET  /health          liveness
    POST /webhook         Pub/Sub push target; drains the inbox
    POST /refresh-watch   re-registers the Gmail watch (call daily)
    POST /cron            project-task reminder engine (call hourly)
    POST /cron-fast       independent-task reminder engine (call every 30 min)

Both reminder engines are no-ops outside business hours, so calling them
more often than needed is safe.

The endpoints do no in-app authentication. The original deployment relied on
the hosting platform for that (private service, push and scheduler identities
allowed to invoke it); that setup is not part of this repository.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException

from .channels.gmail import GmailChannel
from .classifier import Classifier
from .config import Settings
from .models import IncomingMessage
from .records import is_msg_id_processed, load_contacts, log_activity
from .reminders import run_independent_periodic_jobs, run_periodic_jobs
from .router import SupportsClassify, process_message
from .store import Store
from .text import subject_line

log = logging.getLogger("message_gateway.app")

GENERIC_ERROR_REPLY = (
    "Sorry — I hit an internal error while handling your message and "
    "couldn't complete the request. The team has been notified. "
    "If it's urgent, please reach out to your manager directly."
)


@dataclass
class Runtime:
    channel: GmailChannel
    store: Store
    classifier: SupportsClassify


def build_runtime(settings: Settings | None = None) -> Runtime:
    """Production wiring from the environment."""
    from .store.sheets import GoogleSheetsStore

    settings = settings or Settings.from_env()
    channel = GmailChannel.from_env(settings)
    store = GoogleSheetsStore.from_env(settings, credentials=channel.credentials)
    return Runtime(channel=channel, store=store, classifier=Classifier.from_env(settings))


def send_failure_reply_safely(
    channel: GmailChannel, msg: IncomingMessage, store: Store, error: Exception
) -> None:
    """Never silent: apologise to the sender and alert the admin. Each step is
    isolated so a follow-on failure doesn't mask the original error."""
    err_label = f"{type(error).__name__}: {str(error)[:200]}"
    try:
        channel.reply(msg, GENERIC_ERROR_REPLY)
        log_activity(store, "Generic error reply sent", msg.sender_identity, err_label)
    except Exception as reply_err:
        log.exception("failed to send generic error reply for %s", msg.msg_id)
        try:
            log_activity(
                store,
                "Generic error reply FAILED",
                msg.sender_identity,
                f"original={err_label}; reply_err={type(reply_err).__name__}",
            )
        except Exception:
            log.exception("failed to log the failed error reply for %s", msg.msg_id)

    try:
        admin = load_contacts(store).admin
        if admin:
            channel.send_new(
                admin,
                subject=subject_line(f"Processing error from {msg.sender_identity}"),
                body=(
                    f"Failed to process a message from {msg.sender_display_name} "
                    f"({msg.sender_identity}).\n\n"
                    f"Error: {err_label}\n\n"
                    + (f"Subject: {msg.subject}\n\n" if msg.subject else "")
                    + f"{msg.body[:3000]}"
                ),
            )
    except Exception:
        log.exception("failed to alert admin about processing error for %s", msg.msg_id)


def drain_inbox(runtime: Runtime) -> None:
    """Process every unprocessed inbox message (up to one batch)."""
    channel, store = runtime.channel, runtime.store
    msg_ids = channel.list_unprocessed()
    log.info("draining %d unprocessed message(s)", len(msg_ids))
    for mid in msg_ids:
        msg: IncomingMessage | None = None
        try:
            msg = channel.fetch(mid)
            # Idempotency: a redelivered push or a concurrent instance may see
            # the same message. Skip it and re-apply the label defensively.
            if is_msg_id_processed(store, msg.msg_id):
                log.info("skipping already-processed msg_id %s", mid)
                try:
                    channel.mark_processed(msg)
                except Exception:
                    log.exception("defensive mark_processed failed for %s", mid)
                continue
            process_message(msg, channel=channel, store=store, classifier=runtime.classifier)
        except Exception as e:
            log.exception("processing failed for %s", mid)
            log_activity(store, "ERROR", f"msg_id:{mid}", f"{type(e).__name__}: {str(e)[:200]}")
            # The sender always hears back. mark_processed runs last so a
            # crashing message is not retried forever.
            if msg is not None:
                send_failure_reply_safely(channel, msg, store, e)
                try:
                    channel.mark_processed(msg)
                except Exception:
                    log.exception("mark_processed failed after error for %s", mid)


def create_app(
    *,
    runtime_factory: Callable[[], Runtime] | None = None,
    watch_refresher: Callable[[], dict[str, Any]] | None = None,
    load_env: bool = True,
) -> FastAPI:
    if load_env:
        from dotenv import load_dotenv

        load_dotenv(override=True)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if runtime_factory is None:
        runtime_factory = build_runtime
    if watch_refresher is None:
        from .channels.gmail_watch import register_watch

        watch_refresher = register_watch

    app = FastAPI(title="llm-message-gateway")

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/webhook")
    async def webhook() -> dict[str, bool]:
        # The push carries only a historyId; draining instead of tracking
        # history state keeps the handler idempotent and self-healing.
        drain_inbox(runtime_factory())
        return {"ok": True}

    @app.post("/refresh-watch")
    async def refresh_watch() -> dict[str, Any]:
        try:
            info = watch_refresher()
        except Exception as e:
            log.exception("watch refresh failed")
            raise HTTPException(
                status_code=500, detail=f"{type(e).__name__}: {str(e)[:200]}"
            ) from e
        log.info("watch refreshed: %s", info)
        return {"ok": True, **info}

    def _tick(name: str, engine: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        try:
            runtime = runtime_factory()
            summary = engine(runtime.channel, runtime.store)
        except Exception as e:
            log.exception("%s tick failed", name)
            raise HTTPException(
                status_code=500, detail=f"{type(e).__name__}: {str(e)[:200]}"
            ) from e
        log.info("%s tick: %s", name, summary)
        return {"ok": True, **summary}

    @app.post("/cron")
    async def cron() -> dict[str, Any]:
        return _tick("cron", run_periodic_jobs)

    @app.post("/cron-fast")
    async def cron_fast() -> dict[str, Any]:
        return _tick("cron-fast", run_independent_periodic_jobs)

    return app


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else "serve"
    if cmd == "drain":
        from dotenv import load_dotenv

        load_dotenv(override=True)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        drain_inbox(build_runtime())
    elif cmd in {"cron", "cron-fast"}:
        from dotenv import load_dotenv

        load_dotenv(override=True)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        runtime = build_runtime()
        engine = run_periodic_jobs if cmd == "cron" else run_independent_periodic_jobs
        print(engine(runtime.channel, runtime.store))
    else:
        import uvicorn

        uvicorn.run(create_app(), host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
