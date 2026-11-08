"""Row-level access to the store: loaders, writers, the activity log, message-ID
idempotency and conversation pairing. Column order always comes from `schema`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .clock import fmt_ts, now_local, parse_ts
from .models import (
    Contact,
    Contacts,
    Conversation,
    IndependentTask,
    Project,
    Task,
    normalize_status,
)
from .schema import (
    ACTIVITY_LOG,
    CONTACTS,
    INDEPENDENT_TASKS,
    PROJECTS,
    TASK_CONVERSATIONS,
    TASKS,
)
from .store import Store
from .text import next_conv_id, normalize_email, normalize_phone


def _pad(row: list[str], n: int) -> list[str]:
    return (row + [""] * n)[:n]


def _split_emails(cell: str) -> list[str]:
    # A comma-separated email cell lets one contact carry aliases. parseaddr
    # only sees the first address, so split before normalising.
    addresses = [normalize_email(part.strip()) for part in (cell or "").split(",")]
    return [a for a in addresses if a]


def _int_or_zero(value: str) -> int:
    try:
        return int(value) if value else 0
    except ValueError:
        return 0


# ────────────────────────────────────────────────────────────────────────────
# Loaders
# ────────────────────────────────────────────────────────────────────────────


def load_contacts(store: Store) -> Contacts:
    """Active contacts only (active=yes)."""
    rows: list[Contact] = []
    for r in store.read(CONTACTS.range_a2):
        phone, name, email_, division, role, perm, active = _pad(r, CONTACTS.num_cols)
        if active.strip().lower() != "yes":
            continue
        addresses = _split_emails(email_)
        rows.append(
            Contact(
                email=addresses[0] if addresses else "",
                phone=normalize_phone(phone),
                name=name.strip(),
                role=role.strip(),
                permission=perm.strip().lower(),
                division=division.strip(),
                email_aliases=tuple(addresses[1:]),
            )
        )
    return Contacts(rows)


def load_raw_contact_index(store: Store) -> dict[str, dict[str, Any]]:
    """{normalised email or phone -> {name, row_index, active}} over ALL rows,
    inactive included (load_contacts hides those). Used for add_contact dedupe."""
    index: dict[str, dict[str, Any]] = {}
    for offset, r in enumerate(store.read(CONTACTS.range_a2)):
        phone, name, email_, _div, _role, _perm, active = _pad(r, CONTACTS.num_cols)
        row_index = offset + 2  # header row + 1-based numbering
        norm_phone = normalize_phone(phone)
        addresses = _split_emails(email_)
        if not norm_phone and not addresses:
            continue
        meta = {
            "name": (name or "").strip(),
            "row_index": row_index,
            "active": (active or "").strip().lower(),
        }
        if norm_phone:
            index[norm_phone] = meta
        for addr in addresses:
            index[addr] = meta
    return index


def load_projects(store: Store) -> dict[str, Project]:
    out: dict[str, Project] = {}
    for r in store.read(PROJECTS.range_a2):
        name, pid, status, manager, _start, _notes = _pad(r, PROJECTS.num_cols)
        if not pid:
            continue
        out[pid.strip()] = Project(
            id=pid.strip(), name=name.strip(), status=status.strip(), manager=manager.strip()
        )
    return out


def load_tasks(store: Store) -> list[Task]:
    out: list[Task] = []
    for i, r in enumerate(store.read(TASKS.range_a2)):
        r = _pad(r, TASKS.num_cols)
        if not r[0]:
            continue
        out.append(
            Task(
                task_id=r[0].strip(),
                parent_task_id=r[1].strip(),
                project_id=r[2].strip(),
                project_name=r[3].strip(),
                owner_id=r[4].strip(),
                owner_name=r[5].strip(),
                assignee_id=r[6].strip(),
                assignee_name=r[7].strip(),
                description=r[8],
                sub_step_index=_int_or_zero(r[9]),
                status=normalize_status(r[10]),
                due_date=r[11].strip(),
                external_party=r[12].strip(),
                awaiting_response_from=r[13].strip(),
                awaiting_response_since=r[14].strip(),
                last_reminder_sent_at=r[15].strip(),
                escalation_count=_int_or_zero(r[16]),
                completed_at=r[17].strip(),
                original_request_msg_id=r[18].strip(),
                completion_msg_id=r[19].strip(),
                notes=r[20],
                row_index=i + 2,
            )
        )
    return out


def load_independent_tasks(store: Store) -> list[IndependentTask]:
    out: list[IndependentTask] = []
    for i, r in enumerate(store.read(INDEPENDENT_TASKS.range_a2)):
        r = _pad(r, INDEPENDENT_TASKS.num_cols)
        if not r[0]:
            continue
        out.append(
            IndependentTask(
                task_id=r[0].strip(),
                creator_id=r[1].strip(),
                creator_name=r[2].strip(),
                assignee_id=r[3].strip(),
                assignee_name=r[4].strip(),
                description=r[5],
                status=normalize_status(r[6]),
                deadline=r[7].strip(),
                pre_deadline_reminder_minutes=r[8].strip(),
                last_reminder_sent_at=r[9].strip(),
                escalation_count=_int_or_zero(r[10]),
                escalated_at=r[11].strip(),
                completed_at=r[12].strip(),
                original_request_msg_id=r[13].strip(),
                completion_msg_id=r[14].strip(),
                notes=r[15],
                row_index=i + 2,
            )
        )
    return out


def load_conversations(store: Store) -> list[Conversation]:
    out: list[Conversation] = []
    for i, r in enumerate(store.read(TASK_CONVERSATIONS.range_a2)):
        r = _pad(r, TASK_CONVERSATIONS.num_cols)
        if not r[0]:
            continue
        out.append(
            Conversation(
                conv_id=r[0].strip(),
                task_id=r[1].strip(),
                direction=r[2].strip(),
                participant_id=r[3].strip(),
                participant_role=r[4].strip(),
                channel=r[5].strip(),
                msg_id=r[6].strip(),
                timestamp=r[7].strip(),
                purpose=r[8].strip(),
                body_excerpt=r[9],
                awaiting_reply=r[10].strip().lower() in {"true", "yes", "1"},
                row_index=i + 2,
            )
        )
    return out


# ────────────────────────────────────────────────────────────────────────────
# Activity log and idempotency
# ────────────────────────────────────────────────────────────────────────────


def log_activity(store: Store, action: str, related: str, details: str) -> None:
    store.append(ACTIVITY_LOG.range_full, [fmt_ts(now_local()), action, related, details])


# Push notifications are delivered at least once and the service may run as
# several instances, so two drains can fetch the same message before either
# has applied the processed label. Recent Activity_Log rows carrying the
# message ID are the dedupe key.
MSG_ID_DEDUP_LOOKBACK_HOURS = 24


def is_msg_id_processed(
    store: Store, msg_id: str, lookback_hours: int = MSG_ID_DEDUP_LOOKBACK_HOURS
) -> bool:
    """True if Activity_Log shows this msg_id was dispatched within the window.

    Matches any of: 'Message dispatch started' (written before classification),
    '<channel> message processed' (success) or 'ERROR' (handled failure), with
    'msg_id:<id>' in the related or details column.
    """
    if not msg_id:
        return False
    threshold = now_local() - timedelta(hours=lookback_hours)
    needle = f"msg_id:{msg_id}"
    rows = store.read(f"{ACTIVITY_LOG.name}!A:D")
    for r in reversed(rows):
        if not r:
            continue
        ts = parse_ts(r[0]) if r[0] else None
        if ts and ts < threshold:
            # Append-only and chronological: nothing older can match.
            break
        action = r[1] if len(r) > 1 else ""
        related = r[2] if len(r) > 2 else ""
        details = r[3] if len(r) > 3 else ""
        action_match = (
            action == "Message dispatch started"
            or action.endswith("message processed")
            or action == "ERROR"
        )
        if action_match and (needle in related or needle in details):
            return True
    return False


# ────────────────────────────────────────────────────────────────────────────
# Writers
# ────────────────────────────────────────────────────────────────────────────


def task_to_row(t: Task) -> list[str]:
    return [
        t.task_id, t.parent_task_id, t.project_id, t.project_name,
        t.owner_id, t.owner_name, t.assignee_id, t.assignee_name,
        t.description, str(t.sub_step_index), t.status, t.due_date,
        t.external_party, t.awaiting_response_from, t.awaiting_response_since,
        t.last_reminder_sent_at, str(t.escalation_count), t.completed_at,
        t.original_request_msg_id, t.completion_msg_id, t.notes,
    ]  # fmt: skip


def append_task(store: Store, task: Task) -> None:
    store.append(TASKS.range_full, task_to_row(task))


def project_to_row(p: Project) -> list[str]:
    return [p.name, p.id, p.status, p.manager, "", ""]  # Start_date, Notes empty


def append_project(store: Store, project: Project) -> None:
    store.append(PROJECTS.range_full, project_to_row(project))


def append_contact(store: Store, contact_row: list[str]) -> None:
    """Append a raw row in CONTACTS column order:
    [phone, name, email, division, role, permission_level, active]."""
    store.append(CONTACTS.range_full, contact_row)


def independent_task_to_row(t: IndependentTask) -> list[str]:
    return [
        t.task_id, t.creator_id, t.creator_name,
        t.assignee_id, t.assignee_name,
        t.description, t.status, t.deadline,
        t.pre_deadline_reminder_minutes, t.last_reminder_sent_at,
        str(t.escalation_count), t.escalated_at, t.completed_at,
        t.original_request_msg_id, t.completion_msg_id, t.notes,
    ]  # fmt: skip


def append_independent_task(store: Store, task: IndependentTask) -> None:
    store.append(INDEPENDENT_TASKS.range_full, independent_task_to_row(task))


def _update_fields(
    store: Store, tab_name: str, col_index: dict[str, str], obj: Any, fields
) -> None:
    if not obj.row_index:
        raise ValueError(f"{obj.task_id} has no row_index; reload before update.")
    updates = []
    for name, value in fields.items():
        if name not in col_index:
            raise KeyError(f"Unknown field for {tab_name}: {name}")
        updates.append((f"{tab_name}!{col_index[name]}{obj.row_index}", [[str(value)]]))
        setattr(obj, name, int(value) if name == "escalation_count" else value)
    store.batch_update(updates)


def update_task_fields(store: Store, task: Task, **fields: Any) -> None:
    """In-place update of one task: mutates `task` and writes one cell per field."""
    _update_fields(store, TASKS.name, TASKS.col_index, task, fields)


def update_independent_task_fields(store: Store, task: IndependentTask, **fields: Any) -> None:
    _update_fields(store, INDEPENDENT_TASKS.name, INDEPENDENT_TASKS.col_index, task, fields)


# ────────────────────────────────────────────────────────────────────────────
# Conversation pairing
# ────────────────────────────────────────────────────────────────────────────


def append_conversation(
    store: Store,
    *,
    task_id: str,
    direction: str,
    participant_id: str,
    participant_role: str,
    channel: str,
    msg_id: str,
    purpose: str,
    body: str,
    awaiting_reply: bool,
    truncate: bool = True,
) -> Conversation:
    existing_ids = [r[0] for r in store.read(f"{TASK_CONVERSATIONS.name}!A2:A") if r]
    # body_excerpt is normally a 200-char preview; parked requests (e.g.
    # register_project_request) carry the full JSON payload they replay on YES.
    excerpt = (body or "")[:200] if truncate else (body or "")
    conv = Conversation(
        conv_id=next_conv_id(existing_ids),
        task_id=task_id,
        direction=direction,
        participant_id=participant_id,
        participant_role=participant_role,
        channel=channel,
        msg_id=msg_id,
        timestamp=fmt_ts(now_local()),
        purpose=purpose,
        body_excerpt=excerpt,
        awaiting_reply=awaiting_reply,
    )
    store.append(
        TASK_CONVERSATIONS.range_full,
        [
            conv.conv_id, conv.task_id, conv.direction, conv.participant_id,
            conv.participant_role, conv.channel, conv.msg_id, conv.timestamp,
            conv.purpose, conv.body_excerpt, "true" if awaiting_reply else "false",
        ],
    )  # fmt: skip
    return conv


def mark_conversation_replied(store: Store, conv: Conversation) -> None:
    if not conv.row_index:
        return
    col = TASK_CONVERSATIONS.col_index["awaiting_reply"]
    store.update(f"{TASK_CONVERSATIONS.name}!{col}{conv.row_index}", [["false"]])
    conv.awaiting_reply = False


def latest_awaiting_for(
    conversations: list[Conversation], participant_id: str, purpose: str | None = None
) -> Conversation | None:
    """Most recent outbound row to this participant that still awaits a reply."""
    candidates = [
        c
        for c in conversations
        if c.awaiting_reply
        and c.direction == "outbound"
        and c.participant_id == participant_id
        and (purpose is None or c.purpose == purpose)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.timestamp)
