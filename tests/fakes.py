"""In-memory fakes and fixture builders. No network anywhere."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from message_gateway.classifier import ClassificationResult, IntentResult
from message_gateway.context import HandlerContext
from message_gateway.models import Contact, Contacts, IncomingMessage, Project, Task
from message_gateway.records import (
    append_project,
    append_task,
    load_contacts,
    load_conversations,
    load_projects,
    load_tasks,
)
from message_gateway.schema import ACTIVITY_LOG, CONTACTS
from message_gateway.store import InMemoryStore

ADMIN = "admin@example.com"
MANAGER = "manager@example.com"
ALICE = "alice@example.com"
BOB = "bob@example.com"
CAROL = "carol@example.com"  # inactive


@dataclass
class Outbound:
    to: str
    subject: str
    body: str


@dataclass
class SentReply:
    msg_id: str
    body: str
    blocks: list[tuple[str, str | None]] | None


@dataclass
class FakeChannel:
    """Records everything a handler sends."""

    name: str = "fake"
    unknown_log_action: str = "UNKNOWN_FAKE"
    skip: tuple[bool, str] = (False, "")
    outbox: list[Outbound] = field(default_factory=list)
    replies: list[SentReply] = field(default_factory=list)
    processed: list[str] = field(default_factory=list)
    unknown_rows: list[tuple[str, str, str]] = field(default_factory=list)

    def lookup_sender(self, msg: IncomingMessage, contacts: Contacts) -> Contact | None:
        return contacts.by_identity(msg.sender_identity)

    def should_skip(self, msg: IncomingMessage) -> tuple[bool, str]:
        return self.skip

    def reply(self, msg: IncomingMessage, body: str, *, blocks=None) -> None:
        self.replies.append(SentReply(msg.msg_id, body, blocks))

    def send_new(self, contact: Contact, subject: str, body: str, *, blocks=None) -> None:
        self.outbox.append(Outbound(contact.identity, subject, body))

    def mark_processed(self, msg: IncomingMessage) -> None:
        self.processed.append(msg.msg_id)

    def log_unknown_row(self, store, msg: IncomingMessage, locked_reply: str, admin: Contact):
        self.unknown_rows.append((msg.sender_identity, locked_reply, admin.name))

    def to(self, address: str) -> list[Outbound]:
        return [o for o in self.outbox if o.to == address]


class FakeLLM:
    """Returns queued responses (dict -> JSON, str as-is, Exception raised)."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(self, *, model: str, system: str, user: str, max_tokens: int) -> str:
        self.calls.append({"model": model, "system": system, "user": json.loads(user)})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r)


class StubClassifier:
    """Returns a fixed classification and records each call."""

    def __init__(self, result: ClassificationResult) -> None:
        self.result = result
        self.calls: list[IncomingMessage] = []

    def classify(self, msg, sender, projects) -> ClassificationResult:
        self.calls.append(msg)
        return self.result


def one(name: str, confidence: float = 0.95, /, **extracted: Any) -> dict[str, Any]:
    """A raw single-intent payload as the model would return it."""
    return {
        "intents": [
            {"intent": name, "confidence": confidence, "extracted": extracted, "reasoning": "stub"}
        ]
    }


def intent(name: str, /, **extracted: Any) -> IntentResult:
    return IntentResult(intent=name, confidence=0.95, extracted=extracted, reasoning="stub")


def classification(*intents: IntentResult) -> ClassificationResult:
    return ClassificationResult(intents=list(intents))


def seeded_store() -> InMemoryStore:
    store = InMemoryStore.with_schema()
    store.tabs[CONTACTS.name] += [
        # phone, name, email, division, role, permission_level, active
        ["", "Admin One", ADMIN, "Ops", "Admin", "admin", "yes"],
        ["", "Manager One", MANAGER, "Ops", "Manager", "manager", "yes"],
        ["", "Alice", ALICE, "Eng", "Engineer", "employee", "yes"],
        ["", "Bob", BOB, "Eng", "Engineer", "employee", "yes"],
        ["+1 555 555 0199", "Carol", CAROL, "Eng", "Engineer", "employee", "no"],
    ]
    append_project(
        store, Project(id="P-0001", name="Project Alpha", status="active", manager="Manager One")
    )
    append_project(
        store, Project(id="P-0002", name="Project Beta", status="active", manager="Manager One")
    )
    append_project(
        store, Project(id="P-0003", name="Project Gamma", status="completed", manager="Admin One")
    )
    return store


def msg(sender: str, body: str = "", *, msg_id: str = "MSG-1", subject: str = "", raw=None):
    return IncomingMessage(
        msg_id=msg_id,
        sender_identity=sender,
        sender_display_name=sender.split("@")[0],
        subject=subject,
        body=body,
        raw=raw or {},
    )


def ctx(
    store: InMemoryStore,
    channel: FakeChannel,
    sender: str,
    intent_name: str,
    /,
    body: str = "(message)",
    *,
    msg_id: str = "MSG-1",
    subject: str = "",
    **extracted: Any,
) -> HandlerContext:
    contacts = load_contacts(store)
    contact = contacts.by_email(sender)
    assert contact is not None, f"fixture contact {sender} missing"
    return HandlerContext(
        channel=channel,
        store=store,
        msg=msg(sender, body, msg_id=msg_id, subject=subject),
        sender=contact,
        intent=intent(intent_name, **extracted),
        contacts=contacts,
        projects=load_projects(store),
    )


def seed_task(store: InMemoryStore, **fields: Any) -> Task:
    defaults: dict[str, Any] = dict(
        task_id="T-0001",
        parent_task_id="",
        project_id="P-0001",
        project_name="Project Alpha",
        owner_id=MANAGER,
        owner_name="Manager One",
        assignee_id=ALICE,
        assignee_name="Alice",
        description="(seed)",
        sub_step_index=1,
        status="assigned",
        due_date="",
        external_party="",
        awaiting_response_from="",
        awaiting_response_since="",
        last_reminder_sent_at="",
        escalation_count=0,
        completed_at="",
        original_request_msg_id="",
        completion_msg_id="",
        notes="",
    )
    defaults.update(fields)
    t = Task(**defaults)
    append_task(store, t)
    return t


def task(store: InMemoryStore, task_id: str) -> Task:
    return next(t for t in load_tasks(store) if t.task_id == task_id)


def actions(store: InMemoryStore) -> list[str]:
    return [r[1] for r in store.rows(ACTIVITY_LOG) if len(r) > 1]


def convs(store: InMemoryStore, purpose: str | None = None):
    return [c for c in load_conversations(store) if purpose is None or c.purpose == purpose]


def reply_text(context: HandlerContext) -> str:
    return context.replies.plain
