"""Hand-offs to outside parties: ETA proposals and external completion, plus
the full hand-off lifecycle driven through the handlers."""

from __future__ import annotations

from datetime import timedelta

from fakes import ALICE, BOB, MANAGER, actions, convs, ctx, seed_task, task
from message_gateway.approvals import parse_proposal
from message_gateway.clock import now_local
from message_gateway.handlers.assignment import handle_assign_task
from message_gateway.handlers.completion import handle_task_completion
from message_gateway.handlers.confirmation import handle_confirmation_response
from message_gateway.handlers.external import (
    handle_external_completion_confirmation,
    handle_external_eta_response,
)
from message_gateway.models import (
    STATUS_AWAITING_ASSIGNEE_ETA,
    STATUS_AWAITING_EXTERNAL_COMPLETION,
    STATUS_AWAITING_OWNER_CONFIRMATION,
    STATUS_COMPLETED,
)
from message_gateway.records import append_conversation, load_tasks


def in_days(n: int) -> str:
    return (now_local() + timedelta(days=n)).strftime("%d/%m/%Y")


def test_eta_response_is_proposed_to_owner_and_closes_the_eta_request(store, channel):
    seed_task(
        store, task_id="T-0001", status=STATUS_AWAITING_ASSIGNEE_ETA, external_party="Permit Office"
    )
    append_conversation(
        store,
        task_id="T-0001",
        direction="outbound",
        participant_id=ALICE,
        participant_role="assignee",
        channel="fake",
        msg_id="",
        purpose="eta_request",
        body="how long?",
        awaiting_reply=True,
    )
    c = ctx(store, channel, ALICE, "external_eta_response", eta_days=10)
    handle_external_eta_response(c)

    t = task(store, "T-0001")
    assert t.status == STATUS_AWAITING_OWNER_CONFIRMATION
    kind, payload = parse_proposal(t.notes)
    assert kind == "external_eta"
    assert payload == {"new_due": in_days(10), "eta_days": 10, "external_party": "Permit Office"}
    assert not convs(store, "eta_request")[0].awaiting_reply
    assert "Permit Office timeline — needs your OK" in channel.to(MANAGER)[0].subject
    assert "asking the owner about the 10-day timeline from Permit Office" in c.replies.plain
    assert "External ETA proposed" in actions(store)


def test_eta_from_a_date(store, channel):
    seed_task(store, task_id="T-0001", status=STATUS_AWAITING_ASSIGNEE_ETA)
    target = (now_local() + timedelta(days=4)).date().isoformat()
    handle_external_eta_response(
        ctx(store, channel, ALICE, "external_eta_response", eta_date_iso=target)
    )
    assert parse_proposal(task(store, "T-0001").notes)[1]["eta_days"] == 4


def test_eta_without_a_duration_asks(store, channel):
    seed_task(store, task_id="T-0001", status=STATUS_AWAITING_ASSIGNEE_ETA)
    c = ctx(store, channel, ALICE, "external_eta_response", eta_days=0)
    handle_external_eta_response(c)
    assert c.replies.plain == "How many days exactly?"


def test_eta_orphan_and_ambiguous(store, channel):
    c = ctx(store, channel, ALICE, "external_eta_response", eta_days=3)
    handle_external_eta_response(c)
    assert "not currently waiting on an external timeline" in c.replies.plain
    assert "ETA orphan" in actions(store)

    seed_task(store, task_id="T-0001", status=STATUS_AWAITING_ASSIGNEE_ETA, external_party="A Co")
    seed_task(store, task_id="T-0002", status=STATUS_AWAITING_ASSIGNEE_ETA, external_party="B Co")
    c2 = ctx(store, channel, ALICE, "external_eta_response", eta_days=3)
    handle_external_eta_response(c2)
    assert "Which task — T-0001 (A Co), T-0002 (B Co)?" in c2.replies.plain
    c3 = ctx(store, channel, ALICE, "external_eta_response", eta_days=3, task_id_hint="T-0002")
    handle_external_eta_response(c3)
    assert task(store, "T-0002").status == STATUS_AWAITING_OWNER_CONFIRMATION


def test_eta_only_pairs_tasks_assigned_to_the_sender(store, channel):
    seed_task(store, task_id="T-0001", status=STATUS_AWAITING_ASSIGNEE_ETA, assignee_id=BOB)
    c = ctx(store, channel, ALICE, "external_eta_response", eta_days=3, task_id_hint="T-0001")
    handle_external_eta_response(c)
    assert task(store, "T-0001").status == STATUS_AWAITING_ASSIGNEE_ETA


def test_external_completion_closes_step_and_workflow(store, channel):
    seed_task(store, task_id="T-0010", sub_step_index=0, description="[Workflow] 1-step")
    seed_task(
        store,
        task_id="T-0011",
        parent_task_id="T-0010",
        status=STATUS_AWAITING_EXTERNAL_COMPLETION,
        external_party="Permit Office",
        description="get the permit",
    )
    c = ctx(
        store,
        channel,
        ALICE,
        "external_completion_confirmation",
        msg_id="MSG-X",
        external_artifact="permit",
    )
    handle_external_completion_confirmation(c)

    t = task(store, "T-0011")
    assert t.status == STATUS_COMPLETED and t.completion_msg_id == "MSG-X"
    assert "external completion at" in t.notes and t.notes.endswith("permit")
    assert task(store, "T-0010").status == STATUS_COMPLETED
    body = channel.to(MANAGER)[0].body
    assert "confirmed Permit Office delivered permit for T-0011" in body
    assert "Workflow T-0010 fully complete." in body
    assert c.replies.plain == "Excellent — T-0011 marked complete."


def test_external_completion_with_nothing_pending(store, channel):
    c = ctx(store, channel, ALICE, "external_completion_confirmation")
    handle_external_completion_confirmation(c)
    assert "I don't see a task awaiting external completion for you." in c.replies.plain


def test_hand_off_lifecycle_end_to_end(store, channel):
    """Assign two steps, finish step 1, hand step 2 to an outside party, agree
    the timeline with the owner, then confirm delivery."""
    steps = [
        {"description": "prepare drawings"},
        {"description": "obtain approval", "external_party": "Permit Office", "depends_on_step": 1},
    ]
    handle_assign_task(
        ctx(
            store,
            channel,
            MANAGER,
            "assign_task",
            assignee_names=["Alice"],
            project_id="P-0001",
            due_date_iso="2026-05-20",
            steps=steps,
        )
    )
    root, step1, step2 = sorted(load_tasks(store), key=lambda t: t.sub_step_index)

    handle_task_completion(ctx(store, channel, ALICE, "task_completion", task_id=step1.task_id))
    handle_task_completion(
        ctx(
            store,
            channel,
            ALICE,
            "task_completion",
            task_id=step2.task_id,
            external_party_handoff=True,
        )
    )
    assert task(store, step2.task_id).status == STATUS_AWAITING_ASSIGNEE_ETA

    handle_external_eta_response(ctx(store, channel, ALICE, "external_eta_response", eta_days=10))
    assert task(store, step2.task_id).status == STATUS_AWAITING_OWNER_CONFIRMATION

    handle_confirmation_response(
        ctx(store, channel, MANAGER, "completion_confirmation_response", confirmed=True)
    )
    s2 = task(store, step2.task_id)
    assert s2.status == STATUS_AWAITING_EXTERNAL_COMPLETION and s2.due_date == in_days(10)

    handle_external_completion_confirmation(
        ctx(store, channel, ALICE, "external_completion_confirmation", external_artifact="approval")
    )
    assert task(store, step2.task_id).status == STATUS_COMPLETED
    assert task(store, root.task_id).status == STATUS_COMPLETED
