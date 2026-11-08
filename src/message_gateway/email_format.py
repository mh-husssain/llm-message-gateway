"""HTML rendering for email replies.

Replies go out as multipart/alternative: text/plain (handler-authored, the
source of truth and what gets logged) plus text/html rendered here.

Two modes:
  - Block-based: `render_blocks([(plain, html_or_None), ...])` renders one
    card per reply section. Handlers that build a rich card pass it as the
    HTML block; sections without one fall back to the line parser.
  - Line parser: `render_html(plain)` turns plain text into one card,
    detecting headers, bullets, overflow notes and IDs by line shape.

Design constraints:
  - Inline CSS only (many clients strip <style>) and tables for layout,
    with bgcolor= fallbacks for Outlook.
  - No JavaScript, no external CSS, no remote images.
  - Right-to-left layout when the reply contains Arabic.
  - Everything handler-authored is HTML-escaped.
"""

from __future__ import annotations

import html
import re

__all__ = [
    "STATUS_VARIANT",
    "VARIANTS",
    "is_arabic_text",
    "render_action_badge",
    "render_blocks",
    "render_card",
    "render_description_panel",
    "render_detail_card",
    "render_html",
    "render_id_chip",
    "render_stats_grid",
    "render_status_chip",
    "render_task_table",
]

# Neutral palette.
PAGE_BG = "#F3F4F6"
HEADER_BG = "#1F2937"
PANEL_BG = "#F9FAFB"
PANEL_BORDER = "#E5E7EB"
ACCENT = "#2563EB"
CHIP_BG = "#E0E7FF"
CHIP_FG = "#3730A3"
LABEL_GREY = "#6B7280"
TEXT_DARK = "#111827"
TEXT_BODY = "#374151"
TEXT_MUTED = "#6B7280"

FONT_STACK = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, "
    "'Noto Sans Arabic', sans-serif"
)
MONO_STACK = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Courier New',monospace"

# Variant -> (background, foreground) for badges, chips and status pills.
VARIANTS: dict[str, tuple[str, str]] = {
    "confirmed": ("#D1FAE5", "#065F46"),  # added / completed
    "updated": ("#DBEAFE", "#1D4ED8"),  # changes
    "removed": ("#FEE2E2", "#DC2626"),  # deletions
    "info": ("#EDE9FE", "#6D28D9"),  # neutral reply
    "warning": ("#FEF3C7", "#92400E"),  # awaiting / pending
    "escalation": ("#FEE2E2", "#DC2626"),  # escalated
    "neutral": ("#E5E7EB", "#374151"),  # cancelled / unknown
}

# Lowercased status label -> variant key, used when no variant is passed.
STATUS_VARIANT: dict[str, str] = {
    "assigned": "updated",
    "awaiting": "warning",
    "awaiting completion": "warning",
    "awaiting response": "warning",
    "awaiting confirmation": "warning",
    "pending": "warning",
    "in progress": "warning",
    "blocked": "warning",
    "escalated": "escalation",
    "completed": "confirmed",
    "done": "confirmed",
    "cancelled": "neutral",
    "canceled": "neutral",
    "active": "info",
    "open": "info",
    "new": "info",
}

# "Header (N total):" / "Header (N مهمة):" / "Header (N مشروع):".
_HEADER_RE = re.compile(r"\(\s*\d+\s+(?:total|مهمة|مشروع)\s*\)\s*:\s*$")
# Bracketed "[T-0050]" (list rows) or bare "T-0050" (reply sentences).
_ID_RE = re.compile(r"\[([TPCI]-\d{3,5})\]|\b([TPCI]-\d{3,5})\b")
_BULLET_PREFIX = "•"
_ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿ]")


def is_arabic_text(text: str) -> bool:
    return bool(text) and bool(_ARABIC_RE.search(text))


_INLINE_CHIP_OPEN = (
    f'<code style="background:{CHIP_BG};color:{CHIP_FG};'
    f"padding:1px 6px;border-radius:3px;font-family:{MONO_STACK};"
    'font-size:12.5px;">'
)
_INLINE_CHIP_CLOSE = "</code>"


def _replace_id_match(m: re.Match[str]) -> str:
    code = m.group(1) or m.group(2)
    return f"{_INLINE_CHIP_OPEN}{code}{_INLINE_CHIP_CLOSE}"


def inline(text: str) -> str:
    """Escape HTML, then style IDs as chips. The single escape path for content."""
    escaped = html.escape(text or "", quote=False)
    return _ID_RE.sub(_replace_id_match, escaped)


def escape(text: str) -> str:
    return html.escape(text or "", quote=False)


# ────────────────────────────────────────────────────────────────────────────
# Card helpers (handlers compose these)
# ────────────────────────────────────────────────────────────────────────────


def render_action_badge(label: str, variant: str = "info", *, dot: bool = False) -> str:
    """Pill at the top of a card. `dot=True` prefixes a small dot."""
    bg, fg = VARIANTS.get(variant, VARIANTS["info"])
    dot_html = (
        f'<span style="display:inline-block;width:6px;height:6px;'
        f"background-color:{fg};border-radius:50%;margin-right:8px;"
        'vertical-align:middle;">&nbsp;</span>'
        if dot
        else ""
    )
    return (
        '<table cellpadding="0" cellspacing="0" border="0" '
        'style="margin-bottom:14px;"><tr>'
        f'<td bgcolor="{bg}" style="background-color:{bg};'
        'border-radius:20px;padding:4px 14px;">'
        f"{dot_html}"
        f'<span style="font-size:11px;font-weight:700;color:{fg};'
        "text-transform:uppercase;letter-spacing:1px;"
        f'font-family:{FONT_STACK};vertical-align:middle;">'
        f"{html.escape(label, quote=False)}</span>"
        "</td></tr></table>"
    )


def render_id_chip(ident: str) -> str:
    """Monospace pill for IDs; strips brackets from '[T-0050]'."""
    code = (ident or "").strip()
    if code.startswith("[") and code.endswith("]"):
        code = code[1:-1]
    return (
        f'<span style="display:inline-block;background-color:{CHIP_BG};'
        f"color:{CHIP_FG};font-size:12px;font-weight:700;"
        f"padding:2px 9px;border-radius:5px;font-family:{MONO_STACK};"
        f'letter-spacing:0.5px;">{html.escape(code, quote=False)}</span>'
    )


def render_status_chip(label: str, variant: str | None = None) -> str:
    """Small pill for statuses; picks the variant from STATUS_VARIANT if omitted."""
    if variant is None:
        variant = STATUS_VARIANT.get((label or "").strip().lower(), "info")
    bg, fg = VARIANTS.get(variant, VARIANTS["info"])
    return (
        f'<span style="display:inline-block;background-color:{bg};color:{fg};'
        "font-size:10px;font-weight:700;padding:2px 9px;border-radius:20px;"
        "white-space:nowrap;text-transform:uppercase;letter-spacing:0.5px;"
        f'font-family:{FONT_STACK};">'
        f"{html.escape(label or '', quote=False)}</span>"
    )


def render_description_panel(text: str) -> str:
    """Quoted description in a panel with a 3px accent left border."""
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="margin-bottom:16px;"><tr>'
        f'<td bgcolor="{PANEL_BG}" style="background-color:{PANEL_BG};'
        f"border-left:3px solid {ACCENT};border-radius:0 6px 6px 0;"
        'padding:12px 14px;">'
        f'<p style="margin:0;font-size:13px;color:{TEXT_BODY};line-height:1.6;'
        f'font-family:{FONT_STACK};">{inline(text)}</p>'
        "</td></tr></table>"
    )


def render_detail_card(rows: list[tuple[str, str]]) -> str:
    """Key/value rows in a panel. A value containing '<' is treated as
    pre-rendered HTML (e.g. a status chip); anything else is escaped."""
    if not rows:
        return ""
    n = len(rows)
    row_html: list[str] = []
    for i, (label, value) in enumerate(rows):
        border = "" if i == n - 1 else f"border-bottom:1px solid {PANEL_BORDER};"
        value_html = value if "<" in (value or "") else inline(value)
        row_html.append(
            "<tr>"
            f'<td style="padding:7px 0;{border}width:38%;vertical-align:top;">'
            '<span style="font-size:11px;text-transform:uppercase;'
            "letter-spacing:0.8px;"
            f'color:{LABEL_GREY};font-weight:600;font-family:{FONT_STACK};">'
            f"{html.escape(label, quote=False)}</span>"
            "</td>"
            f'<td style="padding:7px 0;{border}vertical-align:top;">'
            f'<span style="font-size:13px;color:{TEXT_DARK};'
            f'font-family:{FONT_STACK};">{value_html}</span>'
            "</td>"
            "</tr>"
        )
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="margin-bottom:18px;"><tr>'
        f'<td bgcolor="{PANEL_BG}" style="background-color:{PANEL_BG};'
        'border-radius:8px;padding:14px 16px;">'
        '<table width="100%" cellpadding="0" cellspacing="0" border="0">'
        + "".join(row_html)
        + "</table>"
        "</td></tr></table>"
    )


def _th(label: str, extra: str = "") -> str:
    return (
        f'<td style="padding:0 12px 8px 0;font-size:10px;text-transform:uppercase;'
        f"letter-spacing:0.8px;color:{LABEL_GREY};font-weight:600;"
        f'font-family:{FONT_STACK};{extra}">{label}</td>'
    )


def render_task_table(rows: list[dict[str, str]]) -> str:
    """Columns: id chip / description / deadline / status pill.

    Row dict: {id, description, deadline, status, status_variant?}.
    """
    if not rows:
        return ""
    head = (
        "<tr>"
        + _th("Task", "width:80px;")
        + _th("Description")
        + _th("Deadline", "white-space:nowrap;width:90px;")
        + _th("Status", "width:90px;")
        + "</tr>"
    )
    body_rows: list[str] = []
    n = len(rows)
    for i, r in enumerate(rows):
        border = "" if i == n - 1 else f"border-bottom:1px solid {PANEL_BORDER};"
        ident = r.get("id", "")
        body_rows.append(
            "<tr>"
            f'<td style="padding:10px 8px 10px 0;{border}vertical-align:top;">'
            f"{render_id_chip(ident) if ident else ''}</td>"
            f'<td style="padding:10px 12px 10px 0;{border}font-size:12px;'
            f"color:{TEXT_BODY};line-height:1.5;vertical-align:top;"
            f'font-family:{FONT_STACK};">{inline(r.get("description", ""))}</td>'
            f'<td style="padding:10px 12px 10px 0;{border}font-size:11px;'
            f"color:{TEXT_MUTED};vertical-align:top;font-family:{FONT_STACK};"
            f'white-space:nowrap;">{escape(r.get("deadline", ""))}</td>'
            f'<td style="padding:10px 0;{border}vertical-align:top;">'
            f"{render_status_chip(r.get('status', ''), r.get('status_variant'))}</td>"
            "</tr>"
        )
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="margin-bottom:14px;">' + head + "".join(body_rows) + "</table>"
    )


def render_stats_grid(metrics: list[tuple[str, object, str]]) -> str:
    """N-across stat cards; each metric is (label, value, variant)."""
    if not metrics:
        return ""
    n = len(metrics)
    cells: list[str] = []
    for i, (label, value, variant) in enumerate(metrics):
        bg, fg = VARIANTS.get(variant, VARIANTS["info"])
        right_pad = 0 if i == n - 1 else 8
        cells.append(
            f'<td width="{100 // n}%" '
            f'style="padding-right:{right_pad}px;vertical-align:top;">'
            '<table width="100%" cellpadding="0" cellspacing="0" border="0">'
            "<tr>"
            f'<td bgcolor="{bg}" style="background-color:{bg};'
            'border-radius:8px;padding:12px;text-align:center;">'
            f'<p style="margin:0;font-size:22px;font-weight:700;color:{fg};'
            f'font-family:{FONT_STACK};">'
            f"{html.escape(str(value), quote=False)}</p>"
            f'<p style="margin:4px 0 0 0;font-size:10px;color:{fg};'
            "text-transform:uppercase;letter-spacing:0.8px;"
            f'font-family:{FONT_STACK};">'
            f"{html.escape(label, quote=False)}</p>"
            "</td></tr></table>"
            "</td>"
        )
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="margin-bottom:18px;"><tr>' + "".join(cells) + "</tr></table>"
    )


def render_section_label(text: str) -> str:
    """Small uppercase caption above a table."""
    return (
        f'<p style="margin:14px 0 6px 0;font-size:11px;'
        f"text-transform:uppercase;letter-spacing:0.8px;color:{LABEL_GREY};"
        f'font-weight:600;font-family:{FONT_STACK};">{escape(text)}</p>'
    )


def render_overflow_note(text: str) -> str:
    return (
        f'<p style="margin:0 0 8px 0;font-size:12px;color:{TEXT_MUTED};'
        f'font-style:italic;">{escape(text)}</p>'
    )


def render_card(content_html: str, *, rtl: bool = False) -> str:
    """Wrap helper output in the white card body."""
    text_align = "right" if rtl else "left"
    direction = "rtl" if rtl else "ltr"
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="margin-bottom:0;"><tr>'
        '<td bgcolor="#FFFFFF" '
        'style="background-color:#FFFFFF;padding:24px 28px 18px 28px;'
        f'text-align:{text_align};direction:{direction};">'
        f"{content_html}"
        "</td></tr></table>"
    )


# ────────────────────────────────────────────────────────────────────────────
# Line parser
# ────────────────────────────────────────────────────────────────────────────


def _classify_line(line: str) -> tuple[str, str]:
    """(kind, content) with kind in {'header', 'bullet', 'note', 'para', 'blank'}."""
    if not line.strip():
        return "blank", ""
    stripped = line.strip()
    if stripped.startswith(_BULLET_PREFIX):
        return "bullet", stripped.lstrip("• \t")
    if _HEADER_RE.search(stripped):
        return "header", stripped
    if stripped.startswith("…"):
        return "note", stripped
    return "para", stripped


def _build_body(plain_text: str) -> str:
    parts: list[str] = []
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            parts.append("</ul>")
            in_list = False

    for raw_line in (plain_text or "").split("\n"):
        kind, content = _classify_line(raw_line)
        if kind == "blank":
            close_list()
            continue
        if kind == "bullet":
            if not in_list:
                parts.append(
                    '<ul style="margin:6px 0 10px 0;margin-left:0;'
                    'padding-left:14px;list-style-type:disc;">'
                )
                in_list = True
            parts.append(
                '<li style="margin:4px 0;line-height:1.55;padding-left:2px;">'
                f"{inline(content)}</li>"
            )
            continue
        close_list()
        if kind == "header":
            parts.append(
                f'<h3 style="margin:14px 0 6px 0;font-size:15px;'
                f'font-weight:600;color:{TEXT_DARK};">{inline(content)}</h3>'
            )
        elif kind == "note":
            parts.append(
                f'<p style="margin:4px 0 8px 0;font-size:12.5px;'
                f'color:{TEXT_MUTED};font-style:italic;">'
                f"{inline(content)}</p>"
            )
        else:
            parts.append(f'<p style="margin:8px 0;line-height:1.5;">{inline(content)}</p>')
    close_list()
    return "\n".join(parts)


# ────────────────────────────────────────────────────────────────────────────
# Document shell
# ────────────────────────────────────────────────────────────────────────────


def _wrap_outer(stacked_cards_html: str, *, rtl: bool) -> str:
    """Neutral shell: a slim header strip, accent rule, the cards and a reply hint."""
    lang = "ar" if rtl else "en"
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{lang}">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width,initial-scale=1">\n'
        "  <title>Automated reply</title>\n"
        "</head>\n"
        f'<body bgcolor="{PAGE_BG}" '
        f'style="margin:0;padding:0;background-color:{PAGE_BG};'
        f'font-family:{FONT_STACK};">\n'
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'bgcolor="{PAGE_BG}" '
        f'style="background-color:{PAGE_BG};padding:32px 0;">\n'
        '<tr><td align="center">\n'
        '<table align="center" width="600" cellpadding="0" cellspacing="0" '
        'border="0" style="width:600px;max-width:600px;">\n'
        # Header strip
        f'  <tr><td bgcolor="{HEADER_BG}" '
        f'style="background-color:{HEADER_BG};padding:12px 28px;'
        'border-radius:12px 12px 0 0;">\n'
        '    <span style="font-size:11px;color:#D1D5DB;'
        f'font-family:{FONT_STACK};letter-spacing:0.3px;">'
        "Automated reply</span>\n"
        "  </td></tr>\n"
        # Accent rule
        f'  <tr><td height="3" bgcolor="{ACCENT}" '
        f'style="height:3px;background-color:{ACCENT};'
        'line-height:3px;font-size:0;">&nbsp;</td></tr>\n'
        # Body
        '  <tr><td bgcolor="#FFFFFF" '
        'style="background-color:#FFFFFF;padding:0;">\n'
        f"    {stacked_cards_html}\n"
        "  </td></tr>\n"
        # Reply hint
        f'  <tr><td bgcolor="{PANEL_BG}" '
        f'style="background-color:{PANEL_BG};padding:12px 28px;'
        f"border-top:1px solid {PANEL_BORDER};font-size:11px;"
        f"color:{TEXT_MUTED};font-family:{FONT_STACK};"
        'border-radius:0 0 12px 12px;">\n'
        "    Reply to this email to respond.\n"
        "  </td></tr>\n"
        "</table>\n"
        "</td></tr>\n"
        "</table>\n"
        "</body>\n"
        "</html>\n"
    )


def render_blocks(blocks: list[tuple[str, str | None]]) -> str:
    """One document, one card per (plain_section, html_block_or_None) entry.

    A provided HTML block is inserted as-is; otherwise the plain section goes
    through the line parser. The shell appears exactly once.
    """
    if not blocks:
        return _wrap_outer("", rtl=False)
    full_plain = "\n\n".join(p or "" for p, _ in blocks)
    rtl = is_arabic_text(full_plain)
    cards = [
        render_card(html_block if html_block is not None else _build_body(plain or ""), rtl=rtl)
        for plain, html_block in blocks
    ]
    return _wrap_outer("\n".join(cards), rtl=rtl)


def render_html(plain_text: str) -> str:
    """A complete HTML document for a plain-text reply (single card)."""
    return render_blocks([(plain_text or "", None)])
