"""task_completion: sequencing, task-scoped permissions, hand-offs and
independent-task completion."""

from __future__ import annotations

from fakes import ADMIN, ALICE, BOB, MANAGER, actions, convs, ctx, seed_task, task
from message_gateway.handlers.completion import handle_task_completion
from message_gateway.models import (
    STATUS_ASSIGNED,
    STATUS_AWAITING_ASSIGNEE_ETA,
    STATUS_BLOCKED_BY_PREDECESSOR,
    STATUS_COMPLETED,
    IndependentTask,
)
from message_gateway.records import append_independent_task, load_independent_tasks


def seed_workflow(store):
    seed_task(
        store,
        task_id="T-0010",
        sub_step_index=0,
        description="[Workflow] 2-step assignment to Alice",
    )
    seed_task(
        store,
        task_id="T-0011",
        parent_task_id="T-0010",
        sub_step_index=1,
        description="draft the plan",
    )
    seed_task(
        store,
        task_id="T-0012",
        parent_task_id="T-0010",
        sub_step_index=2,
        status=STATUS_BLOCKED_BY_PREDECESSOR,
        description="submit the plan",
        external_party="Permit Office",
    )


def test_assignee_completes_own_step_and_next_step_is_promoted(store, channel):
    seed_workflow(store)
    c = ctx(store, channel, ALICE, "task_completion", msg_id="MSG-DONE", subject_hint="draft")
    handle_task_completion(c)

    step1, step2, root = task(store, "T-0011"), task(store, "T-0012"), task(store, "T-0010")
    assert step1.status == STATUS_COMPLETED and step1.completed_at
    assert step1.completion_msg_id == "MSG-DONE"
    assert step2.status == STATUS_ASSIGNED
    assert "promoted from blocked" in step2.notes
    assert root.status == STATUS_ASSIGNED  # not done until every step is

    owner_mail = channel.to(MANAGER)
    assert len(owner_mail) == 1
    assert "Next step now active: T-0012 (submit the plan)." in owner_mail[0].body
    assert "Got it — T-0011 marked complete. Next step: T-0012" in c.replies.plain
    assert "Task completed" in actions(store)


def test_completing_the_last_step_closes_the_workflow_root(store, channel):
    seed_workflow(store)
    handle_task_completion(ctx(store, channel, ALICE, "task_completion", task_id="T-0011"))
    channel.outbox.clear()
    handle_task_completion(ctx(store, channel, ALICE, "task_completion", task_id="T-0012"))
    assert task(store, "T-0010").status == STATUS_COMPLETED
    assert "The parent workflow T-0010 is fully complete." in channel.to(MANAGER)[0].body


def test_manager_can_complete_an_employees_task_by_id(store, channel):
    seed_task(store, task_id="T-0020", assignee_id=BOB, assignee_name="Bob", owner_id=ADMIN)
    handle_task_completion(ctx(store, channel, MANAGER, "task_completion", task_id="T-0020"))
    assert task(store, "T-0020").status == STATUS_COMPLETED


def test_unrelated_employee_is_denied(store, channel):
    seed_task(store, task_id="T-0020", assignee_id=BOB, assignee_name="Bob")
    c = ctx(store, channel, ALICE, "task_completion", task_id="T-0020")
    handle_task_completion(c)
    assert "You don't have permission to complete T-0020." in c.replies.plain
    assert task(store, "T-0020").status == STATUS_ASSIGNED
    assert "Completion permission denied" in actions(store)


def test_joint_assignee_may_complete(store, channel):
    seed_task(store, task_id="T-0021", assignee_id=f"{BOB},{ALICE}", assignee_name="Bob, Alice")
    handle_task_completion(ctx(store, channel, ALICE, "task_completion", task_id="T-0021"))
    assert task(store, "T-0021").status == STATUS_COMPLETED


def test_unknown_id_and_terminal_task(store, channel):
    c = ctx(store, channel, ALICE, "task_completion", task_id="T-0404")
    handle_task_completion(c)
    assert "I don't see a task T-0404" in c.replies.plain

    seed_task(store, task_id="T-0030", status=STATUS_COMPLETED)
    c2 = ctx(store, channel, ALICE, "task_completion", task_id="T-0030")
    handle_task_completion(c2)
    assert "T-0030 is already completed — nothing to do." in c2.replies.plain


def test_root_id_resolves_to_the_single_active_step(store, channel):
    seed_workflow(store)
    handle_task_completion(ctx(store, channel, MANAGER, "task_completion", task_id="T-0010"))
    assert task(store, "T-0011").status == STATUS_COMPLETED
    assert task(store, "T-0010").status == STATUS_ASSIGNED


def test_root_id_with_several_active_steps_asks_which(store, channel):
    seed_task(store, task_id="T-0010", sub_step_index=0, description="[Workflow] 2-step")
    seed_task(store, task_id="T-0011", parent_task_id="T-0010", description="first")
    seed_task(
        store, task_id="T-0012", parent_task_id="T-0010", sub_step_index=2, description="second"
    )
    c = ctx(store, channel, MANAGER, "task_completion", task_id="T-0010")
    handle_task_completion(c)
    assert "T-0010 has 2 active steps." in c.replies.plain
    assert task(store, "T-0011").status == STATUS_ASSIGNED


def test_project_mismatch_is_refused(store, channel):
    seed_task(store, task_id="T-0040", project_id="P-0001")
    c = ctx(store, channel, ALICE, "task_completion", task_id="T-0040", project_id="P-0002")
    handle_task_completion(c)
    assert "T-0040 is on P-0001, not P-0002." in c.replies.plain
    assert task(store, "T-0040").status == STATUS_ASSIGNED


def test_unresolved_project_ref_never_completes_anything(store, channel):
    seed_task(store, task_id="T-0041")
    c = ctx(
        store,
        channel,
        ALICE,
        "task_completion",
        project_ref_unresolved="P-0009",
        subject_hint="all done",
    )
    handle_task_completion(c)
    assert "Project P-0009 does not yet exist." in c.replies.plain
    assert task(store, "T-0041").status == STATUS_ASSIGNED


def test_without_id_no_open_tasks_or_ambiguous(store, channel):
    c = ctx(store, channel, ALICE, "task_completion")
    handle_task_completion(c)
    assert "I don't see any open tasks for you." in c.replies.plain

    seed_task(store, task_id="T-0050", description="paint the fence")
    seed_task(store, task_id="T-0051", description="fix the gate")
    c2 = ctx(store, channel, ALICE, "task_completion")
    handle_task_completion(c2)
    assert "Which task is complete? Open: T-0050 (paint the fence), T-0051 (fix the gate)" in (
        c2.replies.plain
    )
    c3 = ctx(store, channel, ALICE, "task_completion", subject_hint="gate")
    handle_task_completion(c3)
    assert task(store, "T-0051").status == STATUS_COMPLETED


def test_hand_off_to_outside_party_waits_for_an_eta(store, channel):
    seed_task(store, task_id="T-0060", external_party="Permit Office", description="submit forms")
    c = ctx(store, channel, ALICE, "task_completion", task_id="T-0060", external_party_handoff=True)
    handle_task_completion(c)
    t = task(store, "T-0060")
    assert t.status == STATUS_AWAITING_ASSIGNEE_ETA
    assert t.awaiting_response_from == ALICE and t.awaiting_response_since
    assert t.escalation_count == 0
    assert "handed off to Permit Office" in t.notes
    eta = convs(store, "eta_request")
    assert len(eta) == 1 and eta[0].awaiting_reply and eta[0].participant_id == ALICE
    assert "How long does Permit Office typically need" in channel.to(ALICE)[0].body
    assert "handed to Permit Office. How long do they need?" in c.replies.plain


def test_hand_off_flag_without_external_party_completes_normally(store, channel):
    seed_task(store, task_id="T-0061")
    handle_task_completion(
        ctx(store, channel, ALICE, "task_completion", task_id="T-0061", external_party_handoff=True)
    )
    assert task(store, "T-0061").status == STATUS_COMPLETED


# ─── Independent tasks ─────────────────────────────────────────────────────


def seed_independent(store, task_id="I-0005", **kw):
    fields = dict(
        task_id=task_id,
        creator_id=MANAGER,
        creator_name="Manager One",
        assignee_id=BOB,
        assignee_name="Bob",
        description="return the site keys",
        status=STATUS_ASSIGNED,
        deadline="07/05/2026 16:00:00",
        pre_deadline_reminder_minutes="",
        last_reminder_sent_at="",
        escalation_count=0,
        escalated_at="",
        completed_at="",
        original_request_msg_id="SEED",
        completion_msg_id="",
        notes="",
    )
    fields.update(kw)
    append_independent_task(store, IndependentTask(**fields))


def test_independent_completion_routes_by_prefix_and_notifies_creator(store, channel):
    seed_independent(store)
    c = ctx(store, channel, BOB, "task_completion", msg_id="MSG-I", task_id="i-0005")
    handle_task_completion(c)
    t = load_independent_tasks(store)[0]
    assert t.status == STATUS_COMPLETED and t.completed_at
    assert t.completion_msg_id == "MSG-I"
    assert '"return the site keys" marked complete. [I-0005]' in c.replies.plain
    note = channel.to(MANAGER)
    assert len(note) == 1
    assert note[0].subject.endswith("Completed: return the site keys")
    assert "• Task: return the site keys" in note[0].body and "[I-0005]" in note[0].body


def test_creator_closing_their_own_independent_task_is_not_notified(store, channel):
    seed_independent(store)
    handle_task_completion(ctx(store, channel, MANAGER, "task_completion", task_id="I-0005"))
    assert channel.outbox == []


def test_independent_completion_denied_for_unrelated_employee(store, channel):
    seed_independent(store)
    c = ctx(store, channel, ALICE, "task_completion", task_id="I-0005")
    handle_task_completion(c)
    assert "You don't have permission to complete I-0005." in c.replies.plain
    assert load_independent_tasks(store)[0].status == STATUS_ASSIGNED


def test_independent_completion_unknown_and_already_done(store, channel):
    c = ctx(store, channel, BOB, "task_completion", task_id="I-0404")
    handle_task_completion(c)
    assert "I don't see an independent task I-0404." in c.replies.plain
    seed_independent(store, status=STATUS_COMPLETED)
    c2 = ctx(store, channel, BOB, "task_completion", task_id="I-0005")
    handle_task_completion(c2)
    assert "I-0005 is already marked complete." in c2.replies.plain
