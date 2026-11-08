"""Owner approvals: schedule changes proposed by an assignee wait for the task
owner's YES or NO.

The pending proposal is stored in the task's notes as
`PROPOSAL:<kind>:<json>` (kind is delay | external_eta | cancel), the task
moves to awaiting_owner_confirmation, and an outbound conversation row with
awaiting_reply=true lets the owner's next YES/NO be paired without the model.
"""

from __future__ import annotations

import json
from typing import Any

from .channel import Channel
from .clock import fmt_ts, format_display_date, now_local
from .models import (
    STATUS_ASSIGNED,
    STATUS_AWAITING_ASSIGNEE_ETA,
    STATUS_AWAITING_EXTERNAL_COMPLETION,
    STATUS_AWAITING_OWNER_CONFIRMATION,
    STATUS_CANCELLED,
    STATUS_IN_PROGRESS,
    Contacts,
    Conversation,
    Task,
)
from .records import append_conversation, log_activity, update_task_fields
from .store import Store
from .text import ack, subject_line
from .workflow import (
    LATE_OWNER_NOTICE_MARKER,
    OWNER_NO_UPDATE_MARKER,
    lookup_assignees,
    lookup_contact,
)

PROPOSAL_PREFIX = "PROPOSAL:"


def encode_proposal(kind: str, payload: dict[str, Any]) -> str:
    return f"{PROPOSAL_PREFIX}{kind}:{json.dumps(payload, ensure_ascii=False)}"


def _json_object_end(rest: str) -> int:
    """Index just past the first balanced {...} in `rest`, or -1."""
    depth = 0
    for i, ch in enumerate(rest):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def parse_proposal(notes: str) -> tuple[str, dict[str, Any]] | None:
    """Return (kind, payload) for the most recent PROPOSAL marker, or None."""
    if PROPOSAL_PREFIX not in notes:
        return None
    after = notes.split(PROPOSAL_PREFIX)[-1]
    colon = after.find(":")
    if colon < 0:
        return None
    kind = after[:colon].strip()
    rest = after[colon + 1 :]
    end = _json_object_end(rest)
    if end < 0:
        return None
    try:
        payload = json.loads(rest[:end])
    except json.JSONDecodeError:
        return None
    return kind, payload


def strip_proposal(notes: str) -> str:
    """Remove the most recent PROPOSAL marker from notes, keeping the rest."""
    if PROPOSAL_PREFIX not in notes:
        return notes
    head, _, after = notes.rpartition(PROPOSAL_PREFIX)
    colon = after.find(":")
    if colon < 0:
        return head.rstrip(" |")
    rest = after[colon + 1 :]
    end = _json_object_end(rest)
    tail = rest[end:].lstrip(" |") if end > 0 else ""
    combined = head.rstrip(" |")
    if tail:
        combined = (combined + " | " + tail) if combined else tail
    return combined


def format_proposal(task: Task, kind: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    """(body_en, body_ar, subject) for the owner-facing proposal."""
    if kind == "delay":
        new_due = payload.get("new_due", "")
        reason = payload.get("reason", "")
        subject = subject_line(f"{task.task_id}: extension request — needs your OK")
        body_en = (
            f"{task.assignee_name} asked for more time on {task.task_id} "
            f"({task.description}).\n"
            + (f"Reason: {reason}\n" if reason else "")
            + f"Proposed new due: {new_due}.\n\n"
            "Reply YES to accept, NO to reject."
        )
        body_ar = (
            f"{task.assignee_name} طلب وقتا إضافيا على {task.task_id} "
            f"({task.description}).\n"
            + (f"السبب: {reason}\n" if reason else "")
            + f"الموعد الجديد المقترح: {new_due}.\n\n"
            "أجب YES للموافقة أو NO للرفض."
        )
        return body_en, body_ar, subject

    if kind == "external_eta":
        new_due = payload.get("new_due", "")
        eta_days = payload.get("eta_days", "")
        external = payload.get("external_party", task.external_party or "external party")
        subject = subject_line(f"{task.task_id}: {external} timeline — needs your OK")
        body_en = (
            f"{task.assignee_name} reports that {external} needs {eta_days} day(s) "
            f"to complete {task.task_id} ({task.description}).\n"
            f"Proposed new due: {new_due}.\n\n"
            "Reply YES to accept, NO to reject."
        )
        body_ar = (
            f"{task.assignee_name} أبلغ بأن {external} يحتاج {eta_days} يوم/أيام "
            f"لإكمال {task.task_id} ({task.description}).\n"
            f"الموعد الجديد المقترح: {new_due}.\n\n"
            "أجب YES للموافقة أو NO للرفض."
        )
        return body_en, body_ar, subject

    if kind == "cancel":
        reason = payload.get("reason", "")
        subject = subject_line(f"{task.task_id}: cancel request")
        body_en = (
            f"Cancel request on {task.task_id} ({task.description}).\n"
            + (f"Reason: {reason}\n" if reason else "")
            + "Reply YES to cancel, NO to keep."
        )
        body_ar = (
            f"طلب إلغاء {task.task_id} ({task.description}).\n"
            + (f"السبب: {reason}\n" if reason else "")
            + "أجب YES للإلغاء أو NO للإبقاء."
        )
        return body_en, body_ar, subject

    raise ValueError(f"Unknown proposal kind: {kind!r}")


def propose_to_owner(
    *,
    store: Store,
    channel: Channel,
    task: Task,
    contacts: Contacts,
    kind: str,
    payload: dict[str, Any],
    lang: str,
) -> Conversation | None:
    """Stash the proposal, flip the task to awaiting_owner_confirmation,
    message the owner and record an awaiting conversation row.

    Returns the conversation, or None if the owner is not in Contacts.
    """
    owner = contacts.by_identity(task.owner_id)
    if owner is None:
        log_activity(
            store, "Proposal failed", task.task_id, f"Owner {task.owner_id} not found in Contacts."
        )
        return None

    body_en, body_ar, subject = format_proposal(task, kind, payload)
    new_notes = (task.notes + " | " + encode_proposal(kind, payload)).strip(" |")
    update_task_fields(store, task, status=STATUS_AWAITING_OWNER_CONFIRMATION, notes=new_notes)
    channel.send_new(owner, subject=subject, body=ack(body_en, body_ar, lang))
    return append_conversation(
        store,
        task_id=task.task_id,
        direction="outbound",
        participant_id=task.owner_id,
        participant_role="owner",
        channel=channel.name,
        msg_id="",
        purpose="owner_confirmation_request",
        body=body_en,
        awaiting_reply=True,
    )


def apply_owner_response(
    *,
    store: Store,
    channel: Channel,
    task: Task,
    confirmed: bool,
    contacts: Contacts,
    lang: str,
) -> Task | None:
    """Apply (YES) or revert (NO) the proposal stored in task.notes and notify
    the assignee(s). Returns the task, or None if no proposal was found."""
    proposal = parse_proposal(task.notes)
    if proposal is None:
        log_activity(
            store,
            "Proposal apply failed",
            task.task_id,
            "No PROPOSAL: marker in notes; cannot apply owner response.",
        )
        return None
    kind, payload = proposal
    stripped = strip_proposal(task.notes)
    stamp = fmt_ts(now_local())

    if confirmed:
        if kind == "delay":
            new_due = payload.get("new_due", "")
            # An approved delay starts a clean reminder cycle on the new date.
            cleaned_notes = stripped or ""
            for marker in (LATE_OWNER_NOTICE_MARKER, OWNER_NO_UPDATE_MARKER):
                cleaned_notes = cleaned_notes.replace(" | " + marker, "").replace(marker, "")
            update_task_fields(
                store,
                task,
                status=STATUS_ASSIGNED,
                due_date=new_due,
                last_reminder_sent_at="",
                escalation_count=0,
                notes=(
                    cleaned_notes.strip(" |") + f" | DELAY APPROVED on {stamp}: new due {new_due}"
                ).strip(" |"),
            )
        elif kind == "external_eta":
            new_due = payload.get("new_due", "")
            update_task_fields(
                store,
                task,
                status=STATUS_AWAITING_EXTERNAL_COMPLETION,
                due_date=new_due,
                last_reminder_sent_at="",
                escalation_count=0,
                awaiting_response_from="",
                awaiting_response_since="",
                notes=(stripped + f" | EXTERNAL ETA APPROVED on {stamp}: due {new_due}").strip(
                    " |"
                ),
            )
        elif kind == "cancel":
            update_task_fields(
                store,
                task,
                status=STATUS_CANCELLED,
                notes=(stripped + f" | CANCELLED on {stamp}").strip(" |"),
            )
    else:
        revert = STATUS_AWAITING_ASSIGNEE_ETA if kind == "external_eta" else STATUS_IN_PROGRESS
        update_task_fields(
            store,
            task,
            status=revert,
            notes=(stripped + f" | {kind.upper()} REJECTED on {stamp}: reverted to {revert}").strip(
                " |"
            ),
        )

    single = lookup_contact(task.assignee_id, contacts)
    assignees = lookup_assignees(task.assignee_id, contacts) or ([single] if single else [])
    for assignee in assignees:
        if confirmed:
            display_due = format_display_date(task.due_date) if task.due_date else ""
            body_en = f"The owner approved the change on {task.task_id}. " + (
                f"New due: {display_due}." if display_due else ""
            )
            body_ar = f"وافق المسؤول على التغيير على {task.task_id}. " + (
                f"الموعد الجديد: {display_due}." if display_due else ""
            )
        else:
            body_en = (
                f"The owner declined the change on {task.task_id}. "
                "Original schedule still applies; please re-plan or reach out."
            )
            body_ar = (
                f"رفض المسؤول التغيير على {task.task_id}. "
                "الجدول الأصلي ساري؛ يرجى إعادة التخطيط أو التواصل."
            )
        channel.send_new(
            assignee,
            subject=subject_line(f"{task.task_id}: owner decision"),
            body=ack(body_en, body_ar, lang),
        )

    log_activity(
        store,
        "Owner decision applied",
        task.task_id,
        f"kind={kind} confirmed={confirmed} new_status={task.status}",
    )
    return task
