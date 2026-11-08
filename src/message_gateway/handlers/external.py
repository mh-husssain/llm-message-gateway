"""Hand-offs to outside parties.

After a step is handed off (see task_completion), the assignee reports how
long the outside party needs (external_eta_response, proposed to the owner)
and later that the party delivered (external_completion_confirmation).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..approvals import propose_to_owner
from ..clock import DATE_FMT, fmt_ts, now_local
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
    STATUS_AWAITING_EXTERNAL_COMPLETION,
    STATUS_COMPLETED,
    Task,
)
from ..records import (
    load_conversations,
    load_tasks,
    log_activity,
    mark_conversation_replied,
    update_task_fields,
)
from ..text import ack, subject_line
from ..workflow import promote_next_sibling
from .common import project_cell
from .completion import complete_parent_if_done


def _pick(ctx: HandlerContext, status: str) -> tuple[Task | None, list[Task]]:
    """The sender's single task in `status`, or the one named by task_id_hint."""
    task_id_hint = (ctx.extracted.get("task_id_hint") or "").strip() or None
    candidates = [
        t for t in load_tasks(ctx.store) if t.assignee_id == ctx.sender_id and t.status == status
    ]
    target = None
    if task_id_hint:
        target = next((t for t in candidates if t.task_id == task_id_hint), None)
    if target is None and len(candidates) == 1:
        target = candidates[0]
    return target, candidates


def handle_external_eta_response(ctx: HandlerContext) -> None:
    """The assignee answers 'how long does the outside party need?'."""
    ex = ctx.extracted
    sender = ctx.sender
    target, candidates = _pick(ctx, STATUS_AWAITING_ASSIGNEE_ETA)
    if target is None:
        if not candidates:
            ctx.reply(
                "I'm not currently waiting on an external timeline from you.",
                "لا أنتظر منك تحديث عن طرف خارجي حاليا.",
            )
        else:
            opts = ", ".join(f"{t.task_id} ({t.external_party})" for t in candidates[:5])
            ctx.reply(f"Which task — {opts}?", f"أي مهمة — {opts}؟")
        log_activity(
            ctx.store,
            "ETA orphan",
            sender.name,
            f"Could not pair external_eta_response. Candidates: {len(candidates)}.",
        )
        return

    try:
        eta_days = int(ex.get("eta_days") or 0)
    except (TypeError, ValueError):
        eta_days = 0
    if eta_days <= 0 and ex.get("eta_date_iso"):
        try:
            target_date = datetime.strptime(ex["eta_date_iso"], "%Y-%m-%d").date()
            eta_days = (target_date - now_local().date()).days
        except (ValueError, TypeError):
            eta_days = 0
    if eta_days <= 0:
        ctx.reply("How many days exactly?", "كم يوم بالضبط؟")
        return

    new_due_sheet = (now_local() + timedelta(days=eta_days)).strftime(DATE_FMT)
    external_party = ex.get("external_party_hint") or target.external_party or "the external party"

    propose_to_owner(
        store=ctx.store,
        channel=ctx.channel,
        task=target,
        contacts=ctx.contacts,
        kind="external_eta",
        payload={"new_due": new_due_sheet, "eta_days": eta_days, "external_party": external_party},
        lang=ctx.lang,
    )
    # The proposal opens its own owner row; close the assignee's eta_request.
    eta_pending = next(
        (
            c
            for c in load_conversations(ctx.store)
            if c.task_id == target.task_id and c.purpose == "eta_request" and c.awaiting_reply
        ),
        None,
    )
    if eta_pending:
        mark_conversation_replied(ctx.store, eta_pending)

    plain = ack(
        f"Thanks — asking the owner about the {eta_days}-day timeline from {external_party}.",
        f"شكرا — أسأل المسؤول عن مدة {eta_days} يوم من {external_party}.",
        ctx.lang,
    )
    ctx.reply_block(
        plain,
        render_action_badge("ETA Pending Approval", "warning", dot=True)
        + render_detail_card(
            [
                ("Task ID", render_id_chip(target.task_id)),
                ("Project", project_cell(target)),
                ("External party", external_party),
                ("Proposed timeline", f"{eta_days} days"),
                ("Proposed deadline", new_due_sheet),
                ("Status", render_status_chip("Awaiting Approval", "warning")),
            ]
        ),
    )
    log_activity(
        ctx.store,
        "External ETA proposed",
        f"{target.task_id} / {sender.name}",
        f"{eta_days} days → {new_due_sheet}; awaiting owner.",
    )


def handle_external_completion_confirmation(ctx: HandlerContext) -> None:
    """The assignee reports the outside party delivered (e.g. an approval)."""
    ex = ctx.extracted
    sender = ctx.sender
    target, candidates = _pick(ctx, STATUS_AWAITING_EXTERNAL_COMPLETION)
    if target is None:
        if not candidates:
            ctx.reply(
                "I don't see a task awaiting external completion for you.",
                "لا توجد مهمة بانتظار طرف خارجي لك.",
            )
        else:
            opts = ", ".join(f"{t.task_id} ({t.external_party})" for t in candidates[:5])
            ctx.reply(f"Which one — {opts}?", f"أيها — {opts}؟")
        return

    artifact = (ex.get("external_artifact") or "").strip()
    stamp = fmt_ts(now_local())
    update_task_fields(
        ctx.store,
        target,
        status=STATUS_COMPLETED,
        completed_at=stamp,
        completion_msg_id=ctx.msg.msg_id,
        awaiting_response_from="",
        awaiting_response_since="",
        notes=(
            target.notes + f" | external completion at {stamp}: {artifact or target.external_party}"
        ).strip(" |"),
    )

    tasks = load_tasks(ctx.store)
    target_fresh = next((t for t in tasks if t.task_id == target.task_id), target)
    promoted = promote_next_sibling(ctx.store, target_fresh, tasks)
    parent_completed = complete_parent_if_done(ctx.store, target_fresh, tasks, stamp)
    has_next = bool(promoted and promoted.task_id != target.task_id)

    owner = ctx.contacts.by_identity(target.owner_id)
    if owner:
        ext = target.external_party or "the external party"
        body_en = (
            f"{sender.name} confirmed {ext} delivered "
            f"{artifact or target.description} for {target.task_id}."
        )
        body_ar = (
            f"{sender.name} أكد أن {ext} سلّم {artifact or target.description} لـ {target.task_id}."
        )
        if has_next:
            body_en += f"\n\nNext step now active: {promoted.task_id} ({promoted.description})."
            body_ar += f"\n\nالخطوة التالية فعالة: {promoted.task_id} ({promoted.description})."
        if parent_completed:
            body_en += f"\n\nWorkflow {target.parent_task_id} fully complete."
            body_ar += f"\n\nسير العمل {target.parent_task_id} مكتمل."
        ctx.channel.send_new(
            owner,
            subject=subject_line(f"{target.task_id}: external completion"),
            body=ack(body_en, body_ar, ctx.lang),
        )

    plain = ack(
        f"Excellent — {target.task_id} marked complete.",
        f"ممتاز — {target.task_id} مكتمل.",
        ctx.lang,
    )
    detail_rows = [
        ("Task ID", render_id_chip(target.task_id)),
        ("Project", project_cell(target)),
        ("External party", target.external_party or "the external party"),
    ]
    if artifact:
        detail_rows.append(("Delivered", artifact))
    detail_rows.extend([("Completed at", stamp), ("Status", render_status_chip("Completed"))])
    if has_next:
        detail_rows.append(
            ("Next step", f"{render_id_chip(promoted.task_id)} {promoted.description}")
        )
    ctx.reply_block(
        plain,
        render_action_badge("External Completion", "confirmed")
        + render_description_panel(target.description)
        + render_detail_card(detail_rows),
    )
    log_activity(
        ctx.store,
        "External completion",
        f"{target.task_id} / {sender.name}",
        f"Artifact={artifact or '-'}; promoted={promoted.task_id if promoted else 'none'}; "
        f"parent_done={parent_completed}.",
    )
