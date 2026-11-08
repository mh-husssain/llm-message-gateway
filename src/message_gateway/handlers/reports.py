"""end_of_day_report: an employee logs hours against their tasks."""

from __future__ import annotations

import logging

from ..context import HandlerContext
from ..email_format import (
    ACCENT,
    TEXT_DARK,
    escape,
    render_action_badge,
    render_description_panel,
    render_detail_card,
    render_stats_grid,
)
from ..records import append_conversation, load_tasks, log_activity
from ..workflow import find_task_by_hint

log = logging.getLogger("message_gateway.handlers.reports")


def handle_end_of_day_report(ctx: HandlerContext) -> None:
    """Each `{hint, hours}` mention is matched against the sender's open tasks
    (exact T-ID, else description substring). Matches get an eod_log
    conversation row; unmatched hints are reported back rather than dropped."""
    raw_mentions = ctx.extracted.get("task_mentions") or []
    if not isinstance(raw_mentions, list) or not raw_mentions:
        ctx.reply(
            "I didn't pick up any tasks in your end-of-day report. "
            "Try: 'EOD: T-0030 4h, T-0031 3h'.",
            "لم ألتقط أي مهام في تقريرك اليومي. "
            "جرب: 'نهاية اليوم: T-0030 4 ساعات، T-0031 3 ساعات'.",
        )
        return

    sender = ctx.sender
    tasks = load_tasks(ctx.store)
    lang = ctx.lang

    resolved_lines: list[str] = []
    unresolved_lines: list[str] = []
    resolved_data: list[tuple[str, str, float]] = []
    unresolved_hints: list[tuple[str, float]] = []
    total_hours = 0.0

    for m in raw_mentions:
        if not isinstance(m, dict):
            continue
        hint = str(m.get("hint") or "").strip()
        try:
            hours = float(m.get("hours") or 0)
        except (TypeError, ValueError):
            hours = 0.0
        if not hint:
            continue

        # The model sometimes puts a T-ID straight into `hint`.
        task_id_hint = hint if hint.upper().startswith("T-") else None
        target, err = find_task_by_hint(
            tasks,
            ctx.sender_id,
            task_id_hint=task_id_hint,
            description_hint=None if task_id_hint else hint,
        )
        if target is None or err in ("id_not_found", "ambiguous"):
            unresolved_lines.append(
                f"{hint} ({hours}h)" if lang == "en" else f"{hint} ({hours} ساعة)"
            )
            unresolved_hints.append((hint, hours))
            continue

        try:
            append_conversation(
                ctx.store,
                task_id=target.task_id,
                direction="inbound",
                participant_id=ctx.sender_id,
                participant_role="assignee",
                channel=ctx.channel.name,
                msg_id=ctx.msg.msg_id,
                purpose="eod_log",
                body=f"EOD: {hours}h on {target.task_id} (hint='{hint}')",
                awaiting_reply=False,
            )
        except Exception:
            log.exception("eod_log conversation append failed for %s", target.task_id)
        log_activity(
            ctx.store,
            "EOD logged",
            f"{target.task_id} / {sender.name}",
            f"{hours}h reported (hint='{hint[:60]}').",
        )
        total_hours += hours
        resolved_lines.append(
            f"{target.task_id}: {hours}h" if lang == "en" else f"{target.task_id}: {hours} ساعة"
        )
        resolved_data.append((target.task_id, target.description or "(no description)", hours))

    if not resolved_lines and not unresolved_lines:
        ctx.reply(
            "I picked up no tasks in your end-of-day report.",
            "لم ألتقط أي مهام في تقريرك اليومي.",
        )
        return

    if lang == "en":
        head = f"Logged your end-of-day report ({total_hours:g}h total):"
        unres_label = "Couldn't match"
    else:
        head = f"تم تسجيل تقرير نهاية اليوم ({total_hours:g} ساعة إجمالي):"
        unres_label = "تعذر مطابقة"
    parts = [head]
    parts.extend(f"  - {ln}" for ln in resolved_lines)
    if unresolved_lines:
        parts.append(f"{unres_label}: " + ", ".join(unresolved_lines))
    plain_body = "\n".join(parts)

    blocks_html = [
        render_action_badge("End-of-Day Logged", "confirmed"),
        render_stats_grid(
            [
                ("Tasks", len(resolved_data), "info"),
                ("Hours", f"{total_hours:g}", "confirmed"),
                ("Unmatched", len(unresolved_hints), "warning" if unresolved_hints else "neutral"),
            ]
        ),
    ]
    if resolved_data:
        rows: list[tuple[str, str]] = []
        for tid, desc, hrs in resolved_data:
            short_desc = desc if len(desc) <= 60 else desc[:57] + "…"
            value_html = (
                f'<span style="color:{TEXT_DARK};">{escape(short_desc)}</span> '
                f'<span style="color:{ACCENT};font-weight:700;">&middot; {hrs:g}h</span>'
            )
            rows.append((tid, value_html))
        blocks_html.append(render_detail_card(rows))
    if unresolved_hints:
        unres_text = ", ".join(f"{h} ({hrs:g}h)" for h, hrs in unresolved_hints)
        blocks_html.append(
            render_action_badge("Unmatched Hints", "warning", dot=True)
            + render_description_panel(
                f"Couldn't pair these to an open task of yours: {unres_text}. "
                "Reply with a T-ID and I'll log them."
            )
        )
    ctx.reply_block(plain_body, "".join(blocks_html))
