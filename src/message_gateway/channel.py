"""The channel protocol: the only seam between the core and a transport.

A channel turns its transport's payloads into `IncomingMessage`s and knows how
to reply, send fresh messages, mark a message processed and log an unknown
sender. Everything else (gate, classification, handlers, store) is shared.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .models import Contact, Contacts, IncomingMessage

if TYPE_CHECKING:
    from .store import Store

# One entry per reply section: (plain_text, pre-rendered HTML block or None).
ReplyBlocks = list[tuple[str, str | None]]


@runtime_checkable
class Channel(Protocol):
    name: str  # 'email' | ...
    unknown_log_action: str  # Activity_Log action used for unknown senders

    def lookup_sender(self, msg: IncomingMessage, contacts: Contacts) -> Contact | None: ...

    def should_skip(self, msg: IncomingMessage) -> tuple[bool, str]:
        """Return (skip, reason). Filters auto-replies, loops and system messages."""
        ...

    def reply(
        self, msg: IncomingMessage, body: str, *, blocks: ReplyBlocks | None = None
    ) -> None: ...

    def send_new(
        self, contact: Contact, subject: str, body: str, *, blocks: ReplyBlocks | None = None
    ) -> None: ...

    def mark_processed(self, msg: IncomingMessage) -> None: ...

    def log_unknown_row(
        self, store: Store, msg: IncomingMessage, locked_reply: str, admin: Contact
    ) -> None: ...
