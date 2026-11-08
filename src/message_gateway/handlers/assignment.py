"""assign_task: manager-only task creation.

Project tasks become a root row plus one row per sequenced sub-step (later
steps start 'blocked'). Joint tasks store comma-joined assignee IDs. A task
flagged `is_independent` (no project, a time-of-day deadline) goes to the
Independent_Tasks tab instead.
"""

from __future__ import annotations

import json
import logging
from collections import Counter

from ..clock import fmt_ts, iso_to_sheet_date, now_local, parse_deadline_iso, timezone_name
from ..context import HandlerContext
from ..email_format import (
    render_action_badge,
    render_description_panel,
    render_detail_card,
    render_id_chip,
    render_status_chip,
)
from ..models import (
    STATUS_ASSIGNED,
    STATUS_BLOCKED_BY_PREDECESSOR,
    Contact,
    IndependentTask,
    Task,
)
from ..records import (
    append_conversation,
    append_independent_task,
    append_task,
    load_independent_tasks,
    load_tasks,
    log_activity,
)
from ..text import ack, next_independent_task_id, next_project_id, next_task_id, subject_line
from ..workflow import resolve_assignee
from .common import active_projects_hint, reply_project_not_found, short

log = logging.getLogger("message_gateway.handlers.assignment")

REGISTER_PROJECT_PREFIX = "REGISTER_PROJECT:"


def _deny_non_manager(ctx: HandlerContext) -> bool:
    if ctx.sender.can_assign:
        return False
    ctx.reply(
        "Only managers can assign tasks. Please contact your manager.",
        "فقط المدراء يمكنهم تعيين المهام. يرجى التواصل مع المدير.",
    )
    return True


def handle_assign_task(ctx: HandlerContext) -> None:
    sender = ctx.sender
    if _deny_non_manager(ctx):
        log_activity(
            ctx.store,
            "Permission denied",
            ctx.sender_id,
            f"Non-manager attempted task assignment via {ctx.channel.name}.",
        )
        return

    # Project-less, time-bound items have their own path.
    if ctx.extracted.get("is_independent") is True:
        handle_independent_assignment(ctx)
        return

    ex = ctx.extracted
    # Multi-assignee list; a legacy single `assignee_name` string is tolerated.
    raw_names = ex.get("assignee_names")
    if raw_names is None:
        legacy = (ex.get("assignee_name") or "").strip()
        raw_names = [legacy] if legacy else []
    if isinstance(raw_names, str):
        raw_names = [raw_names]
    assignee_names = [n.strip() for n in raw_names if n and str(n).strip()]
    project_id = (ex.get("project_id") or "").strip()
    project_ref_unresolved = (ex.get("project_ref_unresolved") or "").strip()
    due_date_iso = ex.get("due_date_iso")
    steps = ex.get("steps") or []

    # The model flagged a project reference it couldn't resolve. Recovery, in order:
    #   (a) it was created earlier in this same message (projects were reloaded);
    #   (b) past task rows use that project name although Projects has no row:
    #       park the assignment and offer to register the project on YES;
    #   (c) otherwise reply "not found", echoing the parsed task description.
    if project_ref_unresolved and not project_id:
        ref_lc = project_ref_unresolved.lower()
        for p in ctx.projects.values():
            if p.name.strip().lower() == ref_lc and p.status.lower() == "active":
                project_id = p.id
                project_ref_unresolved = ""
                break
    if project_ref_unresolved and not project_id:
        ref_lc = project_ref_unresolved.lower()
        past_matches = [
            t for t in load_tasks(ctx.store) if t.project_name.strip().lower() == ref_lc
        ]
        if past_matches:
            # Reuse the most common ID already on those rows so they stay
            # consistent; mint a new one only if none had an ID.
            id_counts = Counter(t.project_id.strip() for t in past_matches if t.project_id.strip())
            if id_counts:
                candidate_pid = id_counts.most_common(1)[0][0]
            else:
                candidate_pid = next_project_id(list(ctx.projects.keys()))
            payload = {
                "kind": "register_project",
                "name": project_ref_unresolved,
                "pid": candidate_pid,
                "ex": ex,  # original extraction, replayed on YES
            }
            append_conversation(
                ctx.store,
                task_id="(pending)",
                direction="outbound",
                participant_id=ctx.sender_id,
                participant_role="owner",
                channel=ctx.channel.name,
                msg_id=ctx.msg.msg_id,
                purpose="register_project_request",
                body=REGISTER_PROJECT_PREFIX + json.dumps(payload, ensure_ascii=False),
                awaiting_reply=True,
                truncate=False,
            )
            ctx.reply(
                f"I see '{project_ref_unresolved}' in past tasks but it isn't "
                f"registered as a project yet. Reply YES to register "
                f"{candidate_pid} ({project_ref_unresolved}) and create the "
                f"task; reply NO to cancel.",
                f"أرى '{project_ref_unresolved}' في مهام سابقة لكن لم يُسجَّل "
                f"كمشروع بعد. أجب YES لتسجيل {candidate_pid} "
                f"({project_ref_unresolved}) وإنشاء المهمة، أو NO للإلغاء.",
            )
            log_activity(
                ctx.store,
                "Project register prompted",
                sender.name,
                f"name='{project_ref_unresolved}' candidate_pid={candidate_pid}; "
                f"{len(past_matches)} past task(s) reference this name.",
            )
            return

        first_step_desc = ""
        if steps and isinstance(steps[0], dict):
            first_step_desc = (steps[0].get("description") or "").strip()
        if not first_step_desc:
            first_step_desc = (ex.get("description") or "").strip()
        reply_project_not_found(
            ctx,
            project_ref_unresolved=project_ref_unresolved,
            action_tag="task assignment",
            parsed_intent_description=first_step_desc or None,
        )
        return

    if not assignee_names:
        ctx.reply(
            "Who should I assign this to? Please name someone.",
            "لمن سأعين هذه المهمة؟ يرجى ذكر الاسم.",
        )
        log_activity(
            ctx.store, "Assignment failed", sender.name, "No assignee specified in message."
        )
        return

    # Resolve every name; never half-assign. Report all failures in one reply.
    resolved: list[Contact] = []
    not_found: list[str] = []
    ambiguous: list[str] = []
    for name in assignee_names:
        contact, err = resolve_assignee(name, ctx.contacts)
        if err == "not_found" or not contact:
            not_found.append(name)
        elif err == "ambiguous":
            ambiguous.append(name)
        else:
            resolved.append(contact)
    if not_found or ambiguous:
        msgs_en: list[str] = []
        msgs_ar: list[str] = []
        if not_found:
            joined = ", ".join(f"'{n}'" for n in not_found)
            msgs_en.append(f"I couldn't find a contact named {joined}.")
            msgs_ar.append(f"لم أجد جهات اتصال باسم {joined}.")
        if ambiguous:
            joined = ", ".join(f"'{n}'" for n in ambiguous)
            msgs_en.append(f"Multiple contacts match {joined}; use full names.")
            msgs_ar.append(f"يوجد عدة جهات تطابق {joined}؛ يرجى الاسم الكامل.")
        ctx.reply(" ".join(msgs_en) + " Please clarify.", " ".join(msgs_ar) + " يرجى التوضيح.")
        log_activity(
            ctx.store,
            "Assignment failed",
            sender.name,
            f"Assignees not_found={not_found}, ambiguous={ambiguous}.",
        )
        return
    # Dedupe, preserving order ("Alice and Alice").
    seen_ids: set[str] = set()
    deduped: list[Contact] = []
    for c in resolved:
        cid = c.identity
        if cid and cid not in seen_ids:
            seen_ids.add(cid)
            deduped.append(c)
    resolved = deduped

    project = ctx.projects.get(project_id)
    if not project:
        en_suf, ar_suf = active_projects_hint(ctx.projects)
        ctx.reply(
            "I couldn't resolve the project for this task. Mention a project name or P-ID."
            + en_suf,
            "لم أتمكن من تحديد المشروع. يرجى ذكر اسم المشروع أو رقمه." + ar_suf,
        )
        log_activity(
            ctx.store,
            "Assignment failed",
            sender.name,
            f"Project_id '{project_id}' not active. Steps: {len(steps)}.",
        )
        return

    if not steps:
        steps = [
            {
                "description": ex.get("description") or "task",
                "external_party": None,
                "depends_on_step": None,
            }
        ]

    existing_ids = [t.task_id for t in load_tasks(ctx.store)]
    root_id = next_task_id(existing_ids)
    existing_ids.append(root_id)
    child_ids: list[str] = []
    for _ in steps:
        cid = next_task_id(existing_ids)
        child_ids.append(cid)
        existing_ids.append(cid)

    due_sheet = iso_to_sheet_date(due_date_iso) if due_date_iso else ""
    owner_id = ctx.sender_id
    # Joint tasks: comma-joined IDs and names (read via split_assignee_ids).
    assignee_id = ",".join(c.identity for c in resolved)
    assignee_name_combined = ", ".join(c.name for c in resolved)

    append_task(
        ctx.store,
        Task(
            task_id=root_id,
            parent_task_id="",
            project_id=project.id,
            project_name=project.name,
            owner_id=owner_id,
            owner_name=sender.name,
            assignee_id=assignee_id,
            assignee_name=assignee_name_combined,
            description=f"[Workflow] {len(steps)}-step assignment to {assignee_name_combined}",
            sub_step_index=0,
            status=STATUS_ASSIGNED,
            due_date=due_sheet,
            external_party="",
            awaiting_response_from="",
            awaiting_response_since="",
            last_reminder_sent_at="",
            escalation_count=0,
            completed_at="",
            original_request_msg_id=ctx.msg.msg_id,
            completion_msg_id="",
            notes=f"Created from {ctx.channel.name} on {fmt_ts(now_local())}.",
        ),
    )

    for i, (step, cid) in enumerate(zip(steps, child_ids, strict=True), start=1):
        is_first = i == 1
        depends_on = step.get("depends_on_step")
        # First step is active; later steps wait for their predecessor.
        step_status = STATUS_ASSIGNED if is_first else STATUS_BLOCKED_BY_PREDECESSOR
        blocking_id = ""
        if not is_first and depends_on and 1 <= int(depends_on) <= len(child_ids):
            blocking_id = child_ids[int(depends_on) - 1]
        elif not is_first:
            blocking_id = child_ids[i - 2]
        step_notes = f"Created from {ctx.channel.name} on {fmt_ts(now_local())}." + (
            f" Blocked by {blocking_id} until completed." if blocking_id else ""
        )
        append_task(
            ctx.store,
            Task(
                task_id=cid,
                parent_task_id=root_id,
                project_id=project.id,
                project_name=project.name,
                owner_id=owner_id,
                owner_name=sender.name,
                assignee_id=assignee_id,
                assignee_name=assignee_name_combined,
                description=(step.get("description") or "").strip() or "(no description)",
                sub_step_index=i,
                status=step_status,
                due_date=due_sheet,
                external_party=(step.get("external_party") or "").strip(),
                awaiting_response_from="",
                awaiting_response_since="",
                last_reminder_sent_at="",
                escalation_count=0,
                completed_at="",
                original_request_msg_id=ctx.msg.msg_id,
                completion_msg_id="",
                notes=step_notes,
            ),
        )

    # One bundled message per assignee covering every step.
    step_lines_en: list[str] = []
    step_lines_ar: list[str] = []
    for i, step in enumerate(steps, start=1):
        desc = (step.get("description") or "").strip()
        ext = (step.get("external_party") or "").strip()
        step_lines_en.append(f"  {i}. {desc}" + (f" (external: {ext})" if ext else ""))
        step_lines_ar.append(f"  {i}. {desc}" + (f" (طرف خارجي: {ext})" if ext else ""))

    due_line_en = f"\nDue: {due_sheet}" if due_sheet else ""
    due_line_ar = f"\nالموعد النهائي: {due_sheet}" if due_sheet else ""

    # Single-step: cite the actionable child ID. Multi-step: root plus children.
    manager_ref = child_ids[0] if len(steps) == 1 else f"{root_id} (steps: {', '.join(child_ids)})"
    assignee_ref = child_ids[0] if len(steps) == 1 else root_id

    joint_names = ", ".join(c.name for c in resolved if c.name)
    joint_note_en = f" (joint task with {joint_names})" if len(resolved) > 1 else ""
    joint_note_ar = f" (مهمة مشتركة مع {joint_names})" if len(resolved) > 1 else ""

    assignee_subject = subject_line(f"New task ({project.name}) — {assignee_ref}")
    for assignee in resolved:
        body_en = (
            f"Hi {assignee.name},\n\n"
            f"You've been assigned the following from {sender.name} "
            f"({project.name}, {project.id}){joint_note_en}:\n\n"
            + "\n".join(step_lines_en)
            + due_line_en
            + f"\n\nReference: {assignee_ref}"
        )
        body_ar = (
            f"مرحبا {assignee.name}،\n\n"
            f"تم تعيين المهام التالية لك من {sender.name} "
            f"({project.name}, {project.id}){joint_note_ar}:\n\n"
            + "\n".join(step_lines_ar)
            + due_line_ar
            + f"\n\nالمرجع: {assignee_ref}"
        )
        ctx.channel.send_new(
            assignee, subject=assignee_subject, body=ack(body_en, body_ar, ctx.lang)
        )

    # One outbound conversation row per recipient (English body, so the audit
    # log stays searchable regardless of language).
    for assignee in resolved:
        log_body = (
            f"Hi {assignee.name},\n\nYou've been assigned the following from {sender.name} "
            f"({project.name}, {project.id}):\n\n"
            + "\n".join(step_lines_en)
            + due_line_en
            + f"\n\nReference: {assignee_ref}"
        )
        append_conversation(
            ctx.store,
            task_id=child_ids[0],
            direction="outbound",
            participant_id=assignee.identity,
            participant_role="assignee",
            channel=ctx.channel.name,
            msg_id="",
            purpose="assignment",
            body=log_body,
            awaiting_reply=False,
        )

    summary_en = (
        f"Created {manager_ref} for {assignee_name_combined} on {project.name}"
        + (f" with {len(steps)} steps" if len(steps) > 1 else "")
        + (f", due {due_sheet}." if due_sheet else " (no due date set).")
    )
    summary_ar = (
        f"تم إنشاء {manager_ref} لـ {assignee_name_combined} على {project.name}"
        + (f" بـ {len(steps)} خطوات" if len(steps) > 1 else "")
        + (f"، الموعد النهائي {due_sheet}." if due_sheet else " (بدون موعد محدد).")
    )
    plain_summary = ack(summary_en, summary_ar, ctx.lang)

    if len(steps) == 1:
        panel_desc = (steps[0].get("description") or "").strip() or assignee_name_combined
    else:
        panel_desc = "; ".join(
            f"{i}. {(s.get('description') or '').strip()}" for i, s in enumerate(steps, start=1)
        )
    detail_rows = [
        ("Task ID", render_id_chip(child_ids[0] if len(steps) == 1 else root_id)),
        ("Project", f"{project.name} {render_id_chip(project.id)}"),
        ("Assignee", assignee_name_combined),
        ("Deadline", due_sheet or "Not set"),
    ]
    if len(steps) > 1:
        detail_rows.append(("Steps", f"{len(steps)}"))
    detail_rows.append(("Status", render_status_chip("Assigned")))
    ctx.reply_block(
        plain_summary,
        render_action_badge("Task Assigned", "confirmed")
        + render_description_panel(panel_desc)
        + render_detail_card(detail_rows),
    )

    log_activity(
        ctx.store,
        "Task assigned",
        f"{root_id} / {assignee_name_combined}",
        f"{len(steps)} step(s) on {project.id}. Due {due_sheet or 'none'}. "
        f"Assignees: {assignee_id}. Children: {','.join(child_ids)}.",
    )


def handle_independent_assignment(ctx: HandlerContext) -> None:
    """A project-less task with a required, minute-precision deadline.

    Single assignee (extra names are reported and ignored), stored in
    Independent_Tasks, and the assignee is notified immediately.
    """
    sender = ctx.sender
    if _deny_non_manager(ctx):  # unreachable via handle_assign_task; kept as a guard
        return

    ex = ctx.extracted
    raw_names = ex.get("assignee_names")
    if isinstance(raw_names, str):
        raw_names = [raw_names]
    if not raw_names:
        legacy = (ex.get("assignee_name") or "").strip()
        raw_names = [legacy] if legacy else []
    assignee_names = [n.strip() for n in (raw_names or []) if n and str(n).strip()]

    if not assignee_names:
        ctx.reply(
            "Who should I assign this to? Please name someone.",
            "لمن سأعين هذه المهمة؟ يرجى ذكر الاسم.",
        )
        log_activity(
            ctx.store, "Independent assignment failed", sender.name, "No assignee specified."
        )
        return

    deadline_iso = (ex.get("deadline_iso") or "").strip()
    deadline_dt = parse_deadline_iso(deadline_iso) if deadline_iso else None
    if not deadline_dt:
        ctx.reply(
            "When should this be done by? Independent tasks need a deadline (e.g. 'by 4pm today').",
            "متى يجب إنهاؤها؟ المهام المستقلة تحتاج موعدا نهائيا "
            "(مثلا 'بحلول الساعة 4 مساء اليوم').",
        )
        log_activity(
            ctx.store,
            "Independent assignment failed",
            sender.name,
            "No deadline supplied; rejected.",
        )
        return

    primary_name = assignee_names[0]
    extras = assignee_names[1:]
    contact, err = resolve_assignee(primary_name, ctx.contacts)
    if err == "not_found" or not contact:
        ctx.reply(
            f"I couldn't find a contact named '{primary_name}'. Please clarify.",
            f"لم أجد جهة اتصال باسم '{primary_name}'. يرجى التوضيح.",
        )
        log_activity(
            ctx.store,
            "Independent assignment failed",
            sender.name,
            f"Assignee '{primary_name}' not_found.",
        )
        return
    if err == "ambiguous":
        ctx.reply(
            f"Multiple contacts match '{primary_name}'; use the full name.",
            f"يوجد عدة جهات تطابق '{primary_name}'؛ يرجى الاسم الكامل.",
        )
        log_activity(
            ctx.store,
            "Independent assignment failed",
            sender.name,
            f"Assignee '{primary_name}' ambiguous.",
        )
        return

    description = (ex.get("description") or "").strip()
    if not description:
        steps = ex.get("steps") or []
        if steps and isinstance(steps[0], dict):
            description = (steps[0].get("description") or "").strip()
    if not description:
        description = "(no description)"

    # Optional pre-deadline reminder interval, in minutes.
    pre_min_raw = ex.get("pre_deadline_reminder_minutes")
    try:
        pre_min = int(pre_min_raw) if pre_min_raw not in (None, "", 0) else 0
    except (TypeError, ValueError):
        pre_min = 0
    pre_min_str = str(pre_min) if pre_min > 0 else ""

    new_id = next_independent_task_id([t.task_id for t in load_independent_tasks(ctx.store)])
    deadline_sheet = fmt_ts(deadline_dt)
    tz = timezone_name()
    assignee_id = contact.identity

    append_independent_task(
        ctx.store,
        IndependentTask(
            task_id=new_id,
            creator_id=ctx.sender_id,
            creator_name=sender.name,
            assignee_id=assignee_id,
            assignee_name=contact.name,
            description=description,
            status=STATUS_ASSIGNED,
            deadline=deadline_sheet,
            pre_deadline_reminder_minutes=pre_min_str,
            last_reminder_sent_at="",
            escalation_count=0,
            escalated_at="",
            completed_at="",
            original_request_msg_id=ctx.msg.msg_id,
            completion_msg_id="",
            notes=f"Independent task created from {ctx.channel.name} on {fmt_ts(now_local())}.",
        ),
    )

    # The subject leads with the description; the opaque ID stays in the body.
    assignee_subject = subject_line(f"New task: {short(description)}")
    bullets_en = [f"  • Task: {description}", f"  • Due: {deadline_sheet} ({tz})"]
    bullets_ar = [f"  • المهمة: {description}", f"  • الموعد النهائي: {deadline_sheet} ({tz})"]
    if pre_min > 0:
        bullets_en.append(f"  • Reminders: every {pre_min} minutes before the deadline")
        bullets_ar.append(f"  • التذكير: كل {pre_min} دقيقة قبل الموعد النهائي")

    assignee_body_en = (
        f"Hi {contact.name},\n\n"
        f"{sender.name} has assigned you a new task.\n\n"
        + "\n".join(bullets_en)
        + "\n\nReply YES once it's done, or let me know if you need a new deadline.\n\n"
        f"Reference: [{new_id}]."
    )
    assignee_body_ar = (
        f"مرحبا {contact.name}،\n\n"
        f"{sender.name} قام بإسناد مهمة جديدة إليك.\n\n"
        + "\n".join(bullets_ar)
        + "\n\nأجب YES عند الانتهاء، أو أخبرني إن احتجت موعدا جديدا.\n\n"
        f"المرجع: [{new_id}]."
    )
    try:
        ctx.channel.send_new(
            contact,
            subject=assignee_subject,
            body=ack(assignee_body_en, assignee_body_ar, ctx.lang),
        )
    except Exception:
        log.exception("failed to send instant notification to %s for %s", contact.name, new_id)

    try:
        append_conversation(
            ctx.store,
            task_id=new_id,
            direction="outbound",
            participant_id=assignee_id,
            participant_role="assignee",
            channel=ctx.channel.name,
            msg_id="",
            purpose="assignment",
            body=assignee_body_en,
            awaiting_reply=True,
        )
    except Exception:
        log.exception("failed to append assignment conversation for %s", new_id)

    extras_note_en = (
        f"\n\nNote: assigned only to {contact.name}; ignored extras: " + ", ".join(extras) + "."
        if extras
        else ""
    )
    extras_note_ar = (
        f"\n\nملاحظة: تم إسناد {contact.name} فقط؛ تم تجاهل: " + ", ".join(extras) + "."
        if extras
        else ""
    )
    confirm_bullets_en = [
        f"  • Task: {description}",
        f"  • Assignee: {contact.name}",
        f"  • Due: {deadline_sheet} ({tz})",
    ]
    confirm_bullets_ar = [
        f"  • المهمة: {description}",
        f"  • المسند إليه: {contact.name}",
        f"  • الموعد النهائي: {deadline_sheet} ({tz})",
    ]
    if pre_min > 0:
        confirm_bullets_en.append(f"  • Reminders: every {pre_min} minutes before the deadline")
        confirm_bullets_ar.append(f"  • التذكير: كل {pre_min} دقيقة قبل الموعد النهائي")
    ctx.reply(
        "Created an independent task (no project).\n\n"
        + "\n".join(confirm_bullets_en)
        + f"\n\nReference: [{new_id}]."
        + extras_note_en,
        "تم إنشاء مهمة مستقلة (بدون مشروع).\n\n"
        + "\n".join(confirm_bullets_ar)
        + f"\n\nالمرجع: [{new_id}]."
        + extras_note_ar,
    )
    log_activity(
        ctx.store,
        "Independent task assigned",
        f"{new_id} / {contact.name}",
        f"creator={sender.name}; deadline={deadline_sheet}; "
        f"pre_deadline_min={pre_min or '-'}; via {ctx.channel.name}.",
    )
