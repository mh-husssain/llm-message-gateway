"""Per-intent handler context and the shared reply buffer.

Every intent in a message writes into one `ReplyBuffer`; the router sends a
single combined reply at the end, so a multi-request email gets one threaded
answer instead of several.
"""

from __future__ import annotations

from dataclasses import dataclass

from .channel import Channel, ReplyBlocks
from .classifier import IntentResult
from .models import Contact, Contacts, IncomingMessage, Project
from .store import Store
from .text import ack


class ReplyBuffer:
    def __init__(self, lang: str) -> None:
        self.lang = lang
        self.entries: ReplyBlocks = []

    def text(self, en: str, ar: str) -> None:
        """Add a plain section in the message's language."""
        self.entries.append((ack(en, ar, self.lang), None))

    def block(self, plain: str, html_block: str) -> None:
        """Add a section with a pre-rendered HTML card alongside its plain text."""
        self.entries.append((plain, html_block))

    @property
    def plain(self) -> str:
        return "\n\n".join(p for p, _ in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


@dataclass
class HandlerContext:
    channel: Channel
    store: Store
    msg: IncomingMessage
    sender: Contact
    intent: IntentResult
    contacts: Contacts
    projects: dict[str, Project]
    replies: ReplyBuffer = None  # type: ignore[assignment]  # set in __post_init__

    def __post_init__(self) -> None:
        if self.replies is None:
            self.replies = ReplyBuffer(self.msg.language)

    @property
    def lang(self) -> str:
        return self.msg.language

    @property
    def extracted(self) -> dict:
        return self.intent.extracted

    @property
    def sender_id(self) -> str:
        return self.sender.identity

    def reply(self, en: str, ar: str) -> None:
        self.replies.text(en, ar)

    def reply_block(self, plain: str, html_block: str) -> None:
        self.replies.block(plain, html_block)
