"""What a reminder rule returns when it fires."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import Contact, IndependentTask, Task


@dataclass(frozen=True)
class RuleResult:
    """One outbound reminder or escalation, plus the state it leaves behind.

    The engine applies `field_updates` to the task, sends `body_en` to
    `recipient` (reminders go out in English), records an outbound conversation
    row and logs the firing. `body_ar` is the Arabic rendering of the same text.
    """

    rule: str
    task: Task | IndependentTask
    recipient: Contact
    subject: str
    body_en: str
    body_ar: str
    field_updates: dict[str, Any]
    status_after: str
    conversation_purpose: str
    conversation_awaiting_reply: bool
    conversation_role: str
    conversation_participant: str
