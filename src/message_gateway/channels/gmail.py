"""Gmail channel adapter.

Inbound: a Gmail watch publishes to Pub/Sub, Pub/Sub pushes to /webhook, and
the webhook drains every unread, unlabelled inbox message (the notification
itself carries only a historyId). Processed messages get the
`gateway-processed` label and lose UNREAD, so the drain is self-healing.

Outbound: replies are threaded (In-Reply-To / References / threadId) and sent
as multipart/alternative, plain text plus HTML.

Auto-replies, bounces, bulk mail and the gateway's own messages are filtered
before anything else runs, which prevents mail loops.
"""

from __future__ import annotations

import base64
import email.utils
import logging
import re
from collections.abc import Callable
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from ..channel import ReplyBlocks
from ..clock import fmt_ts, now_local
from ..config import Settings
from ..email_format import render_blocks, render_html
from ..google_auth import GMAIL_SCOPES, service_account_credentials
from ..models import Contact, Contacts, IncomingMessage
from ..schema import UNKNOWN_EMAILS
from ..store import Store
from ..text import normalize_email

log = logging.getLogger("message_gateway.channels.gmail")

PROCESSED_LABEL = "gateway-processed"
GMAIL_QUERY = f"in:inbox is:unread -label:{PROCESSED_LABEL} newer_than:7d"
DRAIN_BATCH_SIZE = 25

AUTOREPLY_FROM_PATTERNS = (
    "mailer-daemon@",
    "postmaster@",
    "noreply@",
    "no-reply@",
    "donotreply@",
    "do-not-reply@",
    "bounces@",
    "mailer@",
)
AUTOREPLY_HEADER_FLAGS: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("Auto-Submitted", lambda v: bool(v) and v.lower() != "no"),
    ("Precedence", lambda v: bool(v) and v.lower() in {"bulk", "list", "junk"}),
    ("X-Auto-Response-Suppress", lambda v: bool(v)),
    ("List-Unsubscribe", lambda v: bool(v)),
)
AUTOREPLY_SUBJECT_HINTS = (
    "undeliverable",
    "delivery status notification",
    "out of office",
    "automatic reply",
    "auto-reply",
)


# ────────────────────────────────────────────────────────────────────────────
# MIME and body extraction
# ────────────────────────────────────────────────────────────────────────────


def _decode_b64(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="ignore")


def extract_text_body(payload: dict[str, Any]) -> str:
    """Walk the MIME tree: prefer text/plain, fall back to tag-stripped HTML."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return _decode_b64(payload["body"]["data"])
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return _decode_b64(part["body"]["data"])
    for part in payload.get("parts", []):
        text = extract_text_body(part)
        if text:
            return text
    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        return re.sub(r"<[^>]+>", "", _decode_b64(payload["body"]["data"]))
    return ""


_QUOTED_HISTORY_PATTERNS = (
    r"\nOn .+ wrote:\n.*",
    r"\n-----Original Message-----\n.*",
    r"\nFrom: .+\nSent: .+\nTo: .+\n.*",
    r"\n_{20,}\n.*",
    r"\n--\s*\n.*",
    r"\n>+ .+(\n>+ .*)*",
)


def strip_quoted_history(body: str) -> str:
    """Drop quoted replies and signatures so only the new text is classified."""
    out = body
    for p in _QUOTED_HISTORY_PATTERNS:
        out = re.sub(p, "", out, flags=re.DOTALL)
    return out.strip()


def build_multipart(
    *, body: str, to_addr: str, from_addr: str, subject: str, blocks: ReplyBlocks | None = None
) -> MIMEMultipart:
    """multipart/alternative with the plain body first and HTML last (RFC 2046:
    the last part is preferred). If HTML rendering fails, plain-only is sent."""
    m = MIMEMultipart("alternative")
    m["To"] = to_addr
    m["From"] = from_addr
    m["Subject"] = subject
    m.attach(MIMEText(body, "plain", "utf-8"))
    try:
        html_body = render_blocks(blocks) if blocks is not None else render_html(body)
    except Exception:
        log.exception("HTML render failed; sending plain-only fallback")
        html_body = ""
    if html_body:
        m.attach(MIMEText(html_body, "html", "utf-8"))
    return m


# ────────────────────────────────────────────────────────────────────────────
# Channel
# ────────────────────────────────────────────────────────────────────────────


class GmailChannel:
    name = "email"
    unknown_log_action = "UNKNOWN_CONTACT_MESSAGE"

    def __init__(self, *, gmail_user: str, service: Any = None, credentials: Any = None) -> None:
        if not gmail_user:
            raise ValueError("gmail_user is required")
        self.gmail_user = gmail_user
        # The delegated credentials also carry the spreadsheets scope, so the
        # store can reuse them.
        self.credentials = credentials
        if service is None:
            from googleapiclient.discovery import build

            service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        self._gmail = service
        self._label_id_cache: str | None = None

    @classmethod
    def from_env(cls, settings: Settings) -> GmailChannel:
        settings.require("gmail_user", "google_service_account")
        creds = service_account_credentials(
            settings.google_service_account, GMAIL_SCOPES, subject=settings.gmail_user
        )
        return cls(gmail_user=settings.gmail_user, credentials=creds)

    # ─── Gmail-specific ────────────────────────────────────────────────────

    def _processed_label_id(self) -> str:
        if self._label_id_cache:
            return self._label_id_cache
        labels = self._gmail.users().labels().list(userId="me").execute().get("labels", [])
        for lab in labels:
            if lab["name"] == PROCESSED_LABEL:
                self._label_id_cache = lab["id"]
                return lab["id"]
        created = (
            self._gmail.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": PROCESSED_LABEL,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        self._label_id_cache = created["id"]
        return created["id"]

    def list_unprocessed(self, max_results: int = DRAIN_BATCH_SIZE) -> list[str]:
        resp = (
            self._gmail.users()
            .messages()
            .list(userId="me", q=GMAIL_QUERY, maxResults=max_results)
            .execute()
        )
        return [m["id"] for m in resp.get("messages", [])]

    def fetch(self, msg_id: str) -> IncomingMessage:
        m = self._gmail.users().messages().get(userId="me", id=msg_id, format="full").execute()
        headers = {h["name"]: h["value"] for h in m["payload"].get("headers", [])}
        from_name, from_addr = email.utils.parseaddr(headers.get("From", ""))
        return IncomingMessage(
            msg_id=msg_id,
            sender_identity=normalize_email(from_addr),
            sender_display_name=from_name or from_addr,
            subject=headers.get("Subject", "").strip(),
            body=strip_quoted_history(extract_text_body(m["payload"])),
            raw={
                "thread_id": m.get("threadId", ""),
                "rfc822_id": headers.get("Message-ID", ""),
                "headers": headers,
                "to": headers.get("To", ""),
            },
        )

    # ─── Channel protocol ──────────────────────────────────────────────────

    def lookup_sender(self, msg: IncomingMessage, contacts: Contacts) -> Contact | None:
        return contacts.by_email(msg.sender_identity)

    def should_skip(self, msg: IncomingMessage) -> tuple[bool, str]:
        if msg.sender_identity == self.gmail_user.lower():
            return True, "self-message"
        addr = msg.sender_identity
        for p in AUTOREPLY_FROM_PATTERNS:
            if p in addr:
                return True, f"auto-reply from-pattern: {p}"
        headers = msg.raw.get("headers", {})
        for header, predicate in AUTOREPLY_HEADER_FLAGS:
            if predicate(headers.get(header, "")):
                return True, f"auto-reply header: {header}"
        subj = msg.subject.lower()
        for hint in AUTOREPLY_SUBJECT_HINTS:
            if hint in subj:
                return True, f"auto-reply subject hint: {hint}"
        return False, ""

    def reply(self, msg: IncomingMessage, body: str, *, blocks: ReplyBlocks | None = None) -> None:
        rfc822_id = msg.raw.get("rfc822_id", "")
        thread_id = msg.raw.get("thread_id", "")
        if msg.subject.lower().startswith("re:"):
            subject = msg.subject
        else:
            subject = f"Re: {msg.subject}" if msg.subject else "Re:"
        m = build_multipart(
            body=body,
            to_addr=msg.sender_identity,
            from_addr=self.gmail_user,
            subject=subject,
            blocks=blocks,
        )
        if rfc822_id:
            m["In-Reply-To"] = rfc822_id
            m["References"] = rfc822_id
        raw = base64.urlsafe_b64encode(m.as_bytes()).decode()
        self._gmail.users().messages().send(
            userId="me", body={"raw": raw, "threadId": thread_id}
        ).execute()

    def send_new(
        self, contact: Contact, subject: str, body: str, *, blocks: ReplyBlocks | None = None
    ) -> None:
        if not contact.email:
            log.warning("send_new: contact %s has no email", contact.name)
            return
        m = build_multipart(
            body=body,
            to_addr=contact.email,
            from_addr=self.gmail_user,
            subject=subject,
            blocks=blocks,
        )
        raw = base64.urlsafe_b64encode(m.as_bytes()).decode()
        self._gmail.users().messages().send(userId="me", body={"raw": raw}).execute()

    def mark_processed(self, msg: IncomingMessage) -> None:
        self._gmail.users().messages().modify(
            userId="me",
            id=msg.msg_id,
            body={"addLabelIds": [self._processed_label_id()], "removeLabelIds": ["UNREAD"]},
        ).execute()

    def log_unknown_row(
        self, store: Store, msg: IncomingMessage, locked_reply: str, admin: Contact
    ) -> None:
        store.append(
            UNKNOWN_EMAILS.range_full,
            [
                fmt_ts(now_local()),
                msg.sender_identity,
                msg.sender_display_name,
                msg.subject,
                msg.body[:5000],
                locked_reply,
                admin.name,
                "Yes — admin alerted",
            ],
        )
