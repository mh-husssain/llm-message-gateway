"""task_completion: a completion claim, a hand-off to an outside party, or an
independent (I-XXXX) task being closed."""

from __future__ import annotations

import logging

from ..clock import fmt_ts, now_local, timezone_name
from ..context import HandlerContext
from ..email_format import (
    render_action_badge,
    render_description_panel,
    render_detail_card,
    render_id_chip,
    render_status_chip,
)
from ..models import (
    STATUS_AWAITING_ASSIGNEE_ETA,
    STATUS_BLOCKED_BY_PREDECESSOR,
    STATUS_COMPLETED,
    TERMINAL_STATUSES,
    Task,
)
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
from ..text import ack, humanize_status, subject_line
from ..workflow import (
    all_siblings_complete,
    can_act_on_task,
    find_independent_task_by_id,
    find_task_by_id,
    is_sender_assignee,
    promote_next_sibling,
)
from .common import project_cell, reply_project_not_found, short

log = logging.getLogger("message_gateway.handlers.completion")


def complete_parent_if_done(store: Store, task: Task, tasks: list[Task], stamp: str) -> bool:
    """Close the workflow root once every sub-step is complete."""
    if not (task.parent_task_id and all_siblings_complete(task.parent_task_id, tasks)):
        return False
    parent = next((t for t in tasks if t.task_id == task.parent_task_id), None)
    if not parent or parent.status in TERMINAL_STATUSES:
        return False
    update_task_fields(
        store,
        parent,
        status=STATUS_COMPLETED,
        completed_at=stamp,
        notes=(parent.notes + f" | all sub-steps complete at {stamp}").strip(" |"),
    )
    return True


def _resolve_explicit(ctx: HandlerContext, task_id_hint: str, project_filter: str) -> Task | None:
    """Cross-assignee lookup by T-ID (managers report on others' work), gated by
    task-scoped permission. Replies and returns None when it can't proceed."""
    tasks = load_tasks(ctx.store)
    candidate = find_task_by_id(tasks, task_id_hint)
    if candidate is None:
        ctx.reply(
            f"I don't see a task {task_id_hint}. Could you double-check the ID?",
            f"لا أجد المهمة {task_id_hint}. يرجى التحقق من الرقم.",
        )
        log_activity(
            ctx.store,
            "Completion id_not_found",
            ctx.sender.name,
            f"task_id_hint={task_id_hint} not present in Tasks.",
        )
        return None
    if candidate.status in TERMINAL_STATUSES:
        ctx.reply(
            f"{candidate.task_id} is already {humanize_status(candidate.status, 'en')} "
            "— nothing to do.",
            f"{candidate.task_id} حالتها {humanize_status(candidate.status, 'ar')} "
            "مسبقا — لا شيء للتنفيذ.",
        )
        log_activity(
            ctx.store,
            "Completion noop",
            ctx.sender.name,
            f"{candidate.task_id} already {candidate.status}.",
        )
        return None
    if candidate.sub_step_index == 0:
        # A workflow root was named: resolve to its single active step. With
        # several active steps, ask rather than guess. With none, the root
        # itself is the target.
        children = [t for t in tasks if t.parent_task_id == candidate.task_id]
        active = [
            t
            for t in children
            if t.status not in TERMINAL_STATUSES and t.status != STATUS_BLOCKED_BY_PREDECESSOR
        ]
        if len(active) == 1:
            candidate = active[0]
        elif active:
            options = ", ".join(f"{c.task_id} ({c.description[:40]})" for c in active[:5])
            ctx.reply(
                f"{candidate.task_id} has {len(active)} active steps. "
                f"Which one is complete? Open: {options}",
                f"{candidate.task_id} لديها {len(active)} خطوة نشطة. أيها اكتملت؟ {options}",
            )
            log_activity(
                ctx.store,
                "Completion ambiguous root",
                ctx.sender.name,
                f"Root {candidate.task_id} → {len(active)} active children.",
            )
            return None
    if not can_act_on_task(ctx.sender, candidate):
        ctx.reply(
            f"You don't have permission to complete {candidate.task_id}.",
            f"ليس لديك صلاحية لإغلاق {candidate.task_id}.",
        )
        log_activity(
            ctx.store,
            "Completion permission denied",
            ctx.sender.name,
            f"{candidate.task_id} owned by {candidate.owner_id}, "
            f"assignees={candidate.assignee_id}.",
        )
        return None
    if project_filter and candidate.project_id != project_filter:
        ctx.reply(
            f"{candidate.task_id} is on {candidate.project_id}, not "
            f"{project_filter}. If you meant {candidate.task_id}, drop the project ref.",
            f"{candidate.task_id} على {candidate.project_id}، وليس {project_filter}.",
        )
        log_activity(
            ctx.store,
            "Completion project mismatch",
            ctx.sender.name,
            f"{candidate.task_id} project={candidate.project_id} vs requested {project_filter}.",
        )
        return None
    return candidate


def _resolve_own(ctx: HandlerContext, project_filter: str, desc_hint: str | None) -> Task | None:
    """No T-ID: the sender is reporting their own work. Root rows are excluded."""
    candidates = [
        t
        for t in load_tasks(ctx.store)
        if is_sender_assignee(t, ctx.sender_id)
        and t.status not in TERMINAL_STATUSES
        and t.status != STATUS_BLOCKED_BY_PREDECESSOR
        and t.sub_step_index > 0
    ]
    if project_filter:
        candidates = [t for t in candidates if t.project_id == project_filter]
    target: Task | None = None
    if len(candidates) == 1:
        target = candidates[0]
    elif desc_hint:
        matches = [t for t in candidates if desc_hint.lower() in t.description.lower()]
        if len(matches) == 1:
            target = matches[0]
    if target is None:
        if not candidates:
            ctx.reply(
                "I don't see any open tasks for you. Could you reference the task ID?",
                "لا أرى مهام مفتوحة لك. يرجى ذكر رقم المهمة.",
            )
        else:
            options = ", ".join(f"{t.task_id} ({t.description[:40]})" for t in candidates[:5])
            ctx.reply(
                f"Which task is complete? Open: {options}",
                f"أي مهمة اكتملت؟ المهام المفتوحة: {options}",
            )
        log_activity(
            ctx.store,
            "Completion ambiguous",
            ctx.sender.name,
            f"No task_id_hint; candidates={len(candidates)}.",
        )
    return target


def _hand_off(ctx: HandlerContext, target: Task) -> None:
    """The assignee handed the step to an outside party: don't complete it;
    wait for the party's timeline instead."""
    sender = ctx.sender
    stamp = fmt_ts(now_local())
    update_task_fields(
        ctx.store,
        target,
        status=STATUS_AWAITING_ASSIGNEE_ETA,
        awaiting_response_from=target.assignee_id,
        awaiting_response_since=stamp,
        last_reminder_sent_at="",
        escalation_count=0,
        notes=(target.notes + f" | handed off to {target.external_party} at {stamp}").strip(" |"),
    )
    body_en = (
        f"Thanks {sender.name}. How long does {target.external_party} typically need "
        f"on '{target.description}'? Reference: {target.task_id}."
    )
    body_ar = (
        f"شكرا {sender.name}. كم يوم عادة يأخذ {target.external_party} "
        f"على '{target.description}'؟ المرجع: {target.task_id}."
    )
    ctx.channel.send_new(
        sender,
        subject=subject_line(f"{target.task_id}: ETA from {target.external_party}?"),
        body=ack(body_en, body_ar, ctx.lang),
    )
    append_conversation(
        ctx.store,
        task_id=target.task_id,
        direction="outbound",
        participant_id=target.assignee_id,
        participant_role="assignee",
        channel=ctx.channel.name,
        msg_id="",
        purpose="eta_request",
        body=body_en,
        awaiting_reply=True,
    )
    log_activity(
        ctx.store,
        "External handoff",
        f"{target.task_id} / {sender.name}",
        f"Handed to {target.external_party}. Awaiting ETA.",
    )
    ctx.reply(
        f"Got it — {target.task_id} handed to {target.external_party}. How long do they need?",
        f"تم — {target.task_id} تسلمه {target.external_party}. كم يوم يحتاجون؟",
    )


def handle_task_completion(ctx: HandlerContext) -> None:
    ex = ctx.extracted
    sender = ctx.sender
    project_filter = (ex.get("project_id") or "").strip()
    task_id_hint = (ex.get("task_id") or "").strip() or None
    desc_hint = (ex.get("subject_hint") or "").strip() or None
    project_ref_unresolved = (ex.get("project_ref_unresolved") or "").strip()

    if task_id_hint and task_id_hint.upper().startswith("I-"):
        handle_independent_completion(ctx, task_id_hint.upper())
        return

    # An unresolvable project ref must not silently complete a task elsewhere.
    if project_ref_unresolved and not project_filter:
        reply_project_not_found(
            ctx,
            project_ref_unresolved=project_ref_unresolved,
            action_tag="task completion",
            parsed_intent_description=desc_hint,
        )
        return

    if task_id_hint:
        target = _resolve_explicit(ctx, task_id_hint, project_filter)
    else:
        target = _resolve_own(ctx, project_filter, desc_hint)
    if target is None:
        return

    if bool(ex.get("external_party_handoff")) and target.external_party:
        _hand_off(ctx, target)
        return

    update_task_fields(
        ctx.store,
        target,
        status=STATUS_COMPLETED,
        completed_at=fmt_ts(now_local()),
        completion_msg_id=ctx.msg.msg_id,
        notes=(
            target.notes + f" | completed via {ctx.channel.name} at {fmt_ts(now_local())}"
        ).strip(" |"),
    )

    tasks = load_tasks(ctx.store)
    target_fresh = next((t for t in tasks if t.task_id == target.task_id), target)
    promoted = promote_next_sibling(ctx.store, target_fresh, tasks)
    parent_completed = complete_parent_if_done(ctx.store, target_fresh, tasks, fmt_ts(now_local()))
    has_next = bool(promoted and promoted.task_id != target.task_id)

    owner = ctx.contacts.by_identity(target.owner_id)
    if owner:
        owner_body_en = (
            f"{sender.name} reported {target.task_id} ({target.description}) complete "
            f"on {target.project_name} ({target.project_id})."
        )
        owner_body_ar = (
            f"{sender.name} أبلغ بإكمال {target.task_id} ({target.description}) "
            f"على {target.project_name} ({target.project_id})."
        )
        if has_next:
            owner_body_en += (
                f"\n\nNext step now active: {promoted.task_id} ({promoted.description})."
            )
            owner_body_ar += (
                f"\n\nالخطوة التالية فعالة الآن: {promoted.task_id} ({promoted.description})."
            )
        if parent_completed:
            owner_body_en += f"\n\nThe parent workflow {target.parent_task_id} is fully complete."
            owner_body_ar += f"\n\nسير العمل {target.parent_task_id} مكتمل بالكامل."
        ctx.channel.send_new(
            owner,
            subject=subject_line(f"{target.task_id} completed by {sender.name}"),
            body=ack(owner_body_en, owner_body_ar, ctx.lang),
        )

    extra_en = f" Next step: {promoted.task_id} ({promoted.description})." if has_next else ""
    extra_ar = f" الخطوة التالية: {promoted.task_id} ({promoted.description})." if has_next else ""
    plain_ack = ack(
        f"Got it — {target.task_id} marked complete.{extra_en}",
        f"تم — {target.task_id} مكتمل.{extra_ar}",
        ctx.lang,
    )
    detail_rows = [
        ("Task ID", render_id_chip(target.task_id)),
        ("Project", project_cell(target)),
        ("Completed by", sender.name),
        ("Completed at", fmt_ts(now_local())),
        ("Status", render_status_chip("Completed")),
    ]
    if has_next:
        detail_rows.append(
            ("Next step", f"{render_id_chip(promoted.task_id)} {promoted.description}")
        )
    ctx.reply_block(
        plain_ack,
        render_action_badge("Task Completed", "confirmed")
        + render_description_panel(target.description)
        + render_detail_card(detail_rows),
    )
    log_activity(
        ctx.store,
        "Task completed",
        f"{target.task_id} / {sender.name}",
        f"Promoted={promoted.task_id if promoted else 'none'}; parent_done={parent_completed}.",
    )


def handle_independent_completion(ctx: HandlerContext, task_id: str) -> None:
    """Close an I-XXXX task. Allowed for the assignee, the creator, or any
    manager/admin; the creator is notified unless they closed it themselves."""
    sender = ctx.sender
    target = find_independent_task_by_id(load_independent_tasks(ctx.store), task_id)
    if target is None:
        ctx.reply(
            f"I don't see an independent task {task_id}.", f"لا أجد المهمة المستقلة {task_id}."
        )
        log_activity(
            ctx.store,
            "Independent completion id_not_found",
            sender.name,
            f"task_id={task_id} not present in Independent_Tasks.",
        )
        return
    if target.status == STATUS_COMPLETED:
        ctx.reply(
            f"{target.task_id} is already marked complete.",
            f"{target.task_id} مسجلة كمكتملة سابقا.",
        )
        return
    sender_id = ctx.sender_id
    if not (
        sender.is_admin
        or sender.can_assign
        or sender_id == target.assignee_id
        or sender_id == target.creator_id
    ):
        ctx.reply(
            f"You don't have permission to complete {target.task_id}.",
            f"ليس لديك صلاحية لإكمال {target.task_id}.",
        )
        log_activity(
            ctx.store,
            "Independent completion permission denied",
            sender.name,
            f"{target.task_id} sender={sender_id}.",
        )
        return

    completed_ts = fmt_ts(now_local())
    update_independent_task_fields(
        ctx.store,
        target,
        status=STATUS_COMPLETED,
        completed_at=completed_ts,
        completion_msg_id=ctx.msg.msg_id,
        notes=(target.notes + f" | completed via {ctx.channel.name} at {completed_ts}").strip(" |"),
    )
    ctx.reply(
        f'Got it — "{target.description}" marked complete. [{target.task_id}]',
        f'تم تسجيل "{target.description}" كمكتملة. [{target.task_id}]',
    )
    creator = load_contacts(ctx.store).by_identity(target.creator_id)
    if creator and creator.identity != sender_id:
        body_text = (
            f"Heads up — {target.assignee_name} marked the following task "
            f"complete:\n\n"
            f"  • Task: {target.description}\n"
            f"  • Completed at: {completed_ts} ({timezone_name()})\n\n"
            f"Reference: [{target.task_id}]."
        )
        try:
            ctx.channel.send_new(
                creator,
                subject=subject_line(f"Completed: {short(target.description)}"),
                body=body_text,
            )
        except Exception:
            log.exception("creator notify failed for %s", target.task_id)
    log_activity(
        ctx.store,
        "Independent task completed",
        f"{target.task_id} / {sender.name}",
        f"assignee={target.assignee_name}; via {ctx.channel.name}.",
    )
