"""A minimal in-memory stand-in for the Gmail API resource chain
(`service.users().messages().list(...).execute()` and friends)."""

from __future__ import annotations

import base64
import email
from email.message import Message
from typing import Any


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def gmail_message(
    msg_id: str,
    *,
    sender: str,
    subject: str = "",
    body: str = "",
    headers: dict[str, str] | None = None,
    thread_id: str = "thread-1",
    rfc822_id: str = "<orig-1@example.com>",
) -> dict[str, Any]:
    hdrs = {
        "From": sender,
        "Subject": subject,
        "Message-ID": rfc822_id,
        "To": "gateway@example.com",
    }
    hdrs.update(headers or {})
    return {
        "id": msg_id,
        "threadId": thread_id,
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": k, "value": v} for k, v in hdrs.items()],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64(body)}},
                {"mimeType": "text/html", "body": {"data": b64(f"<p>{body}</p>")}},
            ],
        },
    }


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeGmail:
    """State plus the `users()` entry point GmailChannel calls."""

    def __init__(self, messages: list[dict[str, Any]] | None = None, labels=None):
        self.store = {m["id"]: m for m in (messages or [])}
        self.label_list: list[dict[str, str]] = list(labels or [])
        self.sent: list[dict[str, Any]] = []
        self.modified: list[tuple[str, dict]] = []
        self.queries: list[str] = []
        self.label_creates = 0

    def users(self) -> _Users:
        return _Users(self)

    def sent_mime(self, i: int = -1) -> Message:
        return email.message_from_bytes(base64.urlsafe_b64decode(self.sent[i]["raw"].encode()))


class _Users:
    def __init__(self, g: FakeGmail):
        self.g = g

    def messages(self) -> _Messages:
        return _Messages(self.g)

    def labels(self) -> _Labels:
        return _Labels(self.g)


class _Messages:
    def __init__(self, g: FakeGmail):
        self.g = g

    def list(self, *, userId, q, maxResults):  # noqa: N803
        self.g.queries.append(q)
        return _Req(lambda: {"messages": [{"id": i} for i in list(self.g.store)[:maxResults]]})

    def get(self, *, userId, id, format):  # noqa: A002, N803
        return _Req(lambda: self.g.store[id])

    def send(self, *, userId, body):  # noqa: N803
        return _Req(lambda: self.g.sent.append(body) or {"id": f"sent-{len(self.g.sent)}"})

    def modify(self, *, userId, id, body):  # noqa: A002, N803
        return _Req(lambda: self.g.modified.append((id, body)) or {})


class _Labels:
    def __init__(self, g: FakeGmail):
        self.g = g

    def list(self, *, userId):  # noqa: N803
        return _Req(lambda: {"labels": self.g.label_list})

    def create(self, *, userId, body):  # noqa: N803
        def _create():
            self.g.label_creates += 1
            lab = {"id": f"Label_{len(self.g.label_list) + 1}", "name": body["name"]}
            self.g.label_list.append(lab)
            return lab

        return _Req(_create)
