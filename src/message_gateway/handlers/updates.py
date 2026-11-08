"""task_update: progress notes, blockers and due-date changes."""

from __future__ import annotations

from typing import Any

from ..clock import fmt_ts, iso_to_sheet_date, now_local
from ..context import HandlerContext
from ..email_format import (
    render_action_badge,
    render_description_panel,
    render_detail_card,
    render_id_chip,
    render_status_chip,
)
from ..models import STATUS_ASSIGNED, STATUS_IN_PROGRESS, TERMINAL_STATUSES, Task
from ..records import load_tasks, log_activity, update_task_fields
from ..text import ack, humanize_status
from ..workflow import can_act_on_task, find_task_by_hint, find_task_by_id
from .common import project_cell


def _find_target(
    ctx: HandlerContext, task_id_hint: str | None, description_hint: str | None
) -> Task | None:
    tasks = load_tasks(ctx.store)
    if task_id_hint:
        # Cross-assignee lookup with a permission gate: managers routinely
        # change dates or leave notes on employees' tasks.
        candidate = find_task_by_id(tasks, task_id_hint)
        if candidate is None:
            ctx.reply(
                f"I don't see a task {task_id_hint}. Could you double-check the ID?",
                f"لا أجد المهمة {task_id_hint}. يرجى التحقق من الرقم.",
            )
            log_activity(
                ctx.store,
                "Update id_not_found",
                ctx.sender.name,
                f"task_id_hint={task_id_hint} not present in Tasks.",
            )
            return None
        if not can_act_on_task(ctx.sender, candidate):
            ctx.reply(
                f"You don't have permission to update {candidate.task_id}.",
                f"ليس لديك صلاحية لتحديث {candidate.task_id}.",
            )
            log_activity(
                ctx.store,
                "Update permission denied",
                ctx.sender.name,
                f"{candidate.task_id} owned by {candidate.owner_id}, "
                f"assignees={candidate.assignee_id}.",
            )
            return None
        if candidate.status in TERMINAL_STATUSES:
            ctx.reply(
                f"{candidate.task_id} is already {humanize_status(candidate.status, 'en')}; "
                "updates are ignored.",
                f"{candidate.task_id} حالتها {humanize_status(candidate.status, 'ar')} "
                "— التحديث متجاهل.",
            )
            return None
        return candidate

    # No T-ID: the assignee is narrating their own work.
    target, err = find_task_by_hint(
        tasks, ctx.sender_id, task_id_hint=None, description_hint=description_hint
    )
    if err == "no_open_tasks":
        ctx.reply(
            "No open tasks for you to update. Mention a task ID if I'm missing one.",
            "لا توجد مهام مفتوحة لتحديثها. اذكر رقم المهمة إذا كان لديك واحدة.",
        )
        return None
    if err == "ambiguous":
        ctx.reply(
            "More than one task matches — which one (use the T-ID)?",
            "أكثر من مهمة تتطابق — أيها (اذكر T-ID)؟",
        )
        return None
    if not target:
        ctx.reply(
            "I couldn't find that task. Mention a T-ID and I'll log it.",
            "لم أجد المهمة. اذكر T-ID وسأسجله.",
        )
        return None
    return target


def handle_task_update(ctx: HandlerContext) -> None:
    ex = ctx.extracted
    sender = ctx.sender
    task_id_hint = (ex.get("task_id") or "").strip() or None
    new_due_date_iso = (ex.get("new_due_date_iso") or "").strip() or None
    description_hint = (ex.get("task_description_hint") or "").strip() or None
    is_blocker = bool(ex.get("is_blocker"))
    raw_update_text = (ex.get("update_text") or "").strip()
    only_due_date_change = bool(new_due_date_iso) and not raw_update_text and not is_blocker

    target = _find_target(ctx, task_id_hint, description_hint)
    if target is None:
        return

    field_updates: dict[str, Any] = {}
    note_parts: list[str] = []

    # Only the owner or a manager/admin may reschedule.
    if new_due_date_iso:
        if not (sender.is_admin or sender.can_assign or ctx.sender_id == target.owner_id):
            ctx.reply(
                f"Only the task owner or a manager can change the due date for {target.task_id}.",
                f"فقط مالك المهمة أو المدير يمكنه تعديل تاريخ الاستحقاق لـ {target.task_id}.",
            )
            log_activity(
                ctx.store,
                "Update due-date denied",
                sender.name,
                f"{target.task_id} owner={target.owner_id}.",
            )
            return
        new_due_sheet = iso_to_sheet_date(new_due_date_iso)
        if not new_due_sheet:
            ctx.reply(
                f"I couldn't parse '{new_due_date_iso}' as a date. Use DD/MM/YYYY.",
                f"تعذر فهم التاريخ '{new_due_date_iso}'. استخدم DD/MM/YYYY.",
            )
            return
        field_updates["due_date"] = new_due_sheet
        note_parts.append(
            f"due_date {target.due_date or '(none)'} → {new_due_sheet} via {ctx.channel.name}"
        )

    # Progress narrative. A pure date change doesn't copy the body into notes.
    progress_text = raw_update_text or (
        ctx.msg.body[:300].strip() if not only_due_date_change else ""
    )
    if progress_text or is_blocker:
        new_status = STATUS_IN_PROGRESS if target.status == STATUS_ASSIGNED else target.status
        if new_status != target.status:
            field_updates["status"] = new_status
        note_parts.append(("[BLOCKER] " if is_blocker else "") + (progress_text or "(no detail)"))

    if not field_updates and not note_parts:
        ctx.reply(
            f"I see {target.task_id} but no change was specified. "
            "Send a new due date, a progress note, or a blocker.",
            f"تعرّفت على {target.task_id} لكن لم تطلب تعديلا. "
            "أرسل تاريخ استحقاق جديد، ملاحظة تقدم، أو عائق.",
        )
        return

    if note_parts:
        stamp = fmt_ts(now_local())
        combined_note = " | ".join(f"{stamp}: {p}" for p in note_parts)
        field_updates["notes"] = (target.notes + " | " + combined_note).strip(" |")

    old_due = target.due_date or "Not set"
    update_task_fields(ctx.store, target, **field_updates)

    if "due_date" in field_updates and not progress_text and not is_blocker:
        plain = ack(
            f"Updated due date on {target.task_id} to {field_updates['due_date']}.",
            f"تم تحديث تاريخ الاستحقاق لـ {target.task_id} إلى {field_updates['due_date']}.",
            ctx.lang,
        )
        badge_label = "Deadline Updated"
    elif "due_date" in field_updates:
        plain = ack(
            f"Logged update on {target.task_id}; due date now {field_updates['due_date']}.",
            f"تم تسجيل التحديث على {target.task_id}؛ "
            f"تاريخ الاستحقاق الآن {field_updates['due_date']}.",
            ctx.lang,
        )
        badge_label = "Task Updated"
    else:
        plain = ack(
            f"Logged update on {target.task_id}.",
            f"تم تسجيل التحديث على {target.task_id}.",
            ctx.lang,
        )
        badge_label = "Task Updated"

    detail_rows = [
        ("Task ID", render_id_chip(target.task_id)),
        ("Project", project_cell(target)),
    ]
    if "due_date" in field_updates:
        detail_rows.append(("Old deadline", old_due))
        detail_rows.append(("New deadline", field_updates["due_date"]))
    if is_blocker:
        detail_rows.append(("Flag", render_status_chip("Blocker", "warning")))
    final_status = field_updates.get("status", target.status)
    detail_rows.append(("Status", render_status_chip(humanize_status(final_status, "en"))))

    blocks = [render_action_badge(badge_label, "updated")]
    if progress_text:
        blocks.append(render_description_panel(progress_text))
    blocks.append(render_detail_card(detail_rows))
    ctx.reply_block(plain, "".join(blocks))

    log_activity(
        ctx.store,
        "Task update",
        f"{target.task_id} / {sender.name}",
        f"Blocker={is_blocker}; due_changed={'due_date' in field_updates}; "
        f"snippet: {(progress_text or '')[:80]}",
    )
