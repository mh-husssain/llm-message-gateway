"""Domain models: contacts, projects, tasks, conversations and inbound messages."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .clock import parse_sheet_date, parse_ts
from .text import is_arabic, normalize_email, normalize_phone

# Task status state machine (stored values).
STATUS_ASSIGNED = "assigned"
STATUS_IN_PROGRESS = "in_progress"
STATUS_AWAITING_ASSIGNEE_COMPLETION = "awaiting_assignee_completion"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_DELAYED = "delayed"
STATUS_AWAITING_OWNER_CONFIRMATION = "awaiting_owner_confirmation"
STATUS_AWAITING_ASSIGNEE_ETA = "awaiting_assignee_eta"
STATUS_AWAITING_EXTERNAL_COMPLETION = "awaiting_external_completion"
STATUS_ESCALATED = "escalated"
STATUS_BLOCKED_BY_PREDECESSOR = "blocked"  # waits for the previous sub-step

TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_CANCELLED, STATUS_ESCALATED})

# Older rows used TitleCase values ("New", "In Progress"); they are mapped to
# the canonical snake_case values at load time. Unknown values pass through.
_STATUS_ALIASES = {
    "new": STATUS_ASSIGNED,
    "canceled": STATUS_CANCELLED,
}


def normalize_status(raw: str) -> str:
    """Lowercase, replace spaces with underscores, map legacy aliases."""
    if not raw:
        return ""
    key = raw.strip().lower().replace(" ", "_")
    return _STATUS_ALIASES.get(key, key)


@dataclass(frozen=True)
class Contact:
    email: str  # primary normalised address; '' if not on file
    phone: str  # normalised digits; '' if not on file
    name: str
    role: str
    permission: str  # admin | manager | employee
    division: str = ""
    # Extra addresses parsed from a comma-separated email cell. Outbound mail
    # uses `email`; every alias resolves back to this contact on inbound.
    email_aliases: tuple[str, ...] = ()

    @property
    def is_admin(self) -> bool:
        return self.permission == "admin"

    @property
    def can_assign(self) -> bool:
        return self.permission in {"admin", "manager"}

    @property
    def identity(self) -> str:
        """The identifier stored in owner/assignee columns."""
        return self.email or self.phone

    @property
    def all_emails(self) -> tuple[str, ...]:
        primary = (self.email,) if self.email else ()
        return primary + self.email_aliases


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    status: str
    manager: str = ""


@dataclass
class Task:
    """One row in the Tasks tab.

    A multi-step assignment is a root row (sub_step_index=0, a '[Workflow]'
    description) plus one row per sub-step. Later sub-steps start 'blocked'
    and are promoted when their predecessor completes.
    """

    task_id: str
    parent_task_id: str
    project_id: str
    project_name: str
    owner_id: str
    owner_name: str
    assignee_id: str  # comma-separated for joint tasks
    assignee_name: str
    description: str
    sub_step_index: int
    status: str
    due_date: str  # dd/mm/yyyy or ""
    external_party: str
    awaiting_response_from: str
    awaiting_response_since: str  # TS_FMT or ""
    last_reminder_sent_at: str  # TS_FMT or ""
    escalation_count: int
    completed_at: str  # TS_FMT or ""
    original_request_msg_id: str
    completion_msg_id: str
    notes: str
    row_index: int = 0  # 1-based sheet row, set by the loader for in-place updates

    @property
    def due_date_obj(self) -> datetime | None:
        return parse_sheet_date(self.due_date)

    @property
    def is_root(self) -> bool:
        return not self.parent_task_id

    @property
    def is_open(self) -> bool:
        return self.status not in TERMINAL_STATUSES


@dataclass
class IndependentTask:
    """One row in the Independent_Tasks tab: a project-less action item with a
    minute-precision deadline and a single assignee."""

    task_id: str  # I-XXXX
    creator_id: str
    creator_name: str
    assignee_id: str
    assignee_name: str
    description: str
    status: str
    deadline: str  # TS_FMT, configured timezone
    pre_deadline_reminder_minutes: str  # int as string, or ""
    last_reminder_sent_at: str
    escalation_count: int
    escalated_at: str
    completed_at: str
    original_request_msg_id: str
    completion_msg_id: str
    notes: str
    row_index: int = 0

    @property
    def deadline_obj(self) -> datetime | None:
        return parse_ts(self.deadline)

    @property
    def is_open(self) -> bool:
        return self.status not in TERMINAL_STATUSES

    @property
    def pre_deadline_minutes_int(self) -> int | None:
        """The pre-deadline reminder interval, or None if empty, non-numeric or <= 0."""
        v = (self.pre_deadline_reminder_minutes or "").strip()
        if not v:
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None


@dataclass
class Conversation:
    """One row in Task_Conversations: pairs an outbound question with the reply
    that answers it, via (participant, purpose, awaiting_reply)."""

    conv_id: str
    task_id: str
    direction: str  # outbound | inbound
    participant_id: str
    participant_role: str  # owner | assignee | external | admin
    channel: str
    msg_id: str
    timestamp: str
    purpose: str
    body_excerpt: str
    awaiting_reply: bool
    row_index: int = 0


@dataclass(frozen=True)
class IncomingMessage:
    """Channel-agnostic inbound message.

    `sender_identity` is whatever the channel identifies senders by (a
    normalised email for Gmail). `subject` is empty for channels without
    subjects. `raw` carries channel-specific extras needed on the way out
    (thread id, RFC 822 Message-ID, headers).
    """

    msg_id: str
    sender_identity: str
    sender_display_name: str
    subject: str
    body: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def language(self) -> str:
        return "ar" if is_arabic(f"{self.subject}\n{self.body}") else "en"


class Contacts:
    """Active contacts, indexed by every email address (aliases included) and phone."""

    def __init__(self, contacts: list[Contact]):
        self._all = list(contacts)
        # Later rows win on collisions, matching dict semantics.
        self._by_email: dict[str, Contact] = {}
        for c in contacts:
            for addr in c.all_emails:
                if addr:
                    self._by_email[addr] = c
        self._by_phone = {c.phone: c for c in contacts if c.phone}

    def by_email(self, e: str) -> Contact | None:
        return self._by_email.get(normalize_email(e))

    def by_phone(self, p: str) -> Contact | None:
        return self._by_phone.get(normalize_phone(p))

    def by_identity(self, identifier: str) -> Contact | None:
        return self.by_email(identifier) or self.by_phone(identifier)

    @property
    def admin(self) -> Contact | None:
        return next((c for c in self._all if c.is_admin), None)

    @property
    def all_emails(self) -> set[str]:
        return set(self._by_email.keys())

    @property
    def all_phones(self) -> set[str]:
        return set(self._by_phone.keys())

    def __iter__(self) -> Iterator[Contact]:
        return iter(self._all)
