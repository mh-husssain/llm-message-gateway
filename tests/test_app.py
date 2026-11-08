"""FastAPI app and inbox drain: idempotent skips, never-silent failures."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fakes import ADMIN, ALICE, StubClassifier, classification, intent, seeded_store
from gmail_fake import FakeGmail, gmail_message
from message_gateway.app import GENERIC_ERROR_REPLY, Runtime, create_app, drain_inbox
from message_gateway.channels.gmail import GmailChannel
from message_gateway.channels.gmail_watch import label_ids, register_watch, stop_watch
from message_gateway.config import ConfigError, Settings
from message_gateway.gate import LOCKED_UNKNOWN_REPLY
from message_gateway.records import log_activity


def runtime(fake: FakeGmail, store=None, classifier=None) -> Runtime:
    return Runtime(
        channel=GmailChannel(gmail_user="gateway@example.com", service=fake),
        store=store or seeded_store(),
        classifier=classifier
        or StubClassifier(classification(intent("request_info", scope="my_tasks"))),
    )


def bodies(fake: FakeGmail) -> list[tuple[str, str]]:
    out = []
    for i in range(len(fake.sent)):
        mime = fake.sent_mime(i)
        plain = mime.get_payload()[0].get_payload(decode=True).decode("utf-8")
        out.append((mime["To"], plain))
    return out


def test_drain_processes_each_message_and_labels_it():
    fake = FakeGmail(
        [
            gmail_message(
                "g1", sender=f"Alice <{ALICE}>", subject="my tasks", body="show my tasks"
            ),
            gmail_message("g2", sender="stranger@example.com", subject="hello", body="who are you"),
            gmail_message("g3", sender="noreply@example.com", subject="newsletter", body="buy"),
        ]
    )
    rt = runtime(fake)
    drain_inbox(rt)

    assert [m for m, _ in fake.modified] == ["g1", "g2", "g3"]  # all labelled
    sent = bodies(fake)
    assert sent[0][0] == ALICE and sent[0][1].startswith("Your open tasks")
    assert (("stranger@example.com", LOCKED_UNKNOWN_REPLY)) in sent
    assert any(to == ADMIN for to, _ in sent)  # unknown-sender alert
    assert not any(to == "noreply@example.com" for to, _ in sent)
    assert len(rt.classifier.calls) == 1  # only the known sender was classified


def test_drain_skips_messages_already_marked_processed():
    store = seeded_store()
    log_activity(store, "Message dispatch started", "Alice / msg_id:g1", "")
    fake = FakeGmail([gmail_message("g1", sender=ALICE, body="show my tasks")])
    rt = runtime(fake, store=store)
    drain_inbox(rt)
    assert rt.classifier.calls == []
    assert fake.sent == []
    assert [m for m, _ in fake.modified] == ["g1"]  # label re-applied defensively


def test_redelivery_does_not_run_a_message_twice():
    store = seeded_store()
    fake = FakeGmail([gmail_message("g1", sender=ALICE, body="show my tasks")])
    rt = runtime(fake, store=store)
    drain_inbox(rt)
    drain_inbox(rt)  # e.g. a second push before the label was visible
    assert len(rt.classifier.calls) == 1
    assert len(fake.sent) == 1


def test_unexpected_error_sends_apology_alerts_admin_and_labels():
    class Exploding:
        calls: list = []

        def classify(self, *a):
            raise RuntimeError("model down")

    store = seeded_store()
    fake = FakeGmail([gmail_message("g1", sender=ALICE, subject="help", body="show my tasks")])
    drain_inbox(runtime(fake, store=store, classifier=Exploding()))

    sent = bodies(fake)
    assert (ALICE, GENERIC_ERROR_REPLY) in sent
    admin_alert = next(body for to, body in sent if to == ADMIN)
    assert "RuntimeError: model down" in admin_alert and "show my tasks" in admin_alert
    log = store.rows("Activity_Log")
    assert any(r[1] == "ERROR" and r[2] == "msg_id:g1" for r in log)
    assert any(r[1] == "Generic error reply sent" for r in log)
    assert [m for m, _ in fake.modified] == ["g1"]


def test_health_and_webhook_endpoints():
    fake = FakeGmail([gmail_message("g1", sender=ALICE, body="show my tasks")])
    rt = runtime(fake)
    app = create_app(runtime_factory=lambda: rt, watch_refresher=lambda: {}, load_env=False)
    client = TestClient(app)
    assert client.get("/health").json() == {"ok": True}
    assert client.post("/webhook").json() == {"ok": True}
    assert len(rt.classifier.calls) == 1


def test_refresh_watch_endpoint():
    ok = create_app(
        runtime_factory=lambda: None,
        load_env=False,
        watch_refresher=lambda: {"history_id": "42", "expiration_iso": None},
    )
    assert TestClient(ok).post("/refresh-watch").json() == {
        "ok": True,
        "history_id": "42",
        "expiration_iso": None,
    }

    def failing():
        raise RuntimeError("no topic")

    bad = create_app(runtime_factory=lambda: None, watch_refresher=failing, load_env=False)
    resp = TestClient(bad).post("/refresh-watch")
    assert resp.status_code == 500
    assert resp.json()["detail"] == "RuntimeError: no topic"


def test_no_cron_endpoints_in_the_showcase():
    app = create_app(runtime_factory=lambda: None, watch_refresher=lambda: {}, load_env=False)
    paths = {r.path for r in app.routes}
    assert {"/health", "/webhook", "/refresh-watch"} <= paths
    assert "/cron" not in paths and "/cron-fast" not in paths


def test_watch_registration_without_network():
    class Watch:
        def __init__(self):
            self.body = None
            self.stopped = False

        def users(self):
            return self

        def watch(self, *, userId, body):  # noqa: N803
            self.body = body
            return type(
                "R", (), {"execute": lambda s: {"historyId": "7", "expiration": "1767225600000"}}
            )()

        def stop(self, *, userId):  # noqa: N803
            self.stopped = True
            return type("R", (), {"execute": lambda s: {}})()

    settings = Settings(
        gmail_user="gateway@example.com",
        google_service_account="{}",
        gmail_pubsub_topic="projects/p/topics/t",
        gmail_watch_label_ids="INBOX, Label_2",
    )
    svc = Watch()
    info = register_watch(settings, service=svc)
    assert svc.body == {
        "topicName": "projects/p/topics/t",
        "labelIds": ["INBOX", "Label_2"],
        "labelFilterAction": "include",
    }
    assert info == {"history_id": "7", "expiration_iso": "2026-01-01T00:00:00+00:00"}
    assert stop_watch(settings, service=svc) == {"stopped": True} and svc.stopped
    assert label_ids(Settings(gmail_watch_label_ids=" , ")) == ["INBOX"]
    with pytest.raises(ConfigError, match="GMAIL_PUBSUB_TOPIC"):
        register_watch(
            Settings(gmail_user="g@example.com", google_service_account="x"), service=svc
        )
