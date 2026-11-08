"""The two reminder engines: project tasks (/cron) and independent tasks
(/cron-fast).

Both are no-ops outside business hours, so an over-eager schedule is safe.
For each open task every rule is evaluated in order and its updates are
applied before the next rule runs, so several rules can fire on one task in a
single tick (e.g. a chaser and the owner notice it unlocks).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..channel import Channel
from ..clock import is_business_hours, now_local
from ..email_format import (
    VARIANTS,
    render_action_badge,
    render_description_panel,
    render_detail_card,
    render_id_chip,
    render_status_chip,
)
from ..models import TERMINAL_STATUSES, Contacts
from ..records import (
    append_conversation,
    load_contacts,
    load_independent_tasks,
    load_tasks,
    log_activity,
    update_independent_task_fields,
    update_task_fields,
)
from ..store import Store
from ..text import ack, humanize_status
from .base import RuleResult
from .independent_rules import INDEPENDENT_RULES
from .project_rules import PROJECT_RULES

log = logging.getLogger("message_gateway.reminders")

SKIPPED = {"skipped": "outside_business_hours"}

# Rule -> (badge label, badge variant, dot) for the HTML card.
RULE_PRESENTATION: dict[str, tuple[str, str, bool]] = {
    "A": ("Reminder · T-1 day", "info", False),
    "B": ("Reminder · Due today", "warning", False),
    "C": ("Awaiting External Party", "warning", False),
    "D": ("FYI · External delay", "info", False),
    "E": ("Reminder · Pre-due external", "info", False),
    "G": ("Late · Assignee silent", "escalation", True),
    "H": ("Late · Owner notified", "escalation", True),
    "F": ("Escalation Cap Reached", "escalation", True),
}


def build_rule_html_block(result: RuleResult) -> str:
    """Card for a project-task reminder: badge, description and task facts.
    `body_en` stays the source of truth; this adds structure only."""
    task = result.task
    label, variant, dot = RULE_PRESENTATION.get(result.rule, ("Notification", "info", False))
    rows: list[tuple[str, str]] = [("Task ID", render_id_chip(task.task_id))]
    if task.project_id:
        rows.append(("Project", f"{task.project_name} {render_id_chip(task.project_id)}"))
    elif task.project_name:
        rows.append(("Project", task.project_name))
    if task.assignee_name:
        rows.append(("Assignee", task.assignee_name))
    if task.due_date:
        rows.append(("Deadline", task.due_date))
    if task.external_party:
        rows.append(("External party", task.external_party))
    if task.escalation_count:
        colour = VARIANTS["escalation"][1]
        rows.append(
            (
                "Reminders sent",
                f'<span style="color:{colour};font-weight:700;">{task.escalation_count}</span>',
            )
        )
    rows.append(("Status", render_status_chip(humanize_status(task.status, "en"))))
    return (
        render_action_badge(label, variant, dot=dot)
        + render_description_panel(task.description or "(no description)")
        + render_detail_card(rows)
    )


def _record(store: Store, channel: Channel, result: RuleResult) -> None:
    append_conversation(
        store,
        task_id=result.task.task_id,
        direction="outbound",
        participant_id=result.conversation_participant,
        participant_role=result.conversation_role,
        channel=channel.name,
        msg_id="",
        purpose=result.conversation_purpose,
        body=result.body_en,
        awaiting_reply=result.conversation_awaiting_reply,
    )


def _log_detail(result: RuleResult) -> str:
    return f"Rule {result.rule}: {result.conversation_purpose}; status_after={result.status_after}."


def apply_project_result(store: Store, channel: Channel, result: RuleResult) -> None:
    """Persist the updates, send the reminder (plain plus HTML card), record the
    conversation, log."""
    task = result.task
    update_task_fields(store, task, **result.field_updates)
    body = ack(result.body_en, result.body_ar, "en")
    channel.send_new(
        result.recipient,
        subject=result.subject,
        body=body,
        blocks=[(body, build_rule_html_block(result))],
    )
    _record(store, channel, result)
    log_activity(
        store, "Reminder sent", f"{task.task_id} / {task.assignee_name}", _log_detail(result)
    )


def apply_independent_result(store: Store, channel: Channel, result: RuleResult) -> None:
    """As apply_project_result, for Independent_Tasks. A failed send or
    conversation write is logged and does not stop the tick."""
    task = result.task
    update_independent_task_fields(store, task, **result.field_updates)
    body = ack(result.body_en, result.body_ar, "en")
    try:
        channel.send_new(result.recipient, subject=result.subject, body=body)
    except Exception:
        log.exception("send_new failed for %s on rule %s", task.task_id, result.rule)
    try:
        _record(store, channel, result)
    except Exception:
        log.exception("append_conversation failed for %s on rule %s", task.task_id, result.rule)
    log_activity(
        store,
        "Reminder sent (independent)",
        f"{task.task_id} / {task.assignee_name}",
        _log_detail(result),
    )


def _run(tasks, rules, contacts: Contacts, now: datetime, apply) -> list[dict[str, str]]:
    fired: list[dict[str, str]] = []
    for task in tasks:
        if task.status in TERMINAL_STATUSES:
            continue
        for rule in rules:
            if task.status in TERMINAL_STATUSES:
                break  # an escalation earlier in this tick closed the cycle
            result = rule(task, contacts, now)
            if result is None:
                continue
            apply(result)
            fired.append({"rule": result.rule, "task_id": task.task_id})
    return fired


def run_periodic_jobs(
    channel: Channel,
    store: Store,
    *,
    contacts: Contacts | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project-task engine (rules A-H and F). Returns a summary for logs and tests."""
    now = now or now_local()
    if not is_business_hours(now):
        return dict(SKIPPED)
    contacts = contacts or load_contacts(store)
    fired = _run(
        load_tasks(store),
        PROJECT_RULES,
        contacts,
        now,
        lambda r: apply_project_result(store, channel, r),
    )
    log_activity(
        store, "Run completed", "(agent)", f"Cron via {channel.name}. Rules fired: {len(fired)}."
    )
    return {"fired": len(fired), "details": fired}


def run_independent_periodic_jobs(
    channel: Channel,
    store: Store,
    *,
    contacts: Contacts | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Independent-task engine (rules I, I2, J)."""
    now = now or now_local()
    if not is_business_hours(now):
        return dict(SKIPPED)
    contacts = contacts or load_contacts(store)
    fired = _run(
        load_independent_tasks(store),
        INDEPENDENT_RULES,
        contacts,
        now,
        lambda r: apply_independent_result(store, channel, r),
    )
    log_activity(
        store,
        "Run completed (independent)",
        "(agent)",
        f"Cron-fast via {channel.name}. Rules fired: {len(fired)}.",
    )
    return {"fired": len(fired), "details": fired}
