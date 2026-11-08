"""completion_confirmation_response: a bare YES/NO.

The answer is paired in code with the sender's most recent outbound
conversation row that still awaits a reply; the row's purpose decides what
the YES/NO means. The model only reports `confirmed`.
"""

from __future__ import annotations

import dataclasses
import json

from ..approvals import apply_owner_response
from ..classifier import IntentResult
from ..clock import fmt_ts, now_local
from ..context import HandlerContext
from ..email_format import (
    render_action_badge,
    render_description_panel,
    render_detail_card,
    render_id_chip,
    render_status_chip,
)
from ..models import STATUS_COMPLETED, STATUS_IN_PROGRESS, Conversation, Project
from ..records import (
    append_project,
    latest_awaiting_for,
    load_contacts,
    load_conversations,
    load_projects,
    load_tasks,
    log_activity,
    mark_conversation_replied,
    update_task_fields,
)
from ..text import ack, humanize_status, subject_line
from ..workflow import promote_next_sibling
from .assignment import REGISTER_PROJECT_PREFIX, handle_assign_task
from .common import project_cell


def handle_confirmation_response(ctx: HandlerContext) -> None:
    confirmed = bool(ctx.extracted.get("confirmed"))
    pending = _latest_pending(ctx)
    if pending is None:
        ctx.reply(
            "Thanks — but I don't have a pending question for you right now.",
            "شكرا — ولكن ليس لدي سؤال معلق لك حاليا.",
        )
        log_activity(
            ctx.store,
            "Confirmation orphan",
            ctx.sender.name,
            f"Sender replied YES/NO but no awaiting conversation. confirmed={confirmed}",
        )
        return

    if pending.purpose == "due_check":
        _due_check(ctx, pending, confirmed)
    elif pending.purpose == "owner_confirmation_request":
        _owner_decision(ctx, pending, confirmed)
    elif pending.purpose == "register_project_request":
        _register_project(ctx, pending, confirmed)
    else:
        mark_conversation_replied(ctx.store, pending)
        ctx.reply("Noted — thanks for confirming.", "تم — شكرا للتأكيد.")
        log_activity(
            ctx.store,
            "Confirmation noted",
            f"{pending.task_id} / {ctx.sender.name}",
            f"Purpose={pending.purpose}, confirmed={confirmed}. No handler wired.",
        )


def _latest_pending(ctx: HandlerContext) -> Conversation | None:
    return latest_awaiting_for(load_conversations(ctx.store), ctx.sender_id)


def _due_check(ctx: HandlerContext, pending: Conversation, confirmed: bool) -> None:
    """The assignee was asked whether the task is done; YES completes it."""
    sender = ctx.sender
    tasks = load_tasks(ctx.store)
    target = next((t for t in tasks if t.task_id == pending.task_id), None)
    if target is None:
        mark_conversation_replied(ctx.store, pending)
        ctx.reply("That task can't be found anymore.", "لم أجد المهمة.")
        return
    if confirmed:
        update_task_fields(
            ctx.store,
            target,
            status=STATUS_COMPLETED,
            completed_at=fmt_ts(now_local()),
            completion_msg_id=ctx.msg.msg_id,
            notes=(
                target.notes + f" | completed via due_check confirmation at {fmt_ts(now_local())}"
            ).strip(" |"),
        )
        tasks = load_tasks(ctx.store)
        target_fresh = next((t for t in tasks if t.task_id == target.task_id), target)
        promoted = promote_next_sibling(ctx.store, target_fresh, tasks)
        owner = ctx.contacts.by_identity(target.owner_id)
        if owner:
            ctx.channel.send_new(
                owner,
                subject=subject_line(f"{target.task_id} completed by {sender.name}"),
                body=(
                    f"{sender.name} confirmed {target.task_id} ({target.description}) is complete."
                ),
            )
        plain = ack(
            f"Got it — {target.task_id} marked complete.", f"تم — {target.task_id} مكتمل.", ctx.lang
        )
        html_block = (
            render_action_badge("Task Completed", "confirmed")
            + render_description_panel(target.description)
            + render_detail_card(
                [
                    ("Task ID", render_id_chip(target.task_id)),
                    ("Project", project_cell(target)),
                    ("Completed by", sender.name),
                    ("Completed at", fmt_ts(now_local())),
                    ("Status", render_status_chip("Completed")),
                ]
            )
        )
        ctx.reply_block(plain, html_block)
        log_activity(
            ctx.store,
            "Due check confirmed",
            f"{target.task_id} / {sender.name}",
            f"Promoted={promoted.task_id if promoted else 'none'}.",
        )
    else:
        update_task_fields(
            ctx.store,
            target,
            status=STATUS_IN_PROGRESS,
            notes=(
                target.notes + f" | due_check declined at {fmt_ts(now_local())}: still in progress"
            ).strip(" |"),
        )
        plain = ack(
            f"Understood — {target.task_id} stays in progress.",
            f"فهمت — {target.task_id} لا يزال قيد التنفيذ.",
            ctx.lang,
        )
        html_block = render_action_badge("Status Unchanged", "info") + render_detail_card(
            [
                ("Task ID", render_id_chip(target.task_id)),
                ("Project", project_cell(target)),
                ("Status", render_status_chip("In Progress", "warning")),
            ]
        )
        ctx.reply_block(plain, html_block)
        log_activity(
            ctx.store,
            "Due check declined",
            f"{target.task_id} / {sender.name}",
            "Task remains in progress.",
        )
    mark_conversation_replied(ctx.store, pending)


def _owner_decision(ctx: HandlerContext, pending: Conversation, confirmed: bool) -> None:
    """The owner answers a delay / external_eta / cancel proposal."""
    tasks = load_tasks(ctx.store)
    target = next((t for t in tasks if t.task_id == pending.task_id), None)
    if target is None:
        mark_conversation_replied(ctx.store, pending)
        ctx.reply("That task can't be found anymore.", "لم أجد المهمة.")
        return
    applied = apply_owner_response(
        store=ctx.store,
        channel=ctx.channel,
        task=target,
        confirmed=confirmed,
        contacts=ctx.contacts,
        lang=ctx.lang,
    )
    mark_conversation_replied(ctx.store, pending)
    if not applied:
        ctx.reply(
            "Couldn't apply that decision — please contact admin.",
            "تعذّر تطبيق القرار — يرجى التواصل مع الإدارة.",
        )
        return
    after = next((t for t in load_tasks(ctx.store) if t.task_id == target.task_id), target)
    if confirmed:
        plain = ack(
            f"Confirmed — {target.task_id} updated.",
            f"تم التأكيد — {target.task_id} تم تحديثه.",
            ctx.lang,
        )
        badge = render_action_badge("Confirmed", "confirmed")
    else:
        plain = ack(
            f"Understood — {target.task_id} unchanged.",
            f"فهمت — {target.task_id} بدون تغيير.",
            ctx.lang,
        )
        badge = render_action_badge("Decision Logged", "info")
    ctx.reply_block(
        plain,
        badge
        + render_detail_card(
            [
                ("Task ID", render_id_chip(target.task_id)),
                ("Project", project_cell(after)),
                ("Assignee", after.assignee_name or "—"),
                ("Deadline", after.due_date or "Not set"),
                ("Status", render_status_chip(humanize_status(after.status, "en"))),
            ]
        ),
    )


def _register_project(ctx: HandlerContext, pending: Conversation, confirmed: bool) -> None:
    """A manager answers whether to register a project name seen only on past
    tasks. YES registers it and replays the parked assignment."""
    sender = ctx.sender
    raw = pending.body_excerpt or ""
    payload: dict = {}
    if raw.startswith(REGISTER_PROJECT_PREFIX):
        try:
            payload = json.loads(raw[len(REGISTER_PROJECT_PREFIX) :])
        except (json.JSONDecodeError, ValueError):
            payload = {}
    proj_name = (payload.get("name") or "").strip()
    candidate_pid = (payload.get("pid") or "").strip()
    original_ex = payload.get("ex") if isinstance(payload.get("ex"), dict) else {}

    mark_conversation_replied(ctx.store, pending)

    if not confirmed:
        ctx.reply(
            f"OK, didn't register {proj_name or 'that project'}. "
            "Send a different project ID and I'll retarget the assignment.",
            f"حسناً، لم أُسجِّل {proj_name or 'هذا المشروع'}. "
            "أرسل رقم مشروع مختلف وسأعيد توجيه المهمة.",
        )
        log_activity(
            ctx.store,
            "Project register declined",
            ctx.sender_id,
            f"Declined registration of '{proj_name}' ({candidate_pid}).",
        )
        return

    if not proj_name or not candidate_pid:
        ctx.reply(
            "Sorry — I lost the parked registration details. "
            "Send the assignment again and I'll re-prompt.",
            "عذراً — فقدت تفاصيل التسجيل. أعد إرسال طلب التعيين وسأطلب التأكيد مرة أخرى.",
        )
        log_activity(
            ctx.store, "Project register payload corrupt", sender.name, f"raw={raw[:120]!r}"
        )
        return

    # Register (skipped if it now exists, e.g. a concurrent request did it).
    if candidate_pid not in load_projects(ctx.store):
        append_project(
            ctx.store,
            Project(id=candidate_pid, name=proj_name, status="active", manager=sender.name),
        )
        log_activity(
            ctx.store,
            "Project registered (from prompt)",
            f"{candidate_pid} / {proj_name}",
            f"Registered via YES from {sender.name}.",
        )

    # Replay the parked extraction against the now-registered project.
    replayed_ex = dict(original_ex)
    replayed_ex["project_id"] = candidate_pid
    replayed_ex.pop("project_ref_unresolved", None)
    replayed = dataclasses.replace(
        ctx,
        intent=IntentResult(
            intent="assign_task",
            confidence=1.0,
            extracted=replayed_ex,
            reasoning="Replayed after register_project_request was approved.",
        ),
        projects=load_projects(ctx.store),
        contacts=load_contacts(ctx.store),
    )
    ctx.reply(
        f"Registered {candidate_pid} ({proj_name}). Creating the task now…",
        f"تم تسجيل {candidate_pid} ({proj_name}). جاري إنشاء المهمة…",
    )
    handle_assign_task(replayed)
