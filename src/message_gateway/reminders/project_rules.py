"""Reminder and escalation rules for project tasks (run by /cron).

Each rule is a pure function `(task, contacts, now) -> RuleResult | None`.
Guards against firing twice:
  - daily rules (A, B, C, E, G) skip a task already reminded on the same
    local date (`last_reminder_sent_at`);
  - one-shot notices (D, H) stamp a marker in the task's notes;
  - the cap (F) stamps a marker and moves the task to a terminal status.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..clock import fmt_ts, format_display_date, parse_ts, same_local_date
from ..models import (
    STATUS_ASSIGNED,
    STATUS_AWAITING_ASSIGNEE_COMPLETION,
    STATUS_AWAITING_ASSIGNEE_ETA,
    STATUS_AWAITING_EXTERNAL_COMPLETION,
    STATUS_ESCALATED,
    STATUS_IN_PROGRESS,
    TERMINAL_STATUSES,
    Contacts,
    Task,
)
from ..text import subject_line
from ..workflow import LATE_OWNER_NOTICE_MARKER, OWNER_NO_UPDATE_MARKER, lookup_contact
from .base import RuleResult

# Reminder cycles after which a task is handed to the admin (rule F).
MAX_ESCALATION = 5
ESCALATED_MARKER = "ESCALATED"
# Minimum gap between a hand-off and the first "any update?" ping (rule C).
ETA_PING_MIN_GAP = timedelta(hours=20)


def rule_a_day_before(task: Task, contacts: Contacts, now: datetime) -> RuleResult | None:
    """Reminds the assignee the day before the due date."""
    if task.status not in {STATUS_ASSIGNED, STATUS_IN_PROGRESS}:
        return None
    if task.sub_step_index == 0:  # workflow roots carry no actionable due date
        return None
    due = task.due_date_obj
    if not due or (due.date() - now.date()).days != 1:
        return None
    if same_local_date(task.last_reminder_sent_at, now):
        return None
    assignee = lookup_contact(task.assignee_id, contacts)
    if not assignee:
        return None
    display_due = format_display_date(task.due_date)
    return RuleResult(
        rule="A",
        task=task,
        recipient=assignee,
        subject=subject_line(f"Reminder: {task.task_id} due tomorrow"),
        body_en=(
            f"Reminder, {task.assignee_name}: '{task.description}' is due tomorrow "
            f"({display_due}). Reference: {task.task_id}."
        ),
        body_ar=(
            f"تذكير، {task.assignee_name}: '{task.description}' مستحقة غدا "
            f"({display_due}). المرجع: {task.task_id}."
        ),
        field_updates={"last_reminder_sent_at": fmt_ts(now)},
        status_after=task.status,
        conversation_purpose="t-1_reminder",
        conversation_awaiting_reply=False,
        conversation_role="assignee",
        conversation_participant=task.assignee_id,
    )


def rule_b_due_today(task: Task, contacts: Contacts, now: datetime) -> RuleResult | None:
    """On the due date, asks the assignee whether the work was submitted and
    moves the task to awaiting_assignee_completion. The YES/NO answers the
    `due_check` conversation it opens."""
    if task.status not in {STATUS_ASSIGNED, STATUS_IN_PROGRESS}:
        return None
    if task.sub_step_index == 0:
        return None
    due = task.due_date_obj
    if not due or due.date() != now.date():
        return None
    if same_local_date(task.last_reminder_sent_at, now):
        return None
    assignee = lookup_contact(task.assignee_id, contacts)
    if not assignee:
        return None
    return RuleResult(
        rule="B",
        task=task,
        recipient=assignee,
        subject=subject_line(f"{task.task_id}: due today — submitted?"),
        body_en=(
            f"{task.assignee_name}, has '{task.description}' been submitted today? "
            "Reply YES once it's done, or let me know if you need more time. "
            f"Reference: {task.task_id}."
        ),
        body_ar=(
            f"{task.assignee_name}، هل تم تسليم '{task.description}' اليوم؟ "
            "أجب YES عند الإنتهاء، أو أخبرني إن احتجت وقتا إضافيا. "
            f"المرجع: {task.task_id}."
        ),
        field_updates={
            "status": STATUS_AWAITING_ASSIGNEE_COMPLETION,
            "last_reminder_sent_at": fmt_ts(now),
        },
        status_after=STATUS_AWAITING_ASSIGNEE_COMPLETION,
        conversation_purpose="due_check",
        conversation_awaiting_reply=True,
        conversation_role="assignee",
        conversation_participant=task.assignee_id,
    )


def rule_c_eta_chaser(task: Task, contacts: Contacts, now: datetime) -> RuleResult | None:
    """While a hand-off awaits the outside party's timeline, asks the assignee
    for an update once a day (from the day after the hand-off) and counts the
    reminder."""
    if task.status != STATUS_AWAITING_ASSIGNEE_ETA:
        return None
    if task.awaiting_response_since:
        since = parse_ts(task.awaiting_response_since)
        if since and (now - since) < ETA_PING_MIN_GAP:
            return None
    if same_local_date(task.last_reminder_sent_at, now):
        return None
    assignee = lookup_contact(task.assignee_id, contacts)
    if not assignee:
        return None
    ext = task.external_party or "the external party"
    return RuleResult(
        rule="C",
        task=task,
        recipient=assignee,
        subject=subject_line(f"{task.task_id}: ETA from {ext}?"),
        body_en=(
            f"{task.assignee_name}, any update from {ext} on '{task.description}'? "
            f"Reference: {task.task_id}."
        ),
        body_ar=(
            f"{task.assignee_name}، أي تحديث من {ext} على '{task.description}'؟ "
            f"المرجع: {task.task_id}."
        ),
        field_updates={
            "last_reminder_sent_at": fmt_ts(now),
            "escalation_count": task.escalation_count + 1,
        },
        status_after=task.status,
        conversation_purpose="eta_request",
        conversation_awaiting_reply=True,
        conversation_role="assignee",
        conversation_participant=task.assignee_id,
    )


def rule_d_eta_owner_notice(task: Task, contacts: Contacts, now: datetime) -> RuleResult | None:
    """After two unanswered timeline chasers, tells the task owner once per cycle."""
    if task.status != STATUS_AWAITING_ASSIGNEE_ETA:
        return None
    if task.escalation_count < 2:
        return None
    if OWNER_NO_UPDATE_MARKER in task.notes:
        return None
    owner = lookup_contact(task.owner_id, contacts)
    if not owner:
        return None
    ext = task.external_party or "the external party"
    return RuleResult(
        rule="D",
        task=task,
        recipient=owner,
        subject=subject_line(f"{task.task_id}: no ETA from {ext} yet"),
        body_en=(
            f"FYI: still no ETA from {ext} on {task.task_id} ({task.description}). "
            f"{task.assignee_name} hasn't responded for {task.escalation_count} day(s). "
            "I'll keep pinging."
        ),
        body_ar=(
            f"للعلم: لا تزال لا توجد مدة من {ext} لـ {task.task_id} ({task.description}). "
            f"{task.assignee_name} لم يرد منذ {task.escalation_count} يوم/أيام. "
            "سأستمر بالمتابعة."
        ),
        field_updates={"notes": (task.notes + " | " + OWNER_NO_UPDATE_MARKER).strip(" |")},
        status_after=task.status,
        conversation_purpose="delay_propagation",
        conversation_awaiting_reply=False,
        conversation_role="owner",
        conversation_participant=task.owner_id,
    )


def rule_e_external_due_soon(task: Task, contacts: Contacts, now: datetime) -> RuleResult | None:
    """Reminds the assignee once a day in the three days before an outside
    party's agreed due date."""
    if task.status != STATUS_AWAITING_EXTERNAL_COMPLETION:
        return None
    due = task.due_date_obj
    if not due:
        return None
    days_to_due = (due.date() - now.date()).days
    if days_to_due not in {1, 2, 3}:
        return None
    if same_local_date(task.last_reminder_sent_at, now):
        return None
    assignee = lookup_contact(task.assignee_id, contacts)
    if not assignee:
        return None
    ext = task.external_party or "the external party"
    display_due = format_display_date(task.due_date)
    return RuleResult(
        rule="E",
        task=task,
        recipient=assignee,
        subject=subject_line(f"Reminder: {task.task_id} due in {days_to_due} day(s)"),
        body_en=(
            f"Reminder, {task.assignee_name}: '{task.description}' (with {ext}) is due "
            f"in {days_to_due} day(s) on {display_due}. Follow up if needed. "
            f"Reference: {task.task_id}."
        ),
        body_ar=(
            f"تذكير، {task.assignee_name}: '{task.description}' (مع {ext}) مستحقة "
            f"بعد {days_to_due} يوم/أيام في {display_due}. تابع إذا لزم. "
            f"المرجع: {task.task_id}."
        ),
        field_updates={"last_reminder_sent_at": fmt_ts(now)},
        status_after=task.status,
        conversation_purpose="external_completion_check",
        conversation_awaiting_reply=False,
        conversation_role="assignee",
        conversation_participant=task.assignee_id,
    )


def _days_late(task: Task, now: datetime) -> int | None:
    due = task.due_date_obj
    if not due:
        return None
    return (now.date() - due.date()).days


def rule_g_overdue_chaser(task: Task, contacts: Contacts, now: datetime) -> RuleResult | None:
    """Once the due date has passed without an answer to the due-day check,
    chases the assignee once a day and counts the reminder."""
    if task.status != STATUS_AWAITING_ASSIGNEE_COMPLETION:
        return None
    if task.sub_step_index == 0:
        return None
    days_late = _days_late(task, now)
    if days_late is None or days_late < 1:  # undated, or still rule B's day
        return None
    if same_local_date(task.last_reminder_sent_at, now):
        return None
    assignee = lookup_contact(task.assignee_id, contacts)
    if not assignee:
        return None
    display_due = format_display_date(task.due_date) or task.due_date
    day_word = "day" if days_late == 1 else "days"
    return RuleResult(
        rule="G",
        task=task,
        recipient=assignee,
        subject=subject_line(f"{task.task_id}: {days_late} {day_word} past due"),
        body_en=(
            f"{task.assignee_name}, '{task.description}' was due {display_due} "
            f"({days_late} {day_word} ago) and I haven't received a submission. "
            "Reply YES if it's done, or send a new ETA / extension request. "
            f"Reference: {task.task_id}."
        ),
        body_ar=(
            f"{task.assignee_name}، كانت '{task.description}' مستحقة في {display_due} "
            f"(منذ {days_late} يوم/أيام) ولم أستلم تأكيد التسليم. "
            "أجب YES إذا تم، أو أرسل موعدا جديدا / طلب تمديد. "
            f"المرجع: {task.task_id}."
        ),
        field_updates={
            "last_reminder_sent_at": fmt_ts(now),
            "escalation_count": task.escalation_count + 1,
        },
        status_after=task.status,
        conversation_purpose="late_completion_check",
        conversation_awaiting_reply=True,
        conversation_role="assignee",
        conversation_participant=task.assignee_id,
    )


def rule_h_overdue_owner_notice(task: Task, contacts: Contacts, now: datetime) -> RuleResult | None:
    """After two unanswered overdue chasers, tells the task owner once per cycle."""
    if task.status != STATUS_AWAITING_ASSIGNEE_COMPLETION:
        return None
    if task.escalation_count < 2:
        return None
    if LATE_OWNER_NOTICE_MARKER in (task.notes or ""):
        return None
    days_late = _days_late(task, now)
    if days_late is None or days_late < 1:
        return None
    owner = lookup_contact(task.owner_id, contacts)
    if not owner:
        return None
    display_due = format_display_date(task.due_date) or task.due_date
    day_word = "day" if days_late == 1 else "days"
    return RuleResult(
        rule="H",
        task=task,
        recipient=owner,
        subject=subject_line(f"{task.task_id}: {days_late} {day_word} past due, no submission"),
        body_en=(
            f"FYI: {task.task_id} ({task.description}) was due {display_due} "
            f"({days_late} {day_word} ago). {task.assignee_name} hasn't "
            f"submitted or responded after {task.escalation_count} reminders. "
            "I'll keep pinging."
        ),
        body_ar=(
            f"للعلم: {task.task_id} ({task.description}) كانت مستحقة في {display_due} "
            f"(منذ {days_late} يوم/أيام). {task.assignee_name} لم يقم بالتسليم "
            f"أو الرد بعد {task.escalation_count} تذكير. سأستمر بالمتابعة."
        ),
        field_updates={
            "notes": ((task.notes or "") + " | " + LATE_OWNER_NOTICE_MARKER).strip(" |")
        },
        status_after=task.status,
        conversation_purpose="late_completion_owner_notice",
        conversation_awaiting_reply=False,
        conversation_role="owner",
        conversation_participant=task.owner_id,
    )


def rule_f_escalation_cap(task: Task, contacts: Contacts, now: datetime) -> RuleResult | None:
    """Once a task has collected MAX_ESCALATION reminders, hands it to the admin
    (the first contact with admin permission) and stops the cycle."""
    if task.status in TERMINAL_STATUSES:
        return None
    if task.escalation_count < MAX_ESCALATION:
        return None
    if ESCALATED_MARKER in task.notes:
        return None
    admin = contacts.admin
    if not admin:
        return None
    return RuleResult(
        rule="F",
        task=task,
        recipient=admin,
        subject=subject_line(f"ESCALATED: {task.task_id}"),
        body_en=(
            f"Task {task.task_id} ({task.description}) for {task.assignee_name} hit "
            f"escalation cap ({MAX_ESCALATION}). Manual review required."
        ),
        body_ar=(
            f"المهمة {task.task_id} ({task.description}) لـ {task.assignee_name} "
            f"وصلت لحد التصعيد ({MAX_ESCALATION}). تحتاج مراجعة يدوية."
        ),
        field_updates={
            "status": STATUS_ESCALATED,
            "notes": (task.notes + " | " + ESCALATED_MARKER).strip(" |"),
        },
        status_after=STATUS_ESCALATED,
        conversation_purpose="delay_propagation",
        conversation_awaiting_reply=False,
        conversation_role="admin",
        conversation_participant=admin.identity,
    )


# Order matters. F runs first, on the count from previous ticks, so the cap
# needs one full tick at MAX_ESCALATION before it fires. Updates are applied
# between rules, so a chaser's count bump (C, G) is visible to its owner
# notice (D, H) in the same tick.
PROJECT_RULES = (
    rule_f_escalation_cap,
    rule_a_day_before,
    rule_b_due_today,
    rule_g_overdue_chaser,
    rule_h_overdue_owner_notice,
    rule_c_eta_chaser,
    rule_d_eta_owner_notice,
    rule_e_external_due_soon,
)
