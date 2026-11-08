"""Pure text helpers: normalisation, language detection, IDs and display strings."""

from __future__ import annotations

import os
import re
from email.utils import parseaddr
from typing import TYPE_CHECKING

from .clock import format_display_date

if TYPE_CHECKING:
    from .models import Project, Task

COMPLETION_KEYWORDS_EN = frozenset(
    {"done", "completed", "finished", "submitted", "delivered", "ready"}
)
COMPLETION_KEYWORDS_AR = frozenset({"تم", "خلصت", "انتهيت", "جاهز"})

# A free-text cell starting with one of these is stored with a leading "'" so
# the spreadsheet treats it as text instead of evaluating it as a formula.
FORMULA_PREFIXES = ("=", "+", "-", "@")


def subject_line(text: str) -> str:
    """Prefix an outbound subject with GATEWAY_SUBJECT_PREFIX (default '[Gateway]')."""
    prefix = os.environ.get("GATEWAY_SUBJECT_PREFIX", "[Gateway]").strip()
    return f"{prefix} {text}" if prefix else text


def normalize_email(raw: str) -> str:
    """'Alice <ALICE@example.com>' -> 'alice@example.com'."""
    if not raw:
        return ""
    _, addr = parseaddr(raw)
    return addr.strip().lower()


def normalize_phone(raw: str) -> str:
    """'+1 555 555 0123' -> '15555550123'."""
    return re.sub(r"\D", "", raw or "")


def is_arabic(text: str) -> bool:
    return bool(re.search(r"[؀-ۿ]", text or ""))


def has_completion_keyword(text: str) -> bool:
    t = (text or "").lower()
    if any(k in t for k in COMPLETION_KEYWORDS_EN):
        return True
    return any(k in (text or "") for k in COMPLETION_KEYWORDS_AR)


def sanitize_freetext(s: str) -> str:
    """Formula-injection guard for spreadsheet cells."""
    return ("'" + s) if s and s[0] in FORMULA_PREFIXES else s


def ack(en: str, ar: str, lang: str) -> str:
    return ar if lang == "ar" else en


# Status labels shown to end users. The STATUS_* constants stay snake_case
# (they are the stored values); translation happens at the rendering boundary.
_STATUS_DISPLAY_EN = {
    "assigned": "assigned",
    "in_progress": "in progress",
    "awaiting_assignee_completion": "awaiting completion",
    "blocked": "blocked",
    "delayed": "delayed",
    "awaiting_owner_confirmation": "awaiting owner confirmation",
    "awaiting_assignee_eta": "awaiting ETA",
    "awaiting_external_completion": "awaiting external party",
    "completed": "completed",
    "cancelled": "cancelled",
    "escalated": "escalated",
}
_STATUS_DISPLAY_AR = {
    "assigned": "مسندة",
    "in_progress": "قيد التنفيذ",
    "awaiting_assignee_completion": "بانتظار الإكمال",
    "blocked": "محجوبة",
    "delayed": "متأخرة",
    "awaiting_owner_confirmation": "بانتظار موافقة المالك",
    "awaiting_assignee_eta": "بانتظار مدة زمنية",
    "awaiting_external_completion": "بانتظار طرف خارجي",
    "completed": "مكتملة",
    "cancelled": "ملغاة",
    "escalated": "تم تصعيدها",
}


def humanize_status(status: str, lang: str = "en") -> str:
    """Translate a snake_case status to a reader-friendly phrase.

    Unknown statuses fall back to underscores-as-spaces rather than failing.
    """
    s = (status or "").strip().lower()
    table = _STATUS_DISPLAY_AR if lang == "ar" else _STATUS_DISPLAY_EN
    if s in table:
        return table[s]
    return s.replace("_", " ")


def format_project_ref(project_id: str, projects: dict[str, Project], lang: str = "en") -> str:
    """Render a project as 'Project Name [P-0006]'.

    Falls back to the bare id when the name is missing or equal to the id,
    and to the raw id when the project is unknown.
    """
    pid = (project_id or "").strip()
    if not pid:
        return ""
    proj = projects.get(pid)
    if proj and proj.name and proj.name.strip() and proj.name.strip() != pid:
        return f"{proj.name.strip()} [{pid}]"
    return pid


def format_project_ref_from_obj(project: Project) -> str:
    if project.name and project.name.strip() and project.name.strip() != project.id:
        return f"{project.name.strip()} [{project.id}]"
    return project.id


def format_task_due(t: Task, lang: str = "en") -> str:
    """Render a task's due date for inline reply text ('no due date' when empty)."""
    if not t.due_date:
        return "no due date" if lang == "en" else "بدون موعد"
    return format_display_date(t.due_date)


def _next_id(prefix: str, existing: list[str]) -> str:
    nums = [int(x[2:]) for x in existing if re.fullmatch(rf"{prefix}-\d{{4}}", x)]
    return f"{prefix}-{(max(nums, default=0) + 1):04d}"


def next_project_id(existing: list[str]) -> str:
    return _next_id("P", existing)


def next_task_id(existing: list[str]) -> str:
    return _next_id("T", existing)


def next_independent_task_id(existing: list[str]) -> str:
    return _next_id("I", existing)


def next_conv_id(existing: list[str]) -> str:
    return _next_id("C", existing)
