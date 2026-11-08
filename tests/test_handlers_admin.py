"""create_project, add_contact (including the formula-injection guard),
end_of_day_report and the unclear path."""

from __future__ import annotations

import pytest

from fakes import ADMIN, ALICE, BOB, CAROL, MANAGER, actions, convs, ctx, seed_task
from message_gateway.handlers import handle_unclear
from message_gateway.handlers.contacts import handle_add_contact
from message_gateway.handlers.projects import handle_create_project
from message_gateway.handlers.reports import handle_end_of_day_report
from message_gateway.records import load_contacts, load_projects
from message_gateway.schema import CONTACTS
from message_gateway.text import sanitize_freetext

# ─── create_project ────────────────────────────────────────────────────────


def test_manager_creates_a_project(store, channel):
    c = ctx(store, channel, MANAGER, "create_project", proposed_name="North Depot")
    handle_create_project(c)
    p = load_projects(store)["P-0004"]
    assert (p.name, p.status, p.manager) == ("North Depot", "active", "Manager One")
    assert "Registered new project P-0004 (North Depot)." in c.replies.plain
    assert "Project created" in actions(store)


def test_manager_hint_is_used(store, channel):
    handle_create_project(
        ctx(
            store,
            channel,
            ADMIN,
            "create_project",
            proposed_name="Yard",
            manager_name_hint="Manager One",
        )
    )
    assert load_projects(store)["P-0004"].manager == "Manager One"


def test_employee_cannot_create_projects(store, channel):
    c = ctx(store, channel, ALICE, "create_project", proposed_name="Mine")
    handle_create_project(c)
    assert c.replies.plain.startswith("Only managers can create new projects.")
    assert "P-0004" not in load_projects(store)
    assert "Permission denied" in actions(store)


def test_duplicate_and_missing_project_names(store, channel):
    c = ctx(store, channel, MANAGER, "create_project", proposed_name="project alpha")
    handle_create_project(c)
    assert "'Project Alpha' already exists as P-0001." in c.replies.plain
    c2 = ctx(store, channel, MANAGER, "create_project", proposed_name="  ")
    handle_create_project(c2)
    assert c2.replies.plain.startswith("What's the project name?")
    assert len(load_projects(store)) == 3


# ─── add_contact ───────────────────────────────────────────────────────────

VALID = dict(
    name="Dana",
    phone="+1 555-555-0123",
    email="Dana <DANA@example.com>",
    division="Field",
    role="employee",
)


def add(store, channel, sender=ADMIN, **overrides):
    fields = {**VALID, **overrides}
    c = ctx(store, channel, sender, "add_contact", **fields)
    handle_add_contact(c)
    return c.replies.plain


def contact_rows(store):
    return store.rows(CONTACTS)


def test_admin_adds_a_contact_with_normalised_fields(store, channel):
    body = add(store, channel)
    row = contact_rows(store)[-1]
    assert row == [
        "15555550123",
        "Dana",
        "dana@example.com",
        "Field",
        "employee",
        "employee",
        "yes",
    ]
    assert "Added contact Dana (dana@example.com, +15555550123)." in body
    assert load_contacts(store).by_email("dana@example.com").name == "Dana"


@pytest.mark.parametrize("sender", [MANAGER, ALICE])
def test_only_admins_can_add_contacts(store, channel, sender):
    body = add(store, channel, sender=sender)
    assert body.startswith("Only admins can add new contacts.")
    assert len(contact_rows(store)) == 5


def test_missing_fields_are_named(store, channel):
    c = ctx(store, channel, ADMIN, "add_contact", name="Dana", role="manager")
    handle_add_contact(c)
    assert "Missing required field(s): phone, email, division." in c.replies.plain


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"email": "a@example.com, b@example.com"}, "one email per contact"),
        ({"phone": "555-0123"}, "Invalid phone"),
        ({"phone": "+12"}, "Invalid phone"),
        ({"email": "not-an-email"}, "Invalid email"),
        ({"role": "boss"}, "Invalid role 'boss'"),
        ({"permission_level": "root"}, "Invalid permission_level 'root'"),
        ({"active": "maybe"}, "Invalid active value 'maybe'"),
    ],
)
def test_field_validation(store, channel, overrides, expected):
    body = add(store, channel, **overrides)
    assert expected in body
    assert len(contact_rows(store)) == 5
    assert "Add contact rejected" in actions(store)


def test_duplicate_active_contact_by_email(store, channel):
    body = add(store, channel, email=ALICE)
    assert "already exists (name: Alice, sheet row 4, active=yes)" in body
    assert "Add contact rejected — duplicate active" in actions(store)


def test_duplicate_inactive_contact_by_phone_is_not_reactivated(store, channel):
    body = add(store, channel, phone="+1 555 555 0199", email="new@example.com")
    assert "An inactive contact with this email or phone exists (name: Carol, sheet row 6)" in body
    assert load_contacts(store).by_email(CAROL) is None
    assert len(contact_rows(store)) == 5


@pytest.mark.parametrize("payload", ['=HYPERLINK("http://x")', "+SUM(A1)", "-1+2", "@cmd"])
def test_formula_injection_guard_on_free_text_cells(store, channel, payload):
    add(store, channel, name=payload, division=payload, email=f"x{len(payload)}@example.com")
    row = contact_rows(store)[-1]
    assert row[1] == "'" + payload
    assert row[3] == "'" + payload


def test_sanitize_freetext_leaves_ordinary_text_alone():
    assert sanitize_freetext("Dana") == "Dana"
    assert sanitize_freetext("") == ""
    assert sanitize_freetext("a=b") == "a=b"


# ─── end_of_day_report ─────────────────────────────────────────────────────


def test_eod_report_logs_hours_against_tasks(store, channel):
    seed_task(store, task_id="T-0030", description="site photos")
    seed_task(store, task_id="T-0031", description="cable survey")
    c = ctx(
        store,
        channel,
        ALICE,
        "end_of_day_report",
        msg_id="MSG-EOD",
        task_mentions=[{"hint": "T-0030", "hours": 4}, {"hint": "cable", "hours": 3.5}],
    )
    handle_end_of_day_report(c)
    rows = convs(store, "eod_log")
    assert {r.task_id for r in rows} == {"T-0030", "T-0031"}
    assert all(r.direction == "inbound" and r.msg_id == "MSG-EOD" for r in rows)
    assert actions(store).count("EOD logged") == 2
    assert c.replies.plain.startswith("Logged your end-of-day report (7.5h total):")
    assert "  - T-0030: 4.0h" in c.replies.plain


def test_eod_unmatched_hints_are_reported_not_dropped(store, channel):
    seed_task(store, task_id="T-0030", description="site photos")
    c = ctx(
        store,
        channel,
        ALICE,
        "end_of_day_report",
        task_mentions=[{"hint": "T-0030", "hours": 2}, {"hint": "nonsense", "hours": 1}],
    )
    handle_end_of_day_report(c)
    assert "Couldn't match: nonsense (1.0h)" in c.replies.plain
    assert len(convs(store, "eod_log")) == 1


def test_eod_only_matches_the_senders_tasks(store, channel):
    seed_task(store, task_id="T-0030", assignee_id=BOB, assignee_name="Bob")
    c = ctx(
        store, channel, ALICE, "end_of_day_report", task_mentions=[{"hint": "T-0030", "hours": 2}]
    )
    handle_end_of_day_report(c)
    assert convs(store, "eod_log") == []
    assert "Couldn't match: T-0030 (2.0h)" in c.replies.plain


def test_eod_without_mentions(store, channel):
    c = ctx(store, channel, ALICE, "end_of_day_report", task_mentions=[])
    handle_end_of_day_report(c)
    assert c.replies.plain.startswith("I didn't pick up any tasks")


# ─── unclear ───────────────────────────────────────────────────────────────


def test_unclear_asks_to_rephrase_and_forwards_to_admin(store, channel):
    c = ctx(store, channel, ALICE, "unclear", body="hmm what about the thing", subject="?")
    handle_unclear(c)
    assert c.replies.plain.startswith("I'm not sure how to handle that.")
    fwd = channel.to(ADMIN)
    assert len(fwd) == 1 and "hmm what about the thing" in fwd[0].body
    assert "Unclear message" in actions(store)
