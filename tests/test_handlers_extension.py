"""request_extension and delay_acknowledgment."""

from __future__ import annotations

from datetime import timedelta

from fakes import ALICE, BOB, MANAGER, actions, convs, ctx, seed_task, task
from message_gateway.approvals import parse_proposal, propose_to_owner
from message_gateway.clock import now_local
from message_gateway.handlers.extensions import (
    handle_delay_acknowledgment,
    handle_request_extension,
)
from message_gateway.models import (
    STATUS_AWAITING_ASSIGNEE_COMPLETION,
    STATUS_AWAITING_OWNER_CONFIRMATION,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    Task,
)
from message_gateway.records import load_contacts
from message_gateway.workflow import (
    LATE_OWNER_NOTICE_MARKER,
    OWNER_NO_UPDATE_MARKER,
    clear_late_completion_state,
)


def test_manager_extension_applies_directly(store, channel):
    seed_task(store, task_id="T-0026", due_date="20/05/2026", description="finalize the report")
    c = ctx(
        store,
        channel,
        MANAGER,
        "request_extension",
        task_id="T-0026",
        requested_new_date_iso="2026-05-25",
    )
    handle_request_extension(c)
    assert task(store, "T-0026").due_date == "25/05/2026"
    assert convs(store, "owner_confirmation_request") == []
    assert actions(store).count("Extension applied (direct)") == 1
    assert c.replies.plain == "Extended T-0026 to 25/05/2026."


def test_direct_extension_resets_the_late_cycle(store, channel):
    seed_task(
        store,
        task_id="T-0100",
        due_date="19/05/2026",
        escalation_count=2,
        status=STATUS_AWAITING_ASSIGNEE_COMPLETION,
        last_reminder_sent_at="19/05/2026 10:00:00",
        notes=f"some prior note | {LATE_OWNER_NOTICE_MARKER}",
    )
    handle_request_extension(
        ctx(
            store,
            channel,
            MANAGER,
            "request_extension",
            task_id="T-0100",
            requested_new_date_iso="2026-05-30",
        )
    )
    t = task(store, "T-0100")
    assert t.due_date == "30/05/2026"
    assert t.escalation_count == 0 and t.last_reminder_sent_at == ""
    assert t.status == STATUS_IN_PROGRESS
    assert LATE_OWNER_NOTICE_MARKER not in t.notes and "some prior note" in t.notes


def test_task_owner_who_is_not_a_manager_also_applies_directly(store, channel):
    seed_task(store, task_id="T-0001", owner_id=BOB, owner_name="Bob", due_date="01/06/2026")
    handle_request_extension(
        ctx(store, channel, BOB, "request_extension", task_id="T-0001", requested_extra_days=3)
    )
    assert task(store, "T-0001").due_date == "04/06/2026"


def test_assignee_extension_goes_to_the_owner(store, channel):
    seed_task(store, task_id="T-0001", due_date="01/06/2026")
    c = ctx(store, channel, ALICE, "request_extension", requested_extra_days=2, reason="rain")
    handle_request_extension(c)
    t = task(store, "T-0001")
    assert t.status == STATUS_AWAITING_OWNER_CONFIRMATION
    assert t.due_date == "01/06/2026"
    assert parse_proposal(t.notes) == ("delay", {"new_due": "03/06/2026", "reason": "rain"})
    assert len(channel.to(MANAGER)) == 1
    assert "Asking the manager about extending T-0001 to 03/06/2026." in c.replies.plain
    assert "Extension requested" in actions(store)


def test_extension_needs_a_date_or_duration(store, channel):
    seed_task(store, task_id="T-0001")
    c = ctx(store, channel, ALICE, "request_extension")
    handle_request_extension(c)
    assert c.replies.plain == "How many days more, or until what date?"


def test_extension_without_a_due_date_counts_from_today(store, channel):
    seed_task(store, task_id="T-0001", due_date="")
    handle_request_extension(
        ctx(
            store, channel, MANAGER, "request_extension", task_id="T-0001", requested_extra_days="5"
        )
    )
    expected = (now_local() + timedelta(days=5)).strftime("%d/%m/%Y")
    assert task(store, "T-0001").due_date == expected


def test_extension_permission_and_terminal_checks(store, channel):
    seed_task(store, task_id="T-0001", assignee_id=BOB, assignee_name="Bob")
    c = ctx(store, channel, ALICE, "request_extension", task_id="T-0001", requested_extra_days=1)
    handle_request_extension(c)
    assert "You don't have permission to extend T-0001." in c.replies.plain
    assert "Extension permission denied" in actions(store)

    seed_task(store, task_id="T-0002", status=STATUS_COMPLETED)
    c2 = ctx(store, channel, MANAGER, "request_extension", task_id="T-0002", requested_extra_days=1)
    handle_request_extension(c2)
    assert "T-0002 is already completed; no deadline to extend." in c2.replies.plain

    c3 = ctx(store, channel, MANAGER, "request_extension", task_id="T-0404", requested_extra_days=1)
    handle_request_extension(c3)
    assert "I don't see a task T-0404" in c3.replies.plain


def test_no_task_to_extend(store, channel):
    c = ctx(store, channel, ALICE, "request_extension", requested_extra_days=1)
    handle_request_extension(c)
    assert "I don't see a task to extend." in c.replies.plain


def _pending_proposal(store, channel, task_id, assignee=ALICE, name="Alice"):
    seed_task(
        store, task_id=task_id, assignee_id=assignee, assignee_name=name, due_date="01/06/2026"
    )
    t = task(store, task_id)
    propose_to_owner(
        store=store,
        channel=channel,
        task=t,
        contacts=load_contacts(store),
        kind="delay",
        payload={"new_due": "08/06/2026"},
        lang="en",
    )


def test_manager_free_text_approval_applies_the_single_pending_proposal(store, channel):
    _pending_proposal(store, channel, "T-0001")
    c = ctx(store, channel, MANAGER, "request_extension", target_assignee_name_hint="Alice")
    handle_request_extension(c)
    t = task(store, "T-0001")
    assert t.due_date == "08/06/2026"
    assert "Approved — T-0001 extension applied. New due: 08/06/2026." in c.replies.plain
    assert not convs(store, "owner_confirmation_request")[0].awaiting_reply
    assert "Extension approved (NL)" in actions(store)


def test_several_pending_proposals_are_narrowed_by_name_or_listed(store, channel):
    _pending_proposal(store, channel, "T-0001")
    _pending_proposal(store, channel, "T-0002", assignee=BOB, name="Bob")

    c = ctx(store, channel, MANAGER, "request_extension")
    handle_request_extension(c)
    assert "You have 2 pending extension approvals:" in c.replies.plain
    assert "T-0001 (Alice on P-0001)" in c.replies.plain

    c2 = ctx(store, channel, MANAGER, "request_extension", target_assignee_name_hint="bob")
    handle_request_extension(c2)
    assert task(store, "T-0002").due_date == "08/06/2026"
    assert task(store, "T-0001").status == STATUS_AWAITING_OWNER_CONFIRMATION


def test_delay_acknowledgment_proposes_extra_days(store, channel):
    seed_task(store, task_id="T-0001", due_date="01/06/2026", description="floor plan")
    c = ctx(store, channel, ALICE, "delay_acknowledgment", extra_days=2)
    handle_delay_acknowledgment(c)
    t = task(store, "T-0001")
    assert t.status == STATUS_AWAITING_OWNER_CONFIRMATION
    kind, payload = parse_proposal(t.notes)
    assert kind == "delay" and payload["new_due"] == "03/06/2026"
    assert payload["reason"] == "Alice needs 2 more day(s)"
    assert "push T-0001 by 2 day(s) to 03/06/2026" in c.replies.plain
    assert "Delay acknowledged" in actions(store)


def test_delay_defaults_to_one_day_and_clamps(store, channel):
    seed_task(store, task_id="T-0001", due_date="01/06/2026")
    handle_delay_acknowledgment(ctx(store, channel, ALICE, "delay_acknowledgment", extra_days=-4))
    assert parse_proposal(task(store, "T-0001").notes)[1]["new_due"] == "02/06/2026"


def test_delay_permission_and_missing_task(store, channel):
    seed_task(store, task_id="T-0001", assignee_id=BOB, assignee_name="Bob")
    c = ctx(store, channel, ALICE, "delay_acknowledgment", task_id="T-0001")
    handle_delay_acknowledgment(c)
    assert "You don't have permission to delay T-0001." in c.replies.plain

    c2 = ctx(store, channel, ALICE, "delay_acknowledgment")
    handle_delay_acknowledgment(c2)
    assert "I don't see an open task to delay." in c2.replies.plain


def test_clear_late_completion_state_helper():
    t = Task(
        task_id="T-X",
        parent_task_id="",
        project_id="P-0001",
        project_name="Project Alpha",
        owner_id=MANAGER,
        owner_name="Manager One",
        assignee_id=ALICE,
        assignee_name="Alice",
        description="x",
        sub_step_index=1,
        status="assigned",
        due_date="",
        external_party="",
        awaiting_response_from="",
        awaiting_response_since="",
        last_reminder_sent_at="06/05/2026 10:00:00",
        escalation_count=4,
        completed_at="",
        original_request_msg_id="",
        completion_msg_id="",
        notes=f"prior info | {LATE_OWNER_NOTICE_MARKER} | {OWNER_NO_UPDATE_MARKER}",
    )
    out = clear_late_completion_state(t)
    assert out == {"escalation_count": 0, "last_reminder_sent_at": "", "notes": "prior info"}
