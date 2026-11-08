"""Task lookup, permissions and sequencing.

Pure functions over loaded rows, plus the two writers that move a workflow
forward (promoting the next sub-step).
"""

from __future__ import annotations

from .clock import fmt_ts, now_local
from .models import (
    STATUS_ASSIGNED,
    STATUS_BLOCKED_BY_PREDECESSOR,
    STATUS_COMPLETED,
    TERMINAL_STATUSES,
    Contact,
    Contacts,
    IndependentTask,
    Task,
)
from .records import update_task_fields
from .store import Store

# Notes markers written by the reminder engine (`message_gateway.reminders`)
# so a one-shot owner notice fires once per cycle. Handlers that restart a
# task's schedule clear them via `clear_late_completion_state`.
OWNER_NO_UPDATE_MARKER = "OWNER_NO_UPDATE_NOTICE_SENT"
LATE_OWNER_NOTICE_MARKER = "LATE_OWNER_NOTICE_SENT"


def resolve_assignee(name: str, contacts: Contacts) -> tuple[Contact | None, str]:
    """(contact, error_reason) via case-insensitive substring match on names.

    error_reason is '' on success, else 'no_name' | 'not_found' | 'ambiguous'.
    """
    needle = (name or "").strip().lower()
    if not needle:
        return None, "no_name"
    matches = [c for c in contacts if needle in c.name.lower()]
    if not matches:
        return None, "not_found"
    if len(matches) > 1:
        return None, "ambiguous"
    return matches[0], ""


def split_assignee_ids(assignee_id_field: str) -> list[str]:
    """Joint tasks store comma-separated identifiers in one cell."""
    return [a.strip() for a in (assignee_id_field or "").split(",") if a.strip()]


def is_sender_assignee(task: Task, sender_id: str) -> bool:
    if not sender_id:
        return False
    return sender_id in split_assignee_ids(task.assignee_id)


def find_task_by_id(tasks: list[Task], task_id: str) -> Task | None:
    """Exact T-ID lookup across all assignees (callers gate permission)."""
    needle = (task_id or "").strip()
    if not needle:
        return None
    return next((t for t in tasks if t.task_id == needle), None)


def find_independent_task_by_id(
    tasks: list[IndependentTask], task_id: str
) -> IndependentTask | None:
    needle = (task_id or "").strip()
    if not needle:
        return None
    return next((t for t in tasks if t.task_id == needle), None)


def can_act_on_task(sender: Contact, task: Task) -> bool:
    """Task-scoped permission: admins and managers always; employees only on
    tasks they own or are assigned to."""
    if sender.is_admin or sender.can_assign:
        return True
    sender_id = sender.identity
    if not sender_id:
        return False
    if sender_id == task.owner_id:
        return True
    return is_sender_assignee(task, sender_id)


def find_task_by_hint(
    tasks: list[Task],
    assignee_id: str,
    task_id_hint: str | None = None,
    description_hint: str | None = None,
    statuses: set[str] | None = None,
    restrict_to_assignee: bool = True,
) -> tuple[Task | None, str]:
    """(task, error_reason) over the sender's non-terminal tasks by default.

    An exact task_id_hint wins; otherwise a case-insensitive substring match on
    the description; otherwise the single candidate if there is exactly one.
    With restrict_to_assignee=False the caller must supply a hint.
    """
    if restrict_to_assignee:
        pool = [t for t in tasks if is_sender_assignee(t, assignee_id)]
    else:
        pool = list(tasks)
    if statuses is None:
        pool = [t for t in pool if t.status not in TERMINAL_STATUSES]
    else:
        pool = [t for t in pool if t.status in statuses]
    if not pool:
        return None, "no_open_tasks"
    if task_id_hint:
        match = next((t for t in pool if t.task_id == task_id_hint.strip()), None)
        return (match, "" if match else "id_not_found")
    if description_hint:
        needle = description_hint.lower()
        matches = [t for t in pool if needle in t.description.lower()]
        if len(matches) == 1:
            return matches[0], ""
        if len(matches) > 1:
            return None, "ambiguous"
        return None, "description_not_found"
    if not restrict_to_assignee:
        return None, "ambiguous"
    if len(pool) == 1:
        return pool[0], ""
    return None, "ambiguous"


def promote_next_sibling(store: Store, completed: Task, all_tasks: list[Task]) -> Task | None:
    """When a sub-step finishes, unblock the next one (same parent,
    sub_step_index + 1) and return it."""
    if not completed.parent_task_id:
        return None
    siblings = [
        t
        for t in all_tasks
        if t.parent_task_id == completed.parent_task_id
        and t.sub_step_index == completed.sub_step_index + 1
    ]
    if not siblings:
        return None
    nxt = siblings[0]
    if nxt.status != STATUS_BLOCKED_BY_PREDECESSOR:
        return nxt
    update_task_fields(
        store,
        nxt,
        status=STATUS_ASSIGNED,
        notes=(nxt.notes + f" | promoted from blocked at {fmt_ts(now_local())}").strip(" |"),
    )
    return nxt


def all_siblings_complete(parent_id: str, tasks: list[Task]) -> bool:
    children = [t for t in tasks if t.parent_task_id == parent_id]
    if not children:
        return False
    return all(t.status == STATUS_COMPLETED for t in children)


def lookup_contact(identifier: str, contacts: Contacts) -> Contact | None:
    """First resolvable contact in a (possibly comma-joined) identifier field."""
    if not identifier:
        return None
    candidates = split_assignee_ids(identifier) if "," in identifier else [identifier]
    for cand in candidates:
        c = contacts.by_identity(cand)
        if c:
            return c
    return None


def lookup_assignees(identifier: str, contacts: Contacts) -> list[Contact]:
    """Every resolvable contact in a (possibly comma-joined) assignee field."""
    if not identifier:
        return []
    out: list[Contact] = []
    for cand in split_assignee_ids(identifier):
        c = contacts.by_identity(cand)
        if c and c not in out:
            out.append(c)
    return out


def clear_late_completion_state(task: Task) -> dict[str, object]:
    """Field updates that restart a task's reminder cycle after a deliberate
    schedule change (e.g. a manager extension). Splat into update_task_fields.

    Resets escalation_count and last_reminder_sent_at, and strips the
    reminder markers from notes so a future late cycle starts clean.
    """
    notes = task.notes or ""
    for marker in (LATE_OWNER_NOTICE_MARKER, OWNER_NO_UPDATE_MARKER):
        notes = notes.replace(" | " + marker, "").replace(marker, "")
    return {
        "escalation_count": 0,
        "last_reminder_sent_at": "",
        "notes": notes.strip(" |"),
    }
