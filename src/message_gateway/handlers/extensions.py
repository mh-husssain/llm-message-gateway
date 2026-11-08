"""request_extension and delay_acknowledgment.

Assignees' requests go to the task owner for a YES/NO. Managers and the task
owner are the approvers themselves, so their extensions apply directly. A
manager's free-form "approve X's extension" answers a pending proposal.
"""

from __future__ import annotations

from datetime import timedelta

from ..approvals import apply_owner_response, propose_to_owner
from ..clock import DATE_FMT, format_display_date, iso_to_sheet_date, now_local, parse_sheet_date
from ..context import HandlerContext
from ..email_format import (
    render_action_badge,
    render_description_panel,
    render_detail_card,
    render_id_chip,
    render_status_chip,
)
from ..models import (
    STATUS_AWAITING_ASSIGNEE_COMPLETION,
    STATUS_IN_PROGRESS,
    TERMINAL_STATUSES,
    Task,
)
from ..records import (
    load_conversations,
    load_tasks,
    log_activity,
    mark_conversation_replied,
    update_task_fields,
)
from ..text import ack, humanize_status
from ..workflow import (
    can_act_on_task,
    clear_late_completion_state,
    find_task_by_hint,
    find_task_by_id,
)
from .common import project_cell


def _explicit_target(
    ctx: HandlerContext,
    tasks: list[Task],
    task_id_hint: str,
    *,
    log_prefix: str,
    verb_en: str,
    verb_ar: str,
    nothing_en: str,
    nothing_ar: str,
) -> Task | None:
    """T-ID lookup across assignees (managers extend on employees' behalf),
    then terminal-status and task-scoped permission checks."""
    candidate = find_task_by_id(tasks, task_id_hint)
    if candidate is None:
        ctx.reply(
            f"I don't see a task {task_id_hint}. Could you double-check the ID?",
            f"لا أجد المهمة {task_id_hint}. يرجى التحقق من الرقم.",
        )
        log_activity(
            ctx.store,
            f"{log_prefix} id_not_found",
            ctx.sender.name,
            f"task_id_hint={task_id_hint} not present in Tasks.",
        )
        return None
    if candidate.status in TERMINAL_STATUSES:
        ctx.reply(
            f"{candidate.task_id} is already {humanize_status(candidate.status, 'en')}{nothing_en}",
            f"{candidate.task_id} حالتها {humanize_status(candidate.status, 'ar')}{nothing_ar}",
        )
        return None
    if not can_act_on_task(ctx.sender, candidate):
        ctx.reply(
            f"You don't have permission to {verb_en} {candidate.task_id}.",
            f"ليس لديك صلاحية {verb_ar} {candidate.task_id}.",
        )
        log_activity(
            ctx.store,
            f"{log_prefix} permission denied",
            ctx.sender.name,
            f"{candidate.task_id} owned by {candidate.owner_id}.",
        )
        return None
    return candidate


def _approve_pending_for_manager(ctx: HandlerContext, tasks: list[Task]) -> bool:
    """If the sender has exactly one pending owner_confirmation_request (narrowed
    by a named assignee when given), treat the message as YES on it.

    Returns True when the message was fully handled here.
    """
    target_assignee_hint = (ctx.extracted.get("target_assignee_name_hint") or "").strip().lower()
    pending_for_sender = [
        c
        for c in load_conversations(ctx.store)
        if c.awaiting_reply
        and c.direction == "outbound"
        and c.purpose == "owner_confirmation_request"
        and c.participant_id == ctx.sender_id
    ]
    if pending_for_sender and target_assignee_hint:
        named = [
            c
            for c in pending_for_sender
            if any(
                target_assignee_hint in t.assignee_name.lower()
                for t in tasks
                if t.task_id == c.task_id
            )
        ]
        if named:
            pending_for_sender = named

    if len(pending_for_sender) == 1:
        pending = pending_for_sender[0]
        target = next((t for t in tasks if t.task_id == pending.task_id), None)
        if target is None:
            return False
        applied = apply_owner_response(
            store=ctx.store,
            channel=ctx.channel,
            task=target,
            confirmed=True,
            contacts=ctx.contacts,
            lang=ctx.lang,
        )
        mark_conversation_replied(ctx.store, pending)
        if not applied:
            ctx.reply(
                "Couldn't apply that approval — please contact admin.",
                "تعذّر تطبيق الموافقة — يرجى التواصل مع الإدارة.",
            )
            return True
        new_due_h = format_display_date(target.due_date) or "unchanged"
        plain = ack(
            f"Approved — {target.task_id} extension applied. New due: {new_due_h}.",
            f"تمت الموافقة — {target.task_id} تم تمديده. "
            f"الموعد الجديد: {format_display_date(target.due_date) or 'بدون تغيير'}.",
            ctx.lang,
        )
        ctx.reply_block(
            plain,
            render_action_badge("Extension Approved", "updated")
            + render_detail_card(
                [
                    ("Task ID", render_id_chip(target.task_id)),
                    ("Project", project_cell(target)),
                    ("Assignee", target.assignee_name or "—"),
                    ("New deadline", new_due_h),
                    ("Status", render_status_chip(humanize_status(target.status, "en"))),
                ]
            ),
        )
        log_activity(
            ctx.store,
            "Extension approved (NL)",
            f"{target.task_id} / {ctx.sender.name}",
            f"target_assignee_hint='{target_assignee_hint or '-'}' "
            "resolved via pending owner_confirmation_request.",
        )
        return True

    if len(pending_for_sender) > 1:
        options = []
        for c in pending_for_sender[:5]:
            t = next((tt for tt in tasks if tt.task_id == c.task_id), None)
            if t:
                options.append(f"{t.task_id} ({t.assignee_name} on {t.project_id})")
        opts_text = ", ".join(options) or "(no task details)"
        ctx.reply(
            f"You have {len(pending_for_sender)} pending extension approvals: "
            f"{opts_text}. Reply with the T-ID to confirm one.",
            f"لديك {len(pending_for_sender)} طلبات تمديد بانتظار موافقتك: "
            f"{opts_text}. أرسل T-ID للموافقة على أحدها.",
        )
        log_activity(
            ctx.store,
            "Extension approval ambiguous",
            ctx.sender.name,
            f"{len(pending_for_sender)} pending; target_hint='{target_assignee_hint or '-'}'.",
        )
        return True
    return False


def handle_request_extension(ctx: HandlerContext) -> None:
    ex = ctx.extracted
    sender = ctx.sender
    tasks = load_tasks(ctx.store)
    task_id_hint = (ex.get("task_id") or "").strip() or None

    if not task_id_hint and _approve_pending_for_manager(ctx, tasks):
        return

    if task_id_hint:
        target = _explicit_target(
            ctx,
            tasks,
            task_id_hint,
            log_prefix="Extension",
            verb_en="extend",
            verb_ar="لتمديد",
            nothing_en="; no deadline to extend.",
            nothing_ar=" — لا يوجد موعد للتمديد.",
        )
        if target is None:
            return
    else:
        target, err = find_task_by_hint(
            tasks,
            ctx.sender_id,
            task_id_hint=None,
            description_hint=ex.get("task_description_hint"),
        )
        if err == "no_open_tasks" or target is None:
            ctx.reply(
                "I don't see a task to extend. Reference the T-ID.",
                "لا توجد مهمة لتمديدها. اذكر T-ID.",
            )
            return

    requested_iso = ex.get("requested_new_date_iso")
    extra_days = ex.get("requested_extra_days")
    new_due_sheet = iso_to_sheet_date(requested_iso) if requested_iso else ""
    if not new_due_sheet and extra_days:
        try:
            extra = int(extra_days)
        except (TypeError, ValueError):
            extra = 1
        base = parse_sheet_date(target.due_date) or now_local()
        new_due_sheet = (base + timedelta(days=extra)).strftime(DATE_FMT)
    if not new_due_sheet:
        ctx.reply("How many days more, or until what date?", "كم يوم إضافي، أو حتى أي تاريخ؟")
        return

    # Managers, admins and the task owner are the approvers: apply directly.
    if sender.can_assign or (ctx.sender_id and ctx.sender_id == target.owner_id):
        # A deliberate new date restarts the reminder cycle; an overdue task
        # also returns to in_progress.
        late_reset = clear_late_completion_state(target)
        status_field = (
            {"status": STATUS_IN_PROGRESS}
            if target.status == STATUS_AWAITING_ASSIGNEE_COMPLETION
            else {}
        )
        old_due = target.due_date or "Not set"
        update_task_fields(ctx.store, target, due_date=new_due_sheet, **late_reset, **status_field)
        plain = ack(
            f"Extended {target.task_id} to {new_due_sheet}.",
            f"تم تمديد {target.task_id} إلى {new_due_sheet}.",
            ctx.lang,
        )
        ctx.reply_block(
            plain,
            render_action_badge("Deadline Extended", "updated")
            + render_detail_card(
                [
                    ("Task ID", render_id_chip(target.task_id)),
                    ("Project", project_cell(target)),
                    ("Assignee", target.assignee_name or "—"),
                    ("Old deadline", old_due),
                    ("New deadline", new_due_sheet),
                    (
                        "Status",
                        render_status_chip(
                            humanize_status(status_field.get("status", target.status), "en")
                        ),
                    ),
                ]
            ),
        )
        log_activity(
            ctx.store,
            "Extension applied (direct)",
            f"{target.task_id} / {sender.name}",
            f"New due {new_due_sheet}; sender role={sender.permission}; "
            f"late-cycle reset={'yes' if status_field else 'n/a'}.",
        )
        return

    propose_to_owner(
        store=ctx.store,
        channel=ctx.channel,
        task=target,
        contacts=ctx.contacts,
        kind="delay",
        payload={"new_due": new_due_sheet, "reason": ex.get("reason", "")},
        lang=ctx.lang,
    )
    ctx.reply(
        f"Asking the manager about extending {target.task_id} to {new_due_sheet}. "
        "Will get back to you.",
        f"أسأل المدير عن تمديد {target.task_id} حتى {new_due_sheet}. سأرجع لك.",
    )
    log_activity(
        ctx.store,
        "Extension requested",
        f"{target.task_id} / {sender.name}",
        f"Proposed new due {new_due_sheet}.",
    )


def handle_delay_acknowledgment(ctx: HandlerContext) -> None:
    """Informal 'one more day': always proposed to the owner."""
    ex = ctx.extracted
    sender = ctx.sender
    tasks = load_tasks(ctx.store)
    task_id_hint = (ex.get("task_id") or "").strip() or None

    if task_id_hint:
        target = _explicit_target(
            ctx,
            tasks,
            task_id_hint,
            log_prefix="Delay",
            verb_en="delay",
            verb_ar="لتأجيل",
            nothing_en="; nothing to delay.",
            nothing_ar=" — لا شيء للتأجيل.",
        )
        if target is None:
            return
    else:
        # As in the original, an ambiguous match (target None) gets the same
        # "no task" reply as an empty pool.
        target, _err = find_task_by_hint(tasks, ctx.sender_id, task_id_hint=None)
        if target is None:
            ctx.reply(
                "I don't see an open task to delay. Reference the T-ID.",
                "لا توجد مهمة لتأجيلها. اذكر T-ID.",
            )
            return

    try:
        extra = int(ex.get("extra_days") or 1)
    except (TypeError, ValueError):
        extra = 1
    extra = max(extra, 1)
    base = parse_sheet_date(target.due_date) or now_local()
    new_due_sheet = (base + timedelta(days=extra)).strftime(DATE_FMT)

    delay_reason = (ex.get("reason") or "").strip() or f"{sender.name} needs {extra} more day(s)"
    propose_to_owner(
        store=ctx.store,
        channel=ctx.channel,
        task=target,
        contacts=ctx.contacts,
        kind="delay",
        payload={"new_due": new_due_sheet, "reason": delay_reason},
        lang=ctx.lang,
    )
    plain = ack(
        f"Got it — asking the manager to push {target.task_id} by {extra} day(s) "
        f"to {new_due_sheet}.",
        f"تمام — أسأل المدير لتأجيل {target.task_id} بـ {extra} يوم/أيام إلى {new_due_sheet}.",
        ctx.lang,
    )
    ctx.reply_block(
        plain,
        render_action_badge("Delay Pending Approval", "warning", dot=True)
        + render_description_panel(delay_reason)
        + render_detail_card(
            [
                ("Task ID", render_id_chip(target.task_id)),
                ("Project", project_cell(target)),
                ("Original deadline", target.due_date or "Not set"),
                ("Proposed deadline", new_due_sheet),
                ("Extra days", str(extra)),
                ("Status", render_status_chip("Awaiting Approval", "warning")),
            ]
        ),
    )
    log_activity(
        ctx.store,
        "Delay acknowledged",
        f"{target.task_id} / {sender.name}",
        f"+{extra} day(s) → {new_due_sheet}.",
    )
