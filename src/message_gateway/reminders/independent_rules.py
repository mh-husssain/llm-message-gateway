"""Reminder rules for independent (I-XXXX) tasks (run by /cron-fast).

These items have minute-precision deadlines, so the clock is shorter than for
project tasks: optional reminders before the deadline (I), a chaser every 30
minutes after it (I2), and after two unanswered chasers a single notice to
the creator, after which reminders stop (J).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..clock import fmt_ts, parse_ts
from ..models import (
    STATUS_ASSIGNED,
    STATUS_AWAITING_ASSIGNEE_COMPLETION,
    STATUS_ESCALATED,
    TERMINAL_STATUSES,
    Contacts,
    IndependentTask,
)
from ..text import subject_line
from ..workflow import lookup_contact
from .base import RuleResult

# Unanswered post-deadline chasers before the creator is told (rule J).
INDEPENDENT_MAX_PINGS = 2
INDEPENDENT_LATE_PING_INTERVAL = timedelta(minutes=30)
INDEPENDENT_ESCALATED_MARKER = "INDEPENDENT_ESCALATED_TO_CREATOR"


def _minutes(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds() // 60))


def rule_i_before_deadline(
    task: IndependentTask, contacts: Contacts, now: datetime
) -> RuleResult | None:
    """Reminds the assignee every N minutes before the deadline, only when the
    manager asked for an interval."""
    if task.status != STATUS_ASSIGNED:
        return None
    deadline = task.deadline_obj
    if not deadline or deadline <= now:
        return None
    interval_min = task.pre_deadline_minutes_int
    if not interval_min:
        return None
    if task.last_reminder_sent_at:
        last = parse_ts(task.last_reminder_sent_at)
        if last and (now - last) < timedelta(minutes=interval_min):
            return None
    assignee = lookup_contact(task.assignee_id, contacts)
    if not assignee:
        return None
    minutes_left = _minutes(deadline - now)
    return RuleResult(
        rule="I",
        task=task,
        recipient=assignee,
        subject=subject_line(f"Reminder: {task.task_id} due {task.deadline}"),
        body_en=(
            f"Reminder, {task.assignee_name}: '{task.description}' is due "
            f"{task.deadline} (in ~{minutes_left} minute(s)). "
            f"Reply YES once it's done. Reference: {task.task_id}."
        ),
        body_ar=(
            f"تذكير، {task.assignee_name}: '{task.description}' مستحقة في "
            f"{task.deadline} (خلال نحو {minutes_left} دقيقة). "
            f"أجب YES عند الإنتهاء. المرجع: {task.task_id}."
        ),
        field_updates={"last_reminder_sent_at": fmt_ts(now)},
        status_after=task.status,
        conversation_purpose="pre_deadline_reminder",
        conversation_awaiting_reply=False,
        conversation_role="assignee",
        conversation_participant=task.assignee_id,
    )


def rule_i2_after_deadline(
    task: IndependentTask, contacts: Contacts, now: datetime
) -> RuleResult | None:
    """After the deadline, chases the assignee every 30 minutes, up to
    INDEPENDENT_MAX_PINGS times; the first chaser marks the task overdue."""
    if task.status not in {STATUS_ASSIGNED, STATUS_AWAITING_ASSIGNEE_COMPLETION}:
        return None
    deadline = task.deadline_obj
    if not deadline or deadline > now:
        return None
    if task.escalation_count >= INDEPENDENT_MAX_PINGS:
        return None  # rule J takes over
    if task.last_reminder_sent_at:
        last = parse_ts(task.last_reminder_sent_at)
        if last and (now - last) < INDEPENDENT_LATE_PING_INTERVAL:
            return None
    assignee = lookup_contact(task.assignee_id, contacts)
    if not assignee:
        return None
    minutes_late = _minutes(now - deadline)
    new_status = (
        STATUS_AWAITING_ASSIGNEE_COMPLETION if task.status == STATUS_ASSIGNED else task.status
    )
    return RuleResult(
        rule="I2",
        task=task,
        recipient=assignee,
        subject=subject_line(f"{task.task_id}: {minutes_late} min past due"),
        body_en=(
            f"{task.assignee_name}, '{task.description}' was due {task.deadline} "
            f"(~{minutes_late} minute(s) ago). Reply YES if it's done, or send a "
            f"new ETA. Reference: {task.task_id}."
        ),
        body_ar=(
            f"{task.assignee_name}، '{task.description}' كانت مستحقة في "
            f"{task.deadline} (منذ نحو {minutes_late} دقيقة). أجب YES إذا تم، "
            f"أو أرسل موعدا جديدا. المرجع: {task.task_id}."
        ),
        field_updates={
            "last_reminder_sent_at": fmt_ts(now),
            "escalation_count": task.escalation_count + 1,
            "status": new_status,
        },
        status_after=new_status,
        conversation_purpose="late_completion_check",
        conversation_awaiting_reply=True,
        conversation_role="assignee",
        conversation_participant=task.assignee_id,
    )


def rule_j_creator_notice(
    task: IndependentTask, contacts: Contacts, now: datetime
) -> RuleResult | None:
    """Once the chasers are used up, tells the creator (not the assignee) once,
    marks the task escalated and stops reminding."""
    if task.status in TERMINAL_STATUSES:
        return None
    if task.escalation_count < INDEPENDENT_MAX_PINGS:
        return None
    if task.escalated_at or INDEPENDENT_ESCALATED_MARKER in (task.notes or ""):
        return None
    creator = lookup_contact(task.creator_id, contacts)
    if not creator:
        return None
    deadline = task.deadline_obj
    minutes_late = _minutes(now - deadline) if deadline else 0
    desc = task.description or ""
    short_desc = desc[:60].rstrip() + "…" if len(desc) > 60 else desc
    return RuleResult(
        rule="J",
        task=task,
        recipient=creator,
        subject=subject_line(f"Overdue, no response: {short_desc}"),
        body_en=(
            "Heads up — the following task is overdue and the assignee hasn't responded:\n\n"
            f"  • Task: {task.description}\n"
            f"  • Assignee: {task.assignee_name}\n"
            f"  • Was due: {task.deadline} ({minutes_late} min ago)\n"
            f"  • Reminders sent: {task.escalation_count}\n\n"
            "I've stopped pinging — over to you to follow up directly.\n\n"
            f"Reference: [{task.task_id}]."
        ),
        body_ar=(
            "للعلم — المهمة التالية متأخرة ولم يستجب المسند إليه:\n\n"
            f"  • المهمة: {task.description}\n"
            f"  • المسند إليه: {task.assignee_name}\n"
            f"  • كانت مستحقة: {task.deadline} (منذ {minutes_late} دقيقة)\n"
            f"  • عدد التذكيرات: {task.escalation_count}\n\n"
            "توقفت عن المتابعة — الأمر إليك لمتابعته مباشرة.\n\n"
            f"المرجع: [{task.task_id}]."
        ),
        field_updates={
            "status": STATUS_ESCALATED,
            "escalated_at": fmt_ts(now),
            "notes": (
                (task.notes or "") + f" | {INDEPENDENT_ESCALATED_MARKER} on {fmt_ts(now)}"
            ).strip(" |"),
        },
        status_after=STATUS_ESCALATED,
        conversation_purpose="independent_creator_notice",
        conversation_awaiting_reply=False,
        conversation_role="owner",  # the creator plays the owner role
        conversation_participant=task.creator_id,
    )


# I and I2 are mutually exclusive (the deadline is either ahead or past). J
# runs last so an I2 count bump in the same tick is visible to it.
INDEPENDENT_RULES = (
    rule_i_before_deadline,
    rule_i2_after_deadline,
    rule_j_creator_notice,
)
