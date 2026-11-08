"""Gmail adapter without network: loop filters, parsing, threading, labels."""

from __future__ import annotations

import pytest

import message_gateway.channels.gmail as gmail_mod
from gmail_fake import FakeGmail, b64, gmail_message
from message_gateway.channels.gmail import (
    GMAIL_QUERY,
    PROCESSED_LABEL,
    GmailChannel,
    build_multipart,
    extract_text_body,
    strip_quoted_history,
)
from message_gateway.models import Contact, Contacts, IncomingMessage
from message_gateway.store import InMemoryStore

GATEWAY = "gateway@example.com"


def channel(fake: FakeGmail | None = None) -> GmailChannel:
    return GmailChannel(gmail_user=GATEWAY, service=fake or FakeGmail())


def incoming(sender="alice@example.com", subject="Status", headers=None) -> IncomingMessage:
    return IncomingMessage(
        msg_id="m1",
        sender_identity=sender,
        sender_display_name="Alice",
        subject=subject,
        body="hi",
        raw={"headers": headers or {}, "thread_id": "t1", "rfc822_id": "<orig-1@example.com>"},
    )


# ─── Loop / auto-reply filters ─────────────────────────────────────────────


def test_processed_label_and_query():
    assert PROCESSED_LABEL == "gateway-processed"
    assert GMAIL_QUERY == "in:inbox is:unread -label:gateway-processed newer_than:7d"


def test_ordinary_mail_is_not_skipped():
    assert channel().should_skip(incoming()) == (False, "")


def test_own_messages_are_skipped():
    assert channel().should_skip(incoming(sender=GATEWAY)) == (True, "self-message")


@pytest.mark.parametrize(
    "sender",
    [
        "mailer-daemon@example.com",
        "postmaster@example.com",
        "noreply@example.com",
        "no-reply@example.com",
        "donotreply@example.com",
        "do-not-reply@example.com",
        "bounces@example.com",
        "mailer@example.com",
    ],
)
def test_system_senders_are_skipped(sender):
    skip, reason = channel().should_skip(incoming(sender=sender))
    assert skip and reason.startswith("auto-reply from-pattern")


@pytest.mark.parametrize(
    ("header", "value", "skipped"),
    [
        ("Auto-Submitted", "auto-replied", True),
        ("Auto-Submitted", "no", False),
        ("Precedence", "bulk", True),
        ("Precedence", "list", True),
        ("Precedence", "junk", True),
        ("Precedence", "first-class", False),
        ("X-Auto-Response-Suppress", "All", True),
        ("List-Unsubscribe", "<mailto:unsubscribe@example.com>", True),
    ],
)
def test_auto_reply_headers(header, value, skipped):
    skip, reason = channel().should_skip(incoming(headers={header: value}))
    assert skip is skipped
    if skipped:
        assert reason == f"auto-reply header: {header}"


@pytest.mark.parametrize(
    "subject",
    [
        "Undeliverable: report",
        "Delivery Status Notification (Failure)",
        "Out of Office",
        "Automatic reply: x",
        "Auto-Reply",
    ],
)
def test_auto_reply_subjects(subject):
    skip, reason = channel().should_skip(incoming(subject=subject))
    assert skip and reason.startswith("auto-reply subject hint")


# ─── Parsing ───────────────────────────────────────────────────────────────


def test_body_extraction_prefers_plain_text_and_falls_back_to_html():
    assert extract_text_body({"mimeType": "text/plain", "body": {"data": b64("plain")}}) == "plain"
    nested = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/html", "body": {"data": b64("<b>x</b>")}},
                    {"mimeType": "text/plain", "body": {"data": b64("inner plain")}},
                ],
            }
        ],
    }
    assert extract_text_body(nested) == "inner plain"
    html_only = {"mimeType": "text/html", "body": {"data": b64("<p>Hello <b>there</b></p>")}}
    assert extract_text_body(html_only) == "Hello there"
    assert extract_text_body({"mimeType": "multipart/mixed", "parts": []}) == ""


@pytest.mark.parametrize(
    "tail",
    [
        "\nOn Mon, 4 May 2026, Manager One wrote:\n> old text",
        "\n-----Original Message-----\nold",
        "\nFrom: Manager One\nSent: Monday\nTo: Alice\nold",
        "\n" + "_" * 25 + "\nold",
        "\n--\nAlice | Engineer",
        "\n> quoted line\n> another",
    ],
)
def test_quoted_history_and_signatures_are_stripped(tail):
    assert strip_quoted_history("T-0001 done" + tail) == "T-0001 done"


def test_fetch_builds_an_incoming_message():
    fake = FakeGmail(
        [
            gmail_message(
                "g1",
                sender="Alice Example <ALICE@example.com>",
                subject=" Re: x ",
                body="Done.\nOn Tue, Bob wrote:\n> q",
            )
        ]
    )
    m = channel(fake).fetch("g1")
    assert m.msg_id == "g1"
    assert m.sender_identity == "alice@example.com"
    assert m.sender_display_name == "Alice Example"
    assert m.subject == "Re: x"
    assert m.body == "Done."
    assert m.raw["thread_id"] == "thread-1"
    assert m.raw["rfc822_id"] == "<orig-1@example.com>"
    assert m.raw["headers"]["From"].startswith("Alice Example")


def test_list_unprocessed_uses_the_label_query():
    fake = FakeGmail(
        [gmail_message("a", sender="x@example.com"), gmail_message("b", sender="y@example.com")]
    )
    assert channel(fake).list_unprocessed() == ["a", "b"]
    assert fake.queries == [GMAIL_QUERY]


# ─── Outbound ──────────────────────────────────────────────────────────────


def test_reply_is_threaded_and_multipart():
    fake = FakeGmail()
    channel(fake).reply(
        incoming(subject="Status"),
        "Logged update on T-0001.",
        blocks=[("Logged update on T-0001.", "<b>card</b>")],
    )
    sent = fake.sent[0]
    assert sent["threadId"] == "t1"
    mime = fake.sent_mime()
    assert mime["Subject"] == "Re: Status"
    assert mime["In-Reply-To"] == "<orig-1@example.com>"
    assert mime["References"] == "<orig-1@example.com>"
    assert mime["To"] == "alice@example.com" and mime["From"] == GATEWAY
    types = [p.get_content_type() for p in mime.get_payload()]
    assert types == ["text/plain", "text/html"]
    assert "<b>card</b>" in mime.get_payload()[1].get_payload(decode=True).decode()


@pytest.mark.parametrize(("subject", "expected"), [("Re: Status", "Re: Status"), ("", "Re:")])
def test_reply_subject_rules(subject, expected):
    fake = FakeGmail()
    channel(fake).reply(incoming(subject=subject), "ok")
    assert fake.sent_mime()["Subject"] == expected


def test_send_new_is_unthreaded_and_skips_contacts_without_email():
    fake = FakeGmail()
    ch = channel(fake)
    ch.send_new(
        Contact(email="bob@example.com", phone="", name="Bob", role="", permission="employee"),
        subject="[Gateway] New task",
        body="Hi Bob",
    )
    assert "threadId" not in fake.sent[0]
    assert fake.sent_mime()["To"] == "bob@example.com"
    ch.send_new(
        Contact(email="", phone="15555550123", name="Phone Only", role="", permission="employee"),
        subject="x",
        body="y",
    )
    assert len(fake.sent) == 1


def test_plain_part_is_the_unmodified_body():
    body = (
        "Open tasks for Site Six [P-0006] (1 total):\n  • test — assigned, due 07/05/2026 [T-0001]"
    )
    m = build_multipart(body=body, to_addr="a@example.com", from_addr=GATEWAY, subject="Test")
    plain, html = m.get_payload()
    assert plain.get_payload(decode=True).decode("utf-8").strip() == body.strip()
    html_str = html.get_payload(decode=True).decode("utf-8")
    assert "<h3" in html_str and "<li" in html_str


def test_html_render_failure_falls_back_to_plain_only(monkeypatch):
    def broken(_):
        raise RuntimeError("render")

    monkeypatch.setattr(gmail_mod, "render_html", broken)
    m = build_multipart(body="hi", to_addr="a@example.com", from_addr=GATEWAY, subject="s")
    assert [p.get_content_type() for p in m.get_payload()] == ["text/plain"]


def test_mark_processed_creates_the_label_once_and_removes_unread():
    fake = FakeGmail()
    ch = channel(fake)
    ch.mark_processed(incoming())
    ch.mark_processed(incoming())
    assert fake.label_creates == 1
    assert fake.label_list[0]["name"] == "gateway-processed"
    assert fake.modified[0] == ("m1", {"addLabelIds": ["Label_1"], "removeLabelIds": ["UNREAD"]})


def test_existing_label_is_reused():
    fake = FakeGmail(labels=[{"id": "Label_9", "name": "gateway-processed"}])
    channel(fake).mark_processed(incoming())
    assert fake.label_creates == 0
    assert fake.modified[0][1]["addLabelIds"] == ["Label_9"]


def test_unknown_sender_row_goes_to_unknown_emails():
    store = InMemoryStore.with_schema()
    admin = Contact(
        email="admin@example.com", phone="", name="Admin One", role="", permission="admin"
    )
    channel().log_unknown_row(store, incoming(sender="stranger@example.com"), "LOCKED", admin)
    row = store.rows("Unknown_Emails")[0]
    assert row[1:] == [
        "stranger@example.com",
        "Alice",
        "Status",
        "hi",
        "LOCKED",
        "Admin One",
        "Yes — admin alerted",
    ]


def test_lookup_is_by_email_including_aliases():
    c = Contact(
        email="alice@example.com",
        phone="",
        name="Alice",
        role="",
        permission="employee",
        email_aliases=("alice.alt@example.com",),
    )
    contacts = Contacts([c])
    assert channel().lookup_sender(incoming(sender="alice.alt@example.com"), contacts) is c
    assert channel().lookup_sender(incoming(sender="nobody@example.com"), contacts) is None


def test_gmail_user_is_required():
    with pytest.raises(ValueError):
        GmailChannel(gmail_user="", service=FakeGmail())
