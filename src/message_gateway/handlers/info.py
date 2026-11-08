"""request_info: task lists, single-task status, the project list and the
on-demand manager summary. Every scope is permission-gated in code."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from ..clock import local_tz, now_local, parse_ts
from ..context import HandlerContext
from ..email_format import (
    LABEL_GREY,
    TEXT_DARK,
    TEXT_MUTED,
    escape,
    render_action_badge,
    render_description_panel,
    render_detail_card,
    render_id_chip,
    render_overflow_note,
    render_section_label,
    render_stats_grid,
    render_status_chip,
    render_task_table,
)
from ..models import STATUS_ASSIGNED, STATUS_COMPLETED, Project, Task
from ..records import load_tasks, log_activity
from ..schema import ACTIVITY_LOG
from ..text import (
    ack,
    format_project_ref,
    format_project_ref_from_obj,
    format_task_due,
    humanize_status,
)
from ..workflow import can_act_on_task, find_task_by_id, is_sender_assignee
from .common import project_cell, reply_project_not_found

STATUS_FILTERS = {"open", "completed", "all"}
DATE_FILTERS = {
    "overdue",
    "due_today",
    "tasks_due_today",
    "due_this_week",
    "due_next_week",
    "due_this_month",
}
MAX_SHOW = 10


def format_task_summary(
    tasks: list[Task],
    *,
    scope: str,
    person_name: str,
    project_id: str,
    projects: dict[str, Project],
    lang: str,
    max_show: int = MAX_SHOW,
    status_filter: str = "open",
    task_id_filter: str | None = None,
    include_delegated: bool = False,
    date_filter: str = "",
) -> str:
    """Reader-friendly list: each line leads with the description, then the
    status in plain words, the due date and the project name, with the ID
    trailing in brackets. The project is omitted per line when the header
    already names it."""
    project_label = format_project_ref(project_id, projects, lang) if project_id else ""

    date_adj_en, date_adj_ar = "", ""
    if date_filter == "overdue":
        date_adj_en, date_adj_ar = "overdue ", "المتأخرة "
    elif date_filter in ("due_today", "tasks_due_today"):
        date_adj_en, date_adj_ar = "due today ", "المستحقة اليوم "
    elif date_filter == "due_this_week":
        date_adj_en, date_adj_ar = "due this week ", "المستحقة هذا الأسبوع "
    elif date_filter == "due_next_week":
        date_adj_en, date_adj_ar = "due next week ", "المستحقة الأسبوع القادم "
    elif date_filter == "due_this_month":
        date_adj_en, date_adj_ar = "due this month ", "المستحقة هذا الشهر "

    if date_adj_en and status_filter in ("open", ""):
        adj_en, adj_ar = date_adj_en, date_adj_ar
    elif status_filter == "completed":
        adj_en, adj_ar = "completed " + date_adj_en, "المكتملة " + date_adj_ar
    elif status_filter == "open":
        adj_en, adj_ar = "open ", "المفتوحة "
    else:
        adj_en, adj_ar = date_adj_en, date_adj_ar

    en = lang == "en"
    if task_id_filter:
        header = f"Status of task {task_id_filter}" if en else f"حالة المهمة {task_id_filter}"
    elif scope == "my_tasks":
        if include_delegated:
            header = (
                f"Your {adj_en}tasks (assigned to you + delegated by you)"
                if en
                else f"مهامك {adj_ar}(المسندة إليك + التي وكّلتها)".rstrip()
            )
        else:
            header = f"Your {adj_en}tasks" if en else f"مهامك {adj_ar}".rstrip()
    elif scope == "tasks_due_today":
        header = "Your tasks due today" if en else "مهامك المستحقة اليوم"
    elif scope == "person_tasks":
        header = f"{person_name}'s {adj_en}tasks" if en else f"مهام {person_name} {adj_ar}".rstrip()
        if project_label:
            header += f" on {project_label}" if en else f" في {project_label}"
    elif scope == "project_tasks":
        header = (
            f"{adj_en.capitalize()}tasks for {project_label}".strip()
            if en
            else f"مهام {project_label} {adj_ar}".rstrip()
        )
    elif date_adj_en:
        header = f"Tasks {date_adj_en.strip()}" if en else f"المهام {date_adj_ar.strip()}"
    elif status_filter == "completed":
        header = "All completed tasks" if en else "كل المهام المكتملة"
    elif status_filter == "open":
        header = "All active tasks" if en else "كل المهام النشطة"
    else:
        header = "All tasks" if en else "كل المهام"

    if not tasks:
        return f"{header}: no matching tasks." if en else f"{header}: لا توجد مهام مطابقة."

    shown = tasks[:max_show]
    overflow = len(tasks) - len(shown)
    lines = [f"{header} ({len(tasks)} total):" if en else f"{header} ({len(tasks)} مهمة):"]

    suppress_project_per_line = scope == "project_tasks" or bool(task_id_filter)
    for t in shown:
        desc = (t.description or "").strip() or ("(no description)" if en else "(بدون وصف)")
        status_h = humanize_status(t.status, lang)
        due_h = format_task_due(t, lang)
        proj_inline = ""
        if not suppress_project_per_line and t.project_id:
            ref = format_project_ref(t.project_id, projects, lang)
            if ref:
                proj_inline = f", on {ref}" if en else f"، في {ref}"
        if en:
            lines.append(f"  • {desc} — {status_h}, due {due_h}{proj_inline} [{t.task_id}]")
        else:
            lines.append(f"  • {desc} — {status_h}، مستحقة {due_h}{proj_inline} [{t.task_id}]")

    if overflow > 0:
        lines.append(
            f"… +{overflow} more (showing first {max_show})"
            if en
            else f"… +{overflow} مهام إضافية (عرض أول {max_show})"
        )
    return "\n".join(lines)


# Activity_Log actions surfaced in the manager summary. Keep aligned with the
# log_activity labels in the handlers.
DAILY_SUMMARY_BUCKETS = (
    ("created", ("Task assigned", "Project created")),
    ("completed", ("Task completed",)),
    ("updated", ("Task update",)),
    ("extended", ("Extension applied (direct)", "Extension approved (NL)", "Extension requested")),
    ("delayed", ("Delay acknowledged",)),
    ("eod_logged", ("EOD logged",)),
)


def _extract_task_id_from_related(related: str) -> str:
    if not related:
        return ""
    m = re.search(r"T-\d{4,}", related)
    return m.group(0) if m else ""


def emit_daily_summary(ctx: HandlerContext) -> None:
    """Manager digest of today's activity from Activity_Log, plus open tasks
    due today."""
    lang = ctx.lang
    today = now_local().date()
    buckets: dict[str, list[str]] = {key: [] for key, _ in DAILY_SUMMARY_BUCKETS}
    for r in ctx.store.read(f"{ACTIVITY_LOG.name}!A:D"):
        if not r:
            continue
        ts = parse_ts(r[0]) if r[0] else None
        if not ts or ts.date() != today:
            continue
        action = r[1] if len(r) > 1 else ""
        related = r[2] if len(r) > 2 else ""
        for key, allowed_actions in DAILY_SUMMARY_BUCKETS:
            if action in allowed_actions:
                tid = _extract_task_id_from_related(related)
                buckets[key].append(tid or (related.split(" / ")[0] if related else action))
                break

    tasks = load_tasks(ctx.store)
    due_today = [
        t for t in tasks if t.is_open and t.due_date_obj and t.due_date_obj.date() == today
    ]

    today_str = today.strftime("%d/%m/%Y")
    if lang == "en":
        head = f"Daily summary for {today_str}:"
        labels = {
            "created": "Tasks created",
            "completed": "Tasks completed",
            "updated": "Tasks updated",
            "extended": "Extensions applied",
            "delayed": "Delays acknowledged",
            "eod_logged": "EOD entries",
        }
        due_label = "Open tasks due today"
        empty = "(none)"
    else:
        head = f"الملخص اليومي ليوم {today_str}:"
        labels = {
            "created": "مهام أُنشئت",
            "completed": "مهام مكتملة",
            "updated": "مهام محدّثة",
            "extended": "تمديدات مطبّقة",
            "delayed": "تأخيرات مسجّلة",
            "eod_logged": "تقارير نهاية اليوم",
        }
        due_label = "مهام مفتوحة مستحقة اليوم"
        empty = "(لا يوجد)"

    lines = [head]
    counts: dict[str, int] = {}
    items_per_bucket: dict[str, list[str]] = {}
    for key, _ in DAILY_SUMMARY_BUCKETS:
        uniq = list(dict.fromkeys(x for x in buckets[key] if x))  # dedupe, keep order
        lines.append(f"  - {labels[key]}: {len(uniq)} ({', '.join(uniq) if uniq else empty})")
        counts[key] = len(uniq)
        items_per_bucket[key] = uniq
    if due_today:
        ids = ", ".join(t.task_id for t in due_today[:10])
        more = f" … +{len(due_today) - 10}" if len(due_today) > 10 else ""
        lines.append(f"  - {due_label}: {len(due_today)} ({ids}{more})")
    else:
        lines.append(f"  - {due_label}: 0 ({empty})")
    plain_summary = "\n".join(lines)

    overdue_today = sum(
        1 for t in tasks if t.is_open and t.due_date_obj and t.due_date_obj.date() < today
    )
    blocks_html = [
        render_action_badge(f"Daily Summary · {today_str}", "info"),
        render_stats_grid(
            [
                ("Created", counts.get("created", 0), "info"),
                ("Completed", counts.get("completed", 0), "confirmed"),
                ("Overdue", overdue_today, "escalation" if overdue_today else "neutral"),
                ("Due today", len(due_today), "warning" if due_today else "neutral"),
            ]
        ),
    ]
    bucket_rows: list[tuple[str, str]] = []
    for key, _ in DAILY_SUMMARY_BUCKETS:
        items = items_per_bucket.get(key, [])
        if not items:
            continue
        chip_strs = [
            render_id_chip(x)
            if re.match(r"^[TPCI]-\d{3,5}$", x)
            else f'<span style="color:{TEXT_DARK};">{escape(x)}</span>'
            for x in items[:8]
        ]
        if len(items) > 8:
            chip_strs.append(
                f'<span style="color:{LABEL_GREY};font-size:11px;">… +{len(items) - 8} more</span>'
            )
        bucket_rows.append((labels[key], " ".join(chip_strs)))
    if bucket_rows:
        blocks_html.append(render_detail_card(bucket_rows))
    if due_today:
        blocks_html.append(render_section_label(due_label))
        blocks_html.append(render_task_table([_table_row(t) for t in due_today[:10]]))
        if len(due_today) > 10:
            blocks_html.append(
                render_overflow_note(f"… +{len(due_today) - 10} more (showing first 10)")
            )

    ctx.reply_block(plain_summary, "".join(blocks_html))
    log_activity(
        ctx.store,
        f"{ctx.channel.name} info request answered (daily_summary)",
        f"{ctx.sender.name} / {ctx.msg.sender_identity}",
        "Categories: "
        + ", ".join(f"{k}={len(buckets[k])}" for k, _ in DAILY_SUMMARY_BUCKETS)
        + f", due_today={len(due_today)}",
    )


def _table_row(t: Task) -> dict[str, str]:
    return {
        "id": t.task_id,
        "description": t.description or "(no description)",
        "deadline": t.due_date or "—",
        "status": humanize_status(t.status, "en"),
    }


def _projects_scope(ctx: HandlerContext, status_filter: str) -> None:
    """The noun is 'projects' itself: answer from the Projects tab."""
    lang = ctx.lang
    proj_list = list(ctx.projects.values())
    if status_filter == "open":
        proj_list = [p for p in proj_list if (p.status or "").lower() == "active"]
    elif status_filter == "completed":
        proj_list = [p for p in proj_list if (p.status or "").lower() == "completed"]
    proj_list.sort(key=lambda p: p.id)
    lines = [
        f"  • {format_project_ref_from_obj(p)} — "
        f"{(p.status or 'unknown')}, manager: {p.manager or 'unassigned'}"
        for p in proj_list
    ]
    if status_filter == "open":
        header_en = f"Active projects ({len(proj_list)} total):"
        header_ar = f"المشاريع النشطة ({len(proj_list)} مشروع):"
    elif status_filter == "completed":
        header_en = f"Completed projects ({len(proj_list)} total):"
        header_ar = f"المشاريع المكتملة ({len(proj_list)} مشروع):"
    else:
        header_en = f"Projects ({len(proj_list)} total):"
        header_ar = f"المشاريع ({len(proj_list)} مشروع):"
    body_en = header_en + ("\n" + "\n".join(lines) if lines else "\n  (none)")
    body_ar = header_ar + ("\n" + "\n".join(lines) if lines else "\n  (لا يوجد)")
    plain = ack(body_en, body_ar, lang)
    if proj_list:
        project_rows = []
        for p in proj_list:
            proj_status = (p.status or "unknown").lower()
            value_html = (
                f"{render_id_chip(p.id)} "
                f"{render_status_chip(proj_status.capitalize())} "
                f'<span style="color:{TEXT_MUTED};font-size:12px;">'
                f"manager: {escape(p.manager or 'unassigned')}</span>"
            )
            project_rows.append((p.name or "(unnamed)", value_html))
        html_block = render_action_badge("Project List", "info") + render_detail_card(project_rows)
    else:
        html_block = render_action_badge("Project List", "info") + render_description_panel(
            "No projects match." if lang == "en" else "لا توجد مشاريع مطابقة."
        )
    ctx.reply_block(plain, html_block)
    log_activity(
        ctx.store,
        f"{ctx.channel.name} info request answered (projects)",
        f"{ctx.sender.name} / {ctx.msg.sender_identity}",
        f"status_filter={status_filter}: {len(proj_list)} projects returned.",
    )


def _apply_date_filter(tasks: list[Task], date_filter: str) -> list[Task]:
    """Date-windowed queries drop tasks without a parseable due date."""
    today = now_local().date()
    if date_filter == "overdue":
        return [t for t in tasks if t.due_date_obj and t.due_date_obj.date() < today]
    if date_filter in ("due_today", "tasks_due_today"):
        return [t for t in tasks if t.due_date_obj and t.due_date_obj.date() == today]
    if date_filter == "due_this_week":
        week_end = today + timedelta(days=6)
        return [t for t in tasks if t.due_date_obj and today <= t.due_date_obj.date() <= week_end]
    if date_filter == "due_next_week":
        start, end = today + timedelta(days=7), today + timedelta(days=13)
        return [t for t in tasks if t.due_date_obj and start <= t.due_date_obj.date() <= end]
    if date_filter == "due_this_month":
        return [
            t
            for t in tasks
            if t.due_date_obj
            and t.due_date_obj.year == today.year
            and t.due_date_obj.month == today.month
        ]
    return tasks


def handle_request_info(ctx: HandlerContext) -> None:
    ex = ctx.extracted
    sender = ctx.sender
    scope = (ex.get("scope") or "").strip().lower()
    person_name = (ex.get("person_name") or "").strip()
    project_id = (ex.get("project_id") or "").strip()
    project_ref_unresolved = (ex.get("project_ref_unresolved") or "").strip()
    task_id_filter = (ex.get("task_id") or "").strip().upper()
    raw_status_filter = (ex.get("status_filter") or "").strip().lower()
    # Default "open": an unqualified "show X's tasks" means current workload.
    status_filter = raw_status_filter if raw_status_filter in STATUS_FILTERS else "open"
    raw_date_filter = (ex.get("date_filter") or "").strip().lower()
    date_filter = raw_date_filter if raw_date_filter in DATE_FILTERS else ""
    lang = ctx.lang

    if scope == "projects":
        _projects_scope(ctx, status_filter)
        return

    if scope == "daily_summary":
        if not sender.can_assign:
            ctx.reply(
                "Daily summary is for managers only. Try 'show my tasks' instead.",
                "الملخص اليومي للمدراء فقط. جرب 'اعرض مهامي' بدلًا.",
            )
            log_activity(
                ctx.store,
                "Permission denied",
                ctx.sender_id,
                f"Non-manager requested daily_summary via {ctx.channel.name}.",
            )
            return
        emit_daily_summary(ctx)
        return

    # A named project that isn't active: say so instead of broadening the filter.
    if project_ref_unresolved and not project_id:
        reply_project_not_found(
            ctx, project_ref_unresolved=project_ref_unresolved, action_tag="info request"
        )
        return

    # A specific T-ID overrides scope-based listing.
    targeted_lookup = bool(task_id_filter)

    if scope == "all_tasks" and not sender.is_admin and not targeted_lookup:
        ctx.reply(
            "Only admins can list all tasks. Try asking about your own tasks "
            '("my tasks") or a specific project ("status of P-0001").',
            "فقط المدراء يمكنهم رؤية كل المهام. جرب السؤال عن مهامك الشخصية أو حالة مشروع معين.",
        )
        log_activity(
            ctx.store,
            "Permission denied",
            ctx.sender_id,
            f"Non-admin requested all_tasks via {ctx.channel.name}.",
        )
        return

    if scope == "person_tasks" and not targeted_lookup:
        asking_about_self = bool(person_name) and person_name.lower() == sender.name.lower()
        if not asking_about_self and not sender.can_assign:
            ctx.reply(
                "Only managers can ask about other people's tasks.",
                "فقط المدراء يمكنهم السؤال عن مهام الآخرين.",
            )
            log_activity(
                ctx.store,
                "Permission denied",
                ctx.sender_id,
                f"Non-manager asked about {person_name or '(unknown)'} via {ctx.channel.name}.",
            )
            return

    if scope == "person_tasks" and not person_name and not targeted_lookup:
        ctx.reply(
            "Which person? Please include the name in your message.",
            "أي شخص؟ يرجى ذكر الاسم في رسالتك.",
        )
        return
    if scope == "project_tasks" and not project_id and not targeted_lookup:
        ctx.reply(
            "Which project? Please include the project ID (e.g. P-0001) or its name.",
            "أي مشروع؟ يرجى ذكر رقم المشروع (مثل P-0001) أو اسمه.",
        )
        return

    tasks = load_tasks(ctx.store)
    sender_id = ctx.sender_id
    include_delegated = False
    if targeted_lookup:
        match = find_task_by_id(tasks, task_id_filter)
        if match is None:
            ctx.reply(f"I don't see a task {task_id_filter}.", f"لا أجد المهمة {task_id_filter}.")
            log_activity(
                ctx.store,
                f"{ctx.channel.name} info request — id_not_found",
                sender_id,
                f"task_id={task_id_filter} not in Tasks.",
            )
            return
        if not can_act_on_task(sender, match):
            ctx.reply(
                f"You don't have visibility on {match.task_id}.",
                f"ليس لديك صلاحية لعرض {match.task_id}.",
            )
            log_activity(
                ctx.store,
                "Permission denied",
                sender_id,
                f"Info request for {match.task_id} blocked.",
            )
            return
        tasks = [match]
    elif scope == "my_tasks":
        # Managers rarely hold direct assignments; "my tasks" for them means
        # their own assignments plus the work they delegated.
        if sender.can_assign:
            tasks = [
                t
                for t in tasks
                if is_sender_assignee(t, sender_id) or (t.owner_id and t.owner_id == sender_id)
            ]
            include_delegated = True
        else:
            tasks = [t for t in tasks if is_sender_assignee(t, sender_id)]
    elif scope == "person_tasks":
        tasks = [t for t in tasks if t.assignee_name.lower() == person_name.lower()]
        if project_id:
            tasks = [t for t in tasks if t.project_id == project_id]
    elif scope == "project_tasks":
        tasks = [t for t in tasks if t.project_id == project_id]
    elif scope == "tasks_due_today":
        today = now_local().date()
        tasks = [
            t
            for t in tasks
            if t.assignee_name.lower() == sender.name.lower()
            and t.due_date_obj
            and t.due_date_obj.date() == today
        ]
        if not date_filter:
            date_filter = "due_today"
    elif scope != "all_tasks":
        ctx.reply(
            "I can show your tasks, a project's tasks, someone's tasks, "
            "all active tasks (admins), what's due today, the active projects "
            "list, or the daily summary. Which one?",
            "يمكنني عرض مهامك، مهام مشروع، مهام شخص، كل المهام النشطة "
            "(للمدراء)، المستحق اليوم، قائمة المشاريع، أو الملخص اليومي. أيها تريد؟",
        )
        log_activity(
            ctx.store,
            "Info request — unclear scope",
            sender_id,
            f"scope='{scope}' not recognized; intent confidence={ctx.intent.confidence:.2f}",
        )
        return

    # Workflow container rows are bookkeeping, not actionable work.
    if not targeted_lookup:
        tasks = [
            t for t in tasks if t.sub_step_index > 0 and not t.description.startswith("[Workflow]")
        ]

    if status_filter == "open":
        tasks = [t for t in tasks if t.is_open]
    elif status_filter == "completed":
        tasks = [t for t in tasks if t.status == STATUS_COMPLETED]

    if date_filter and not targeted_lookup:
        tasks = _apply_date_filter(tasks, date_filter)

    # Open first, then by due date (undated last).
    far_future = datetime.max.replace(tzinfo=local_tz())
    tasks_sorted = sorted(tasks, key=lambda t: (not t.is_open, t.due_date_obj or far_future))

    summary = format_task_summary(
        tasks_sorted,
        scope=scope,
        person_name=person_name,
        project_id=project_id,
        projects=ctx.projects,
        lang=lang,
        status_filter=status_filter,
        task_id_filter=task_id_filter or None,
        include_delegated=include_delegated,
        date_filter=date_filter,
    )

    if targeted_lookup and len(tasks_sorted) == 1:
        t = tasks_sorted[0]
        rows = [
            ("Task ID", render_id_chip(t.task_id)),
            ("Project", project_cell(t)),
            ("Assignee", t.assignee_name or "—"),
            ("Deadline", t.due_date or "Not set"),
            ("Status", render_status_chip(humanize_status(t.status, "en"))),
        ]
        if t.notes:
            rows.append(("Latest note", t.notes if len(t.notes) <= 200 else t.notes[:197] + "…"))
        html_block = (
            render_action_badge("Task Status", "info")
            + render_description_panel(t.description or "(no description)")
            + render_detail_card(rows)
        )
    elif tasks_sorted:
        n_total = len(tasks_sorted)
        blocks = [
            render_action_badge("Task List", "info"),
            render_stats_grid(
                [
                    ("Total", n_total, "neutral"),
                    ("Open", sum(1 for t in tasks_sorted if t.status == STATUS_ASSIGNED), "info"),
                    (
                        "Pending",
                        sum(1 for t in tasks_sorted if t.is_open and t.status != STATUS_ASSIGNED),
                        "warning",
                    ),
                    (
                        "Done",
                        sum(1 for t in tasks_sorted if t.status == STATUS_COMPLETED),
                        "confirmed",
                    ),
                ]
            ),
            render_task_table([_table_row(t) for t in tasks_sorted[:MAX_SHOW]]),
        ]
        if n_total > MAX_SHOW:
            blocks.append(
                render_overflow_note(f"… +{n_total - MAX_SHOW} more (showing first {MAX_SHOW})")
            )
        html_block = "".join(blocks)
    else:
        html_block = render_action_badge("Task List", "info") + render_description_panel(
            "No matching tasks." if lang == "en" else "لا توجد مهام مطابقة."
        )

    ctx.reply_block(summary, html_block)
    log_activity(
        ctx.store,
        f"{ctx.channel.name} info request answered",
        f"{sender.name} / {ctx.msg.sender_identity}",
        f"scope={scope} project={project_id or '-'} "
        f"person={person_name or '-'} task_id={task_id_filter or '-'} "
        f"status_filter={status_filter} date_filter={date_filter or '-'}: "
        f"{len(tasks_sorted)} tasks returned.",
    )
