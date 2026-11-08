"""create_project: managers register a new project."""

from __future__ import annotations

from ..context import HandlerContext
from ..email_format import (
    render_action_badge,
    render_detail_card,
    render_id_chip,
    render_status_chip,
)
from ..models import Project
from ..records import append_project, log_activity
from ..text import ack, next_project_id


def handle_create_project(ctx: HandlerContext) -> None:
    """Register a project. In a multi-intent message the router reloads
    projects between intents, so a following assign_task resolves the new ID."""
    sender = ctx.sender
    if not sender.can_assign:
        ctx.reply(
            "Only managers can create new projects. Please contact your manager.",
            "فقط المدراء يمكنهم إنشاء مشاريع جديدة. يرجى التواصل مع المدير.",
        )
        log_activity(
            ctx.store,
            "Permission denied",
            ctx.sender_id,
            f"Non-manager attempted create_project via {ctx.channel.name}.",
        )
        return

    ex = ctx.extracted
    proposed_name = (ex.get("proposed_name") or "").strip()
    if not proposed_name:
        ctx.reply(
            "What's the project name? I need a name to register it.",
            "ما اسم المشروع؟ أحتاج الاسم لتسجيله.",
        )
        return

    # Same name (case-insensitive) already registered: surface the existing ID.
    existing = next(
        (p for p in ctx.projects.values() if p.name.strip().lower() == proposed_name.lower()),
        None,
    )
    if existing:
        ctx.reply(
            f"'{existing.name}' already exists as {existing.id}. "
            "Use that project ID for assignments.",
            f"المشروع '{existing.name}' موجود مسبقاً ({existing.id}). استخدم هذا الرقم للتعيين.",
        )
        log_activity(
            ctx.store,
            "Create project — duplicate",
            sender.name,
            f"Skipped duplicate of {existing.id} ({existing.name}).",
        )
        return

    manager_hint = (ex.get("manager_name_hint") or "").strip() or sender.name
    new_id = next_project_id(list(ctx.projects.keys()))
    append_project(
        ctx.store, Project(id=new_id, name=proposed_name, status="active", manager=manager_hint)
    )

    plain = ack(
        f"Registered new project {new_id} ({proposed_name}). "
        f"Manager: {manager_hint}. Assignments can now reference it.",
        f"تم تسجيل مشروع جديد {new_id} ({proposed_name}). "
        f"المدير: {manager_hint}. يمكن إسناد المهام إليه الآن.",
        ctx.lang,
    )
    html_block = render_action_badge("Project Created", "confirmed") + render_detail_card(
        [
            ("Project ID", render_id_chip(new_id)),
            ("Name", proposed_name),
            ("Manager", manager_hint),
            ("Status", render_status_chip("Active")),
        ]
    )
    ctx.reply_block(plain, html_block)
    log_activity(
        ctx.store,
        "Project created",
        f"{new_id} / {proposed_name}",
        f"Manager={manager_hint}; via {ctx.channel.name}.",
    )
