"""add_contact: admins register a new person in the Contacts tab.

The model extracts fields permissively; every field is re-validated here,
active and inactive rows are checked for a duplicate phone or email, and
free-text cells pass the formula-injection guard before being written.
"""

from __future__ import annotations

import re

from ..context import HandlerContext
from ..email_format import render_action_badge, render_detail_card, render_status_chip
from ..records import append_contact, load_raw_contact_index, log_activity
from ..text import ack, normalize_email, normalize_phone, sanitize_freetext

ROLE_VALUES = {"manager", "employee"}
PERMISSION_VALUES = {"admin", "manager", "employee"}
ACTIVE_VALUES = {"yes", "no"}

PHONE_EXAMPLE = "+15555550123"


def _reject(ctx: HandlerContext, en: str, ar: str, log_detail: str) -> None:
    ctx.reply(en, ar)
    log_activity(ctx.store, "Add contact rejected", ctx.sender.name, log_detail)


def handle_add_contact(ctx: HandlerContext) -> None:
    """Admin-only. An inactive admin never gets here: inactive rows are not
    loaded, so the gate treats them as unknown senders."""
    sender = ctx.sender
    if not sender.is_admin:
        ctx.reply(
            "Only admins can add new contacts. Please contact your admin.",
            "فقط المسؤولون يمكنهم إضافة جهات اتصال جديدة. يرجى التواصل مع المسؤول.",
        )
        log_activity(
            ctx.store,
            "Permission denied",
            ctx.sender_id,
            f"Non-admin (perm={sender.permission}) attempted add_contact via {ctx.channel.name}.",
        )
        return

    ex = ctx.extracted or {}
    raw_name = (ex.get("name") or "").strip()
    raw_phone = (ex.get("phone") or "").strip()
    raw_email = (ex.get("email") or "").strip()
    raw_div = (ex.get("division") or "").strip()
    raw_role = (ex.get("role") or "").strip().lower()
    raw_perm = (ex.get("permission_level") or "").strip().lower() or "employee"
    raw_active = (ex.get("active") or "").strip().lower() or "yes"

    required = {
        "name": raw_name,
        "phone": raw_phone,
        "email": raw_email,
        "division": raw_div,
        "role": raw_role,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        _reject(
            ctx,
            f"Missing required field(s): {', '.join(missing)}. "
            f"I need name, phone (E.164 like {PHONE_EXAMPLE}), email, division, role.",
            f"حقول مفقودة: {', '.join(missing)}. أحتاج إلى الاسم، الهاتف، البريد، القسم، الدور.",
            f"Missing fields: {missing}",
        )
        return

    if "," in raw_email:
        _reject(
            ctx,
            "Please add one email per contact. "
            "Aliases must be added by editing the sheet directly.",
            "يرجى إضافة بريد إلكتروني واحد لكل جهة اتصال. "
            "تُضاف الأسماء البديلة بتعديل الجدول مباشرة.",
            "Multiple emails supplied.",
        )
        return

    # Strip whitespace and dashes first; people write "+1 555-555-0123".
    phone_compact = re.sub(r"[\s\-]", "", raw_phone)
    if not re.fullmatch(r"\+\d{8,15}", phone_compact):
        _reject(
            ctx,
            f"Invalid phone '{raw_phone}'. Use E.164 format like {PHONE_EXAMPLE} "
            "(+ then 8 to 15 digits).",
            f"رقم الهاتف '{raw_phone}' غير صالح. استخدم تنسيق E.164 مثل {PHONE_EXAMPLE}.",
            f"Bad phone: {raw_phone!r}",
        )
        return

    parsed_email = normalize_email(raw_email)
    if not parsed_email or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", parsed_email):
        _reject(
            ctx,
            f"Invalid email '{raw_email}'.",
            f"البريد الإلكتروني '{raw_email}' غير صالح.",
            f"Bad email: {raw_email!r}",
        )
        return

    if raw_role not in ROLE_VALUES:
        _reject(
            ctx,
            f"Invalid role '{raw_role}'. Must be 'manager' or 'employee'.",
            f"الدور '{raw_role}' غير صالح. يجب أن يكون 'manager' أو 'employee'.",
            f"Bad role: {raw_role!r}",
        )
        return

    if raw_perm not in PERMISSION_VALUES:
        _reject(
            ctx,
            f"Invalid permission_level '{raw_perm}'. Must be admin, manager, or employee.",
            f"مستوى الصلاحية '{raw_perm}' غير صالح. يجب أن يكون admin أو manager أو employee.",
            f"Bad permission_level: {raw_perm!r}",
        )
        return

    if raw_active not in ACTIVE_VALUES:
        _reject(
            ctx,
            f"Invalid active value '{raw_active}'. Must be 'yes' or 'no'.",
            f"قيمة 'active' '{raw_active}' غير صالحة. يجب أن تكون 'yes' أو 'no'.",
            f"Bad active: {raw_active!r}",
        )
        return

    norm_phone = normalize_phone(phone_compact)
    raw_index = load_raw_contact_index(ctx.store)
    hit = raw_index.get(parsed_email) or raw_index.get(norm_phone)
    if hit:
        if hit["active"] == "yes":
            ctx.reply(
                "A contact with this email or phone already exists "
                f"(name: {hit['name']}, sheet row {hit['row_index']}, active=yes). "
                "No new row was added.",
                "جهة اتصال بهذا البريد أو الهاتف موجودة بالفعل "
                f"(الاسم: {hit['name']}، الصف {hit['row_index']}). لم تتم الإضافة.",
            )
            action = "Add contact rejected — duplicate active"
        else:
            ctx.reply(
                "An inactive contact with this email or phone exists "
                f"(name: {hit['name']}, sheet row {hit['row_index']}). "
                "To reactivate, edit the sheet directly and set active=yes — "
                "I won't auto-reactivate to avoid silently overwriting their stored details.",
                "توجد جهة اتصال غير نشطة بهذا البريد أو الهاتف "
                f"(الاسم: {hit['name']}، الصف {hit['row_index']}). "
                "لإعادة التفعيل، عدّل الجدول مباشرةً واضبط active=yes.",
            )
            action = "Add contact rejected — duplicate inactive"
        log_activity(
            ctx.store,
            action,
            sender.name,
            f"Match row={hit['row_index']} name={hit['name']!r} "
            f"submitted email={parsed_email} phone={norm_phone}.",
        )
        return

    # Phone and email passed format validation and the enums are constrained,
    # so only the free-text cells need the formula guard.
    safe_name = sanitize_freetext(raw_name)
    safe_div = sanitize_freetext(raw_div)
    append_contact(
        ctx.store, [norm_phone, safe_name, parsed_email, safe_div, raw_role, raw_perm, raw_active]
    )

    plain = ack(
        f"Added contact {raw_name} ({parsed_email}, +{norm_phone}). "
        f"Role: {raw_role}. Permission: {raw_perm}. Division: {raw_div}. "
        f"Active: {raw_active}.",
        f"تمت إضافة جهة الاتصال {raw_name} ({parsed_email}، +{norm_phone}). "
        f"الدور: {raw_role}. الصلاحية: {raw_perm}. القسم: {raw_div}. "
        f"الحالة: {raw_active}.",
        ctx.lang,
    )
    active_chip = render_status_chip(
        "Active" if raw_active == "yes" else "Inactive",
        "confirmed" if raw_active == "yes" else "neutral",
    )
    html_block = render_action_badge("Contact Added", "confirmed") + render_detail_card(
        [
            ("Name", raw_name),
            ("Email", parsed_email),
            ("Phone", f"+{norm_phone}"),
            ("Division", raw_div),
            ("Role", raw_role.capitalize()),
            ("Permission", raw_perm.capitalize()),
            ("Status", active_chip),
        ]
    )
    ctx.reply_block(plain, html_block)
    log_activity(
        ctx.store,
        "Contact added",
        raw_name,
        f"phone=+{norm_phone}; email={parsed_email}; role={raw_role}; "
        f"perm={raw_perm}; division={raw_div}; active={raw_active}; "
        f"by={sender.name} via {ctx.channel.name}.",
    )
