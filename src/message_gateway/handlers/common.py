"""Helpers shared by several handlers."""

from __future__ import annotations

from ..context import HandlerContext
from ..email_format import render_id_chip
from ..models import Project, Task
from ..records import log_activity


def active_projects_hint(projects: dict[str, Project]) -> tuple[str, str]:
    """(en_suffix, ar_suffix) listing active projects, or ('', '') if none."""
    active = sorted(
        (p for p in projects.values() if p.status.lower() == "active"), key=lambda p: p.id
    )
    if not active:
        return "", ""
    listed = ", ".join(f"{p.id} ({p.name})" if p.name and p.name != p.id else p.id for p in active)
    return f" Active projects: {listed}.", f" المشاريع النشطة: {listed}."


def reply_project_not_found(
    ctx: HandlerContext,
    *,
    project_ref_unresolved: str,
    action_tag: str,
    parsed_intent_description: str | None = None,
) -> None:
    """Reply that the project doesn't exist (with the active-project hint) and
    log it. When the caller already parsed a task description, echo it back so
    the manager can resend with the right project ID without retyping."""
    if project_ref_unresolved.upper().startswith("P-"):
        en = f"Project {project_ref_unresolved} does not yet exist."
        ar = f"المشروع {project_ref_unresolved} غير موجود بعد."
    else:
        en = f"I don't see a project matching '{project_ref_unresolved}'."
        ar = f"لا أجد مشروعاً يطابق '{project_ref_unresolved}'."
    en_suf, ar_suf = active_projects_hint(ctx.projects)
    desc_en = desc_ar = ""
    if parsed_intent_description:
        snippet = parsed_intent_description.strip()
        if len(snippet) > 200:
            snippet = snippet[:197] + "…"
        desc_en = (
            f' Task I parsed: "{snippet}". Reply with the right project ID and I\'ll create it.'
        )
        desc_ar = f' المهمة المستخرجة: "{snippet}". أرسل رقم المشروع الصحيح وسأنشئها.'
    ctx.reply(en + en_suf + desc_en, ar + ar_suf + desc_ar)
    log_activity(
        ctx.store,
        f"{ctx.channel.name} {action_tag} — unresolvable project",
        ctx.sender_id,
        f"project_ref_unresolved='{project_ref_unresolved}'"
        + (
            f"; parsed_desc='{parsed_intent_description[:80]}'" if parsed_intent_description else ""
        ),
    )


def project_cell(task: Task) -> str:
    """Detail-card value for a task's project: 'Name <P-chip>' or a dash."""
    if task.project_id:
        return f"{task.project_name} {render_id_chip(task.project_id)}"
    return task.project_name or "—"


def short(text: str, limit: int = 60) -> str:
    """Truncate to `limit` characters with an ellipsis."""
    return (text[:limit].rstrip() + "…") if len(text) > limit else text
