"""HTML rendering: neutral shell, RTL, escaping, card helpers."""

from __future__ import annotations

from message_gateway.email_format import (
    ACCENT,
    HEADER_BG,
    PAGE_BG,
    STATUS_VARIANT,
    VARIANTS,
    is_arabic_text,
    render_action_badge,
    render_blocks,
    render_description_panel,
    render_detail_card,
    render_html,
    render_id_chip,
    render_stats_grid,
    render_status_chip,
    render_task_table,
)

SUMMARY = (
    "Open tasks for Site Six [P-0006] (3 total):\n"
    "  • review the invoice — awaiting completion, due 07/05/2026 [T-0050]\n"
    "  • send the invoice by 4pm — blocked, due 07/05/2026 [T-0051]\n"
    "  • work on the project — assigned, due no due date [T-0048]"
)


def test_task_summary_renders_header_list_and_id_chips():
    out = render_html(SUMMARY)
    assert out.startswith("<!DOCTYPE html>")
    assert "<h3" in out and "Open tasks for Site Six" in out
    assert out.count("<li") == 3
    assert "[T-0050]" not in out and "T-0050</code>" in out
    assert "[P-0006]" not in out and "P-0006</code>" in out
    assert "<script" not in out.lower()


def test_shell_is_neutral_and_appears_once():
    out = render_blocks([("a", "<p>A</p>"), ("b", None), ("c", "<p>C</p>")])
    assert out.count("Automated reply</span>") == 1
    assert out.count("Reply to this email to respond.") == 1
    assert out.index("<p>A</p>") < out.index("b</p>") < out.index("<p>C</p>")
    assert out.count('bgcolor="#FFFFFF" style="background-color:#FFFFFF;padding:24px') == 3


def test_arabic_reply_flips_direction():
    plain_ar = "المشاريع النشطة (2 مشروع):\n  • Project Alpha [P-0001] — active"
    out = render_html(plain_ar)
    assert is_arabic_text(plain_ar) and not is_arabic_text("Hello world")
    assert "direction:rtl" in out and "text-align:right" in out
    assert 'lang="ar"' in out
    assert "<h3" in out and "المشاريع النشطة" in out
    assert 'lang="en"' in render_html("Hello") and "direction:ltr" in render_html("Hello")


def test_untrusted_content_is_escaped():
    out = render_html("Status (1 total):\n  • <script>alert('x')</script> — assigned [T-0001]")
    assert "<script>" not in out and "&lt;script&gt;" in out
    panel = render_description_panel("Review <img src=x onerror=1> calcs")
    assert "<img" not in panel and "&lt;img" in panel
    card = render_detail_card([("Name", "<b>x</b>"), ("Plain", "a & b")])
    assert "a &amp; b" in card


def test_paragraphs_lists_and_overflow_notes():
    out = render_html(
        "Daily summary:\n  • a\n  • b\n\nTrailing paragraph\n… +13 more (showing first 10)"
    )
    assert out.count("<ul") == 1 and out.count("<li") == 2
    assert "Trailing paragraph" in out
    assert "font-style:italic" in out and "+13 more" in out


def test_empty_and_single_line_bodies():
    assert render_html("").startswith("<!DOCTYPE html>")
    out = render_html("Got it — T-0001 marked complete.")
    assert "<p" in out and "T-0001</code>" in out


def test_badge_variants_and_dot():
    for variant, (bg, fg) in VARIANTS.items():
        out = render_action_badge(f"Test {variant}", variant)
        assert bg in out and fg in out
    assert "border-radius:50%" not in render_action_badge("X")
    assert "border-radius:50%" in render_action_badge("X", "escalation", dot=True)
    assert VARIANTS["info"][0] in render_action_badge("X", "no-such-variant")


def test_id_chip_strips_brackets():
    assert "[" not in render_id_chip("[T-0050]") and "T-0050" in render_id_chip("[T-0050]")


def test_status_chip_picks_variant_from_label():
    assert VARIANTS[STATUS_VARIANT["assigned"]][0] in render_status_chip("Assigned")
    assert VARIANTS["escalation"][0] in render_status_chip("Escalated")
    assert VARIANTS["info"][0] in render_status_chip("Something new")


def test_detail_card_rows_and_last_row_border():
    out = render_detail_card([("Task ID", "T-0001"), ("Status", render_status_chip("Assigned"))])
    assert out.count("<tr>") == 3  # wrapper + 2 rows
    assert "T-0001</code>" in out
    assert "border-bottom" not in out[out.rfind("<tr>") :]
    assert render_detail_card([]) == ""


def test_task_table_and_stats_grid():
    rows = [
        {"id": "T-0001", "description": "a", "deadline": "07/05/2026", "status": "Assigned"},
        {"id": "T-0002", "description": "b", "deadline": "08/05/2026", "status": "Escalated"},
        {"id": "T-0003", "description": "c", "deadline": "09/05/2026", "status": "Completed"},
    ]
    table = render_task_table(rows)
    assert table.count("<tr>") == 4
    assert VARIANTS["escalation"][0] in table and VARIANTS["confirmed"][0] in table
    assert render_task_table([]) == ""
    grid = render_stats_grid(
        [
            ("Total", 12, "neutral"),
            ("Open", 5, "info"),
            ("Pending", 4, "warning"),
            ("Done", 3, "confirmed"),
        ]
    )
    assert grid.count('width="25%"') == 4
    for n in ("12", "5", "4", "3"):
        assert f">{n}</p>" in grid
    assert render_stats_grid([]) == ""


def test_outlook_bgcolor_fallbacks():
    out = render_html("Hello world.")
    for colour in (PAGE_BG, HEADER_BG, ACCENT, "#FFFFFF"):
        assert f'bgcolor="{colour}"' in out
