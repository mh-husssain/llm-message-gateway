"""YES/NO pairing and owner approvals."""

from __future__ import annotations

import json

import pytest

from fakes import ALICE, BOB, MANAGER, actions, convs, ctx, seed_task, task
from message_gateway.approvals import (
    apply_owner_response,
    encode_proposal,
    parse_proposal,
    propose_to_owner,
    strip_proposal,
)
from message_gateway.handlers.confirmation import handle_confirmation_response
from message_gateway.models import (
    STATUS_ASSIGNED,
    STATUS_AWAITING_ASSIGNEE_ETA,
    STATUS_AWAITING_EXTERNAL_COMPLETION,
    STATUS_AWAITING_OWNER_CONFIRMATION,
    STATUS_BLOCKED_BY_PREDECESSOR,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
)
from message_gateway.records import append_conversation, load_contacts, load_projects, load_tasks
from message_gateway.workflow import LATE_OWNER_NOTICE_MARKER


def park(store, *, task_id, participant, purpose, body="question"):
    return append_conversation(
        store,
        task_id=task_id,
        direction="outbound",
        participant_id=participant,
        participant_role="assignee",
        channel="fake",
        msg_id="",
        purpose=purpose,
        body=body,
        awaiting_reply=True,
        truncate=False,
    )


def propose(store, channel, task_id, kind, payload):
    t = task(store, task_id)
    return propose_to_owner(
        store=store,
        channel=channel,
        task=t,
        contacts=load_contacts(store),
        kind=kind,
        payload=payload,
        lang="en",
    )


# ─── Proposal markers ──────────────────────────────────────────────────────


def test_proposal_marker_round_trip():
    notes = "first | " + encode_proposal("delay", {"new_due": "01/06/2026", "reason": "rain {x}"})
    kind, payload = parse_proposal(notes + " | later note")
    assert kind == "delay" and payload == {"new_due": "01/06/2026", "reason": "rain {x}"}
    assert strip_proposal(notes + " | later note") == "first | later note"
    assert parse_proposal("no marker here") is None
    assert parse_proposal("PROPOSAL:delay:{broken") is None


def test_unknown_proposal_kind_raises(store, channel):
    seed_task(store, task_id="T-0001")
    with pytest.raises(ValueError):
        propose(store, channel, "T-0001", "teleport", {})


# ─── Pairing ───────────────────────────────────────────────────────────────


def test_yes_without_a_pending_question(store, channel):
    c = ctx(store, channel, ALICE, "completion_confirmation_response", confirmed=True)
    handle_confirmation_response(c)
    assert "I don't have a pending question for you" in c.replies.plain
    assert "Confirmation orphan" in actions(store)


def test_due_check_yes_completes_and_promotes(store, channel):
    seed_task(store, task_id="T-0010", sub_step_index=0, description="[Workflow] 2-step")
    seed_task(store, task_id="T-0011", parent_task_id="T-0010", description="first")
    seed_task(
        store,
        task_id="T-0012",
        parent_task_id="T-0010",
        sub_step_index=2,
        status=STATUS_BLOCKED_BY_PREDECESSOR,
        description="second",
    )
    park(store, task_id="T-0011", participant=ALICE, purpose="due_check")

    c = ctx(
        store, channel, ALICE, "completion_confirmation_response", msg_id="MSG-Y", confirmed=True
    )
    handle_confirmation_response(c)

    assert task(store, "T-0011").status == STATUS_COMPLETED
    assert task(store, "T-0011").completion_msg_id == "MSG-Y"
    assert task(store, "T-0012").status == STATUS_ASSIGNED
    assert "confirmed T-0011 (first) is complete" in channel.to(MANAGER)[0].body
    assert not convs(store, "due_check")[0].awaiting_reply
    assert c.replies.plain == "Got it — T-0011 marked complete."


def test_due_check_no_keeps_task_in_progress(store, channel):
    seed_task(store, task_id="T-0011")
    park(store, task_id="T-0011", participant=ALICE, purpose="due_check")
    c = ctx(store, channel, ALICE, "completion_confirmation_response", confirmed=False)
    handle_confirmation_response(c)
    assert task(store, "T-0011").status == STATUS_IN_PROGRESS
    assert "stays in progress" in c.replies.plain
    assert not convs(store, "due_check")[0].awaiting_reply


def test_due_check_for_a_deleted_task(store, channel):
    park(store, task_id="T-0999", participant=ALICE, purpose="due_check")
    c = ctx(store, channel, ALICE, "completion_confirmation_response", confirmed=True)
    handle_confirmation_response(c)
    assert "can't be found anymore" in c.replies.plain
    assert not convs(store, "due_check")[0].awaiting_reply


def test_unknown_purpose_is_acknowledged(store, channel):
    park(store, task_id="T-0001", participant=ALICE, purpose="something_else")
    c = ctx(store, channel, ALICE, "completion_confirmation_response", confirmed=True)
    handle_confirmation_response(c)
    assert c.replies.plain == "Noted — thanks for confirming."
    assert "Confirmation noted" in actions(store)


# ─── Owner approvals ───────────────────────────────────────────────────────


def test_propose_to_owner_flips_status_and_messages_owner(store, channel):
    seed_task(store, task_id="T-0001", description="pour the slab", notes="n")
    conv = propose(store, channel, "T-0001", "delay", {"new_due": "05/06/2026", "reason": "rain"})
    t = task(store, "T-0001")
    assert t.status == STATUS_AWAITING_OWNER_CONFIRMATION
    assert parse_proposal(t.notes) == ("delay", {"new_due": "05/06/2026", "reason": "rain"})
    mail = channel.to(MANAGER)[0]
    assert "extension request — needs your OK" in mail.subject
    assert "Reason: rain" in mail.body and "Reply YES to accept, NO to reject." in mail.body
    assert conv.purpose == "owner_confirmation_request" and conv.awaiting_reply


def test_propose_without_owner_contact_logs_failure(store, channel):
    seed_task(store, task_id="T-0001", owner_id="gone@example.com")
    assert propose(store, channel, "T-0001", "delay", {"new_due": "x"}) is None
    assert "Proposal failed" in actions(store)
    assert task(store, "T-0001").status == STATUS_ASSIGNED


def test_owner_yes_applies_delay_and_resets_reminder_cycle(store, channel):
    seed_task(
        store,
        task_id="T-0001",
        due_date="01/06/2026",
        escalation_count=2,
        last_reminder_sent_at="01/06/2026 09:00:00",
        notes=f"x | {LATE_OWNER_NOTICE_MARKER}",
    )
    propose(store, channel, "T-0001", "delay", {"new_due": "05/06/2026", "reason": ""})
    channel.outbox.clear()

    c = ctx(store, channel, MANAGER, "completion_confirmation_response", confirmed=True)
    handle_confirmation_response(c)

    t = task(store, "T-0001")
    assert t.status == STATUS_ASSIGNED and t.due_date == "05/06/2026"
    assert t.escalation_count == 0 and t.last_reminder_sent_at == ""
    assert LATE_OWNER_NOTICE_MARKER not in t.notes and "PROPOSAL:" not in t.notes
    assert "DELAY APPROVED" in t.notes
    assert (
        "The owner approved the change on T-0001. New due: 05/06/2026." in channel.to(ALICE)[0].body
    )
    assert c.replies.plain == "Confirmed — T-0001 updated."
    assert not convs(store, "owner_confirmation_request")[0].awaiting_reply


def test_owner_no_reverts_delay(store, channel):
    seed_task(store, task_id="T-0001", status=STATUS_IN_PROGRESS)
    propose(store, channel, "T-0001", "delay", {"new_due": "05/06/2026"})
    c = ctx(store, channel, MANAGER, "completion_confirmation_response", confirmed=False)
    handle_confirmation_response(c)
    t = task(store, "T-0001")
    assert t.status == STATUS_IN_PROGRESS
    assert "DELAY REJECTED" in t.notes
    assert "declined the change" in channel.to(ALICE)[-1].body
    assert c.replies.plain == "Understood — T-0001 unchanged."


def test_external_eta_yes_and_no(store, channel):
    seed_task(
        store, task_id="T-0001", status=STATUS_AWAITING_ASSIGNEE_ETA, awaiting_response_from=ALICE
    )
    propose(store, channel, "T-0001", "external_eta", {"new_due": "20/06/2026", "eta_days": 10})
    handle_confirmation_response(
        ctx(store, channel, MANAGER, "completion_confirmation_response", confirmed=True)
    )
    t = task(store, "T-0001")
    assert t.status == STATUS_AWAITING_EXTERNAL_COMPLETION and t.due_date == "20/06/2026"
    assert t.awaiting_response_from == ""

    seed_task(store, task_id="T-0002", status=STATUS_AWAITING_ASSIGNEE_ETA)
    propose(store, channel, "T-0002", "external_eta", {"new_due": "20/06/2026", "eta_days": 10})
    handle_confirmation_response(
        ctx(store, channel, MANAGER, "completion_confirmation_response", confirmed=False)
    )
    assert task(store, "T-0002").status == STATUS_AWAITING_ASSIGNEE_ETA


def test_cancel_proposal(store, channel):
    seed_task(store, task_id="T-0001")
    propose(store, channel, "T-0001", "cancel", {"reason": "scope change"})
    assert "Reason: scope change" in channel.to(MANAGER)[0].body
    handle_confirmation_response(
        ctx(store, channel, MANAGER, "completion_confirmation_response", confirmed=True)
    )
    assert task(store, "T-0001").status == STATUS_CANCELLED


def test_apply_without_marker_returns_none(store, channel):
    seed_task(store, task_id="T-0001")
    result = apply_owner_response(
        store=store,
        channel=channel,
        task=task(store, "T-0001"),
        confirmed=True,
        contacts=load_contacts(store),
        lang="en",
    )
    assert result is None
    assert "Proposal apply failed" in actions(store)


def test_owner_decision_when_marker_is_missing_tells_the_sender(store, channel):
    seed_task(store, task_id="T-0001")
    park(store, task_id="T-0001", participant=MANAGER, purpose="owner_confirmation_request")
    c = ctx(store, channel, MANAGER, "completion_confirmation_response", confirmed=True)
    handle_confirmation_response(c)
    assert "Couldn't apply that decision" in c.replies.plain


def test_joint_task_decision_notifies_every_assignee(store, channel):
    seed_task(store, task_id="T-0001", assignee_id=f"{ALICE},{BOB}", assignee_name="Alice, Bob")
    propose(store, channel, "T-0001", "delay", {"new_due": "05/06/2026"})
    handle_confirmation_response(
        ctx(store, channel, MANAGER, "completion_confirmation_response", confirmed=True)
    )
    assert len(channel.to(ALICE)) == 1 and len(channel.to(BOB)) == 1


# ─── Parked project registration ───────────────────────────────────────────


def park_registration(store, payload):
    append_conversation(
        store,
        task_id="(pending)",
        direction="outbound",
        participant_id=MANAGER,
        participant_role="owner",
        channel="fake",
        msg_id="MSG-0",
        purpose="register_project_request",
        body="REGISTER_PROJECT:" + json.dumps(payload),
        awaiting_reply=True,
        truncate=False,
    )


def test_register_yes_creates_project_and_replays_assignment(store, channel):
    park_registration(
        store,
        {
            "kind": "register_project",
            "name": "Old Site",
            "pid": "P-0042",
            "ex": {
                "assignee_names": ["Alice"],
                "project_id": None,
                "project_ref_unresolved": "Old Site",
                "steps": [{"description": "inspect the foundation"}],
            },
        },
    )
    c = ctx(store, channel, MANAGER, "completion_confirmation_response", confirmed=True)
    handle_confirmation_response(c)

    assert load_projects(store)["P-0042"].name == "Old Site"
    created = [t for t in load_tasks(store) if t.project_id == "P-0042"]
    assert {t.description for t in created} == {
        "[Workflow] 1-step assignment to Alice",
        "inspect the foundation",
    }
    assert "Registered P-0042 (Old Site). Creating the task now…" in c.replies.plain
    assert "for Alice on Old Site" in c.replies.plain  # same buffer: one combined reply
    assert "Project registered (from prompt)" in actions(store)


def test_register_no_declines(store, channel):
    park_registration(store, {"name": "Old Site", "pid": "P-0042", "ex": {}})
    c = ctx(store, channel, MANAGER, "completion_confirmation_response", confirmed=False)
    handle_confirmation_response(c)
    assert "OK, didn't register Old Site." in c.replies.plain
    assert "P-0042" not in load_projects(store)


def test_register_with_corrupt_payload(store, channel):
    append_conversation(
        store,
        task_id="(pending)",
        direction="outbound",
        participant_id=MANAGER,
        participant_role="owner",
        channel="fake",
        msg_id="",
        purpose="register_project_request",
        body="REGISTER_PROJECT:{not json",
        awaiting_reply=True,
        truncate=False,
    )
    c = ctx(store, channel, MANAGER, "completion_confirmation_response", confirmed=True)
    handle_confirmation_response(c)
    assert "I lost the parked registration details" in c.replies.plain
