"""assign_task: permissions, sequencing, joint tasks, project resolution and
independent tasks."""

from __future__ import annotations

import json

from fakes import ALICE, BOB, MANAGER, actions, convs, ctx, seed_task, task
from message_gateway.handlers.assignment import handle_assign_task
from message_gateway.models import STATUS_ASSIGNED, STATUS_BLOCKED_BY_PREDECESSOR
from message_gateway.records import load_independent_tasks, load_tasks

TWO_STEPS = [
    {"description": "draft the plan", "external_party": None, "depends_on_step": None},
    {
        "description": "submit the plan for approval",
        "external_party": "Permit Office",
        "depends_on_step": 1,
    },
]


def test_employee_cannot_assign(store, channel):
    c = ctx(store, channel, ALICE, "assign_task", assignee_names=["Bob"], project_id="P-0001")
    handle_assign_task(c)
    assert "Only managers can assign tasks" in c.replies.plain
    assert load_tasks(store) == []
    assert "Permission denied" in actions(store)
    assert channel.outbox == []


def test_multi_step_assignment_creates_root_and_sequenced_steps(store, channel):
    c = ctx(
        store,
        channel,
        MANAGER,
        "assign_task",
        msg_id="MSG-A",
        assignee_names=["Alice"],
        project_id="P-0001",
        due_date_iso="2026-05-20",
        steps=TWO_STEPS,
    )
    handle_assign_task(c)

    rows = [t for t in load_tasks(store) if t.original_request_msg_id == "MSG-A"]
    assert len(rows) == 3
    root = next(t for t in rows if t.is_root)
    step1 = next(t for t in rows if t.sub_step_index == 1)
    step2 = next(t for t in rows if t.sub_step_index == 2)
    assert root.sub_step_index == 0 and root.description.startswith("[Workflow] 2-step")
    assert step1.parent_task_id == root.task_id == step2.parent_task_id
    assert step1.status == STATUS_ASSIGNED
    assert step2.status == STATUS_BLOCKED_BY_PREDECESSOR
    assert f"Blocked by {step1.task_id} until completed." in step2.notes
    assert step1.due_date == step2.due_date == "20/05/2026"
    assert step2.external_party == "Permit Office"
    assert step1.owner_id == MANAGER and step1.assignee_id == ALICE

    # One bundled message to the assignee listing both numbered steps.
    sent = channel.to(ALICE)
    assert len(sent) == 1
    assert "1. draft the plan" in sent[0].body
    assert "2. submit the plan for approval (external: Permit Office)" in sent[0].body
    assert root.task_id in sent[0].subject

    assignment_rows = convs(store, "assignment")
    assert [(r.task_id, r.participant_id) for r in assignment_rows] == [(step1.task_id, ALICE)]

    assert f"Created {root.task_id} (steps: {step1.task_id}, {step2.task_id})" in c.replies.plain
    assert "with 2 steps, due 20/05/2026." in c.replies.plain
    assert "Task assigned" in actions(store)


def test_single_step_reply_cites_the_actionable_child_id(store, channel):
    c = ctx(
        store,
        channel,
        MANAGER,
        "assign_task",
        assignee_names=["Alice"],
        project_id="P-0001",
        steps=[{"description": "check the drawings"}],
    )
    handle_assign_task(c)
    child = next(t for t in load_tasks(store) if t.sub_step_index == 1)
    assert c.replies.plain.startswith(f"Created {child.task_id} for Alice on Project Alpha")
    assert "(no due date set)." in c.replies.plain


def test_explicit_dependency_points_at_the_named_step(store, channel):
    steps = [
        {"description": "a"},
        {"description": "b"},
        {"description": "c", "depends_on_step": 1},
    ]
    handle_assign_task(
        ctx(
            store,
            channel,
            MANAGER,
            "assign_task",
            assignee_names=["Alice"],
            project_id="P-0001",
            steps=steps,
        )
    )
    children = sorted(
        (t for t in load_tasks(store) if t.sub_step_index), key=lambda t: t.sub_step_index
    )
    assert f"Blocked by {children[0].task_id}" in children[2].notes


def test_joint_task_stores_both_assignees_and_messages_each(store, channel):
    c = ctx(
        store,
        channel,
        MANAGER,
        "assign_task",
        assignee_names=["Alice", "Bob", "alice"],
        project_id="P-0001",
        steps=[{"description": "joint inspection"}],
    )
    handle_assign_task(c)
    child = next(t for t in load_tasks(store) if t.sub_step_index == 1)
    assert child.assignee_id == f"{ALICE},{BOB}"  # duplicate name deduped
    assert child.assignee_name == "Alice, Bob"
    assert len(channel.to(ALICE)) == 1 and len(channel.to(BOB)) == 1
    assert "(joint task with Alice, Bob)" in channel.to(ALICE)[0].body
    assert len(convs(store, "assignment")) == 2


def test_legacy_single_assignee_string_is_accepted(store, channel):
    handle_assign_task(
        ctx(store, channel, MANAGER, "assign_task", assignee_name="Bob", project_id="P-0001")
    )
    child = next(t for t in load_tasks(store) if t.sub_step_index == 1)
    assert child.assignee_id == BOB
    assert child.description == "task"  # default single step


def test_unknown_and_ambiguous_assignees_create_nothing(store, channel):
    # As in the original, a name matching several contacts is reported in the
    # "couldn't find" list (resolve_assignee returns no contact for it).
    c = ctx(
        store, channel, MANAGER, "assign_task", assignee_names=["Zed", "e"], project_id="P-0001"
    )
    handle_assign_task(c)
    assert c.replies.plain == "I couldn't find a contact named 'Zed', 'e'. Please clarify."
    assert load_tasks(store) == [] and channel.outbox == []


def test_missing_assignee_asks_who(store, channel):
    c = ctx(store, channel, MANAGER, "assign_task", assignee_names=[], project_id="P-0001")
    handle_assign_task(c)
    assert "Who should I assign this to?" in c.replies.plain
    assert "Assignment failed" in actions(store)


def test_unknown_project_id_is_rejected_with_active_project_hint(store, channel):
    c = ctx(store, channel, MANAGER, "assign_task", assignee_names=["Alice"], project_id="P-0099")
    handle_assign_task(c)
    assert "I couldn't resolve the project" in c.replies.plain
    assert "Active projects: P-0001 (Project Alpha), P-0002 (Project Beta)." in c.replies.plain
    assert load_tasks(store) == []


def test_unresolved_ref_matching_a_registered_name_is_recovered(store, channel):
    c = ctx(
        store,
        channel,
        MANAGER,
        "assign_task",
        assignee_names=["Alice"],
        project_id=None,
        project_ref_unresolved="project beta",
    )
    handle_assign_task(c)
    child = next(t for t in load_tasks(store) if t.sub_step_index == 1)
    assert child.project_id == "P-0002"


def test_orphan_project_name_parks_a_register_request(store, channel):
    seed_task(
        store,
        task_id="T-0099",
        project_id="P-0042",
        project_name="Old Site",
        status="completed",
        description="legacy work",
    )
    c = ctx(
        store,
        channel,
        MANAGER,
        "assign_task",
        msg_id="MSG-ORPHAN",
        assignee_names=["Alice"],
        project_id=None,
        project_ref_unresolved="Old Site",
        steps=[{"description": "inspect the foundation"}],
    )
    handle_assign_task(c)

    assert "Reply YES to register P-0042 (Old Site)" in c.replies.plain
    parked = convs(store, "register_project_request")
    assert len(parked) == 1 and parked[0].awaiting_reply
    payload = json.loads(parked[0].body_excerpt.removeprefix("REGISTER_PROJECT:"))
    assert payload["pid"] == "P-0042" and payload["name"] == "Old Site"
    assert payload["ex"]["steps"][0]["description"] == "inspect the foundation"
    assert [t.task_id for t in load_tasks(store)] == ["T-0099"]  # nothing created yet


def test_unknown_project_ref_echoes_the_parsed_task(store, channel):
    c = ctx(
        store,
        channel,
        MANAGER,
        "assign_task",
        assignee_names=["Alice"],
        project_id=None,
        project_ref_unresolved="P-0042",
        steps=[{"description": "pour the slab"}],
    )
    handle_assign_task(c)
    assert "Project P-0042 does not yet exist." in c.replies.plain
    assert 'Task I parsed: "pour the slab"' in c.replies.plain
    assert any("unresolvable project" in a for a in actions(store))


def test_arabic_message_gets_arabic_confirmation(store, channel):
    c = ctx(
        store,
        channel,
        MANAGER,
        "assign_task",
        body="خلي أليس تراجع المخططات",
        assignee_names=["Alice"],
        project_id="P-0001",
        steps=[{"description": "مراجعة المخططات"}],
    )
    handle_assign_task(c)
    assert c.replies.plain.startswith("تم إنشاء")
    assert channel.to(ALICE)[0].body.startswith("مرحبا Alice")


# ─── Independent tasks ─────────────────────────────────────────────────────


def test_independent_task_happy_path(store, channel):
    c = ctx(
        store,
        channel,
        MANAGER,
        "assign_task",
        msg_id="MSG-IND",
        assignee_names=["Bob"],
        project_id=None,
        is_independent=True,
        steps=[{"description": "send the signed form to the courier"}],
        deadline_iso="2026-05-07T16:00:00+00:00",
        pre_deadline_reminder_minutes=30,
    )
    handle_assign_task(c)

    rows = load_independent_tasks(store)
    assert len(rows) == 1
    t = rows[0]
    assert t.task_id == "I-0001"
    assert (t.assignee_name, t.creator_name) == ("Bob", "Manager One")
    assert t.description == "send the signed form to the courier"
    assert t.deadline == "07/05/2026 16:00:00"
    assert t.pre_deadline_reminder_minutes == "30"
    assert t.status == STATUS_ASSIGNED
    assert load_tasks(store) == []

    sent = channel.to(BOB)
    assert len(sent) == 1
    assert "send the signed form" in sent[0].subject  # subject leads with the description
    assert "[I-0001]" in sent[0].body
    assert "• Task:" in sent[0].body and "• Due: 07/05/2026 16:00:00 (UTC)" in sent[0].body
    assert "every 30 minutes" in sent[0].body

    assert "• Assignee: Bob" in c.replies.plain and "[I-0001]" in c.replies.plain
    assignment = [r for r in convs(store, "assignment") if r.task_id == "I-0001"]
    assert len(assignment) == 1 and assignment[0].awaiting_reply
    assert "Independent task assigned" in actions(store)


def test_independent_task_without_deadline_is_rejected(store, channel):
    c = ctx(
        store,
        channel,
        MANAGER,
        "assign_task",
        assignee_names=["Bob"],
        is_independent=True,
        deadline_iso=None,
        steps=[{"description": "call the supplier"}],
    )
    handle_assign_task(c)
    assert "need a deadline" in c.replies.plain
    assert load_independent_tasks(store) == []
    assert "Independent assignment failed" in actions(store)


def test_independent_task_without_assignee_is_rejected(store, channel):
    c = ctx(
        store,
        channel,
        MANAGER,
        "assign_task",
        assignee_names=[],
        is_independent=True,
        deadline_iso="2026-05-07T17:00:00Z",
    )
    handle_assign_task(c)
    assert "Who should I assign this to?" in c.replies.plain
    assert load_independent_tasks(store) == []


def test_independent_task_takes_first_assignee_and_reports_extras(store, channel):
    c = ctx(
        store,
        channel,
        MANAGER,
        "assign_task",
        assignee_names=["Bob", "Alice"],
        is_independent=True,
        deadline_iso="2026-05-07T17:00:00Z",
        description="collect the keys",
    )
    handle_assign_task(c)
    assert load_independent_tasks(store)[0].assignee_id == BOB
    assert "ignored extras: Alice." in c.replies.plain
    assert channel.to(ALICE) == []


def test_independent_task_unknown_assignee(store, channel):
    c = ctx(
        store,
        channel,
        MANAGER,
        "assign_task",
        assignee_names=["Zed"],
        is_independent=True,
        deadline_iso="2026-05-07T17:00:00Z",
    )
    handle_assign_task(c)
    assert "I couldn't find a contact named 'Zed'" in c.replies.plain
    assert load_independent_tasks(store) == []


def test_employee_cannot_create_independent_task(store, channel):
    c = ctx(
        store,
        channel,
        ALICE,
        "assign_task",
        assignee_names=["Bob"],
        is_independent=True,
        deadline_iso="2026-05-07T17:00:00Z",
    )
    handle_assign_task(c)
    assert "Only managers can assign tasks" in c.replies.plain
    assert load_independent_tasks(store) == []


def test_ids_continue_from_existing_rows(store, channel):
    seed_task(store, task_id="T-0007")
    handle_assign_task(
        ctx(store, channel, MANAGER, "assign_task", assignee_names=["Alice"], project_id="P-0001")
    )
    assert {"T-0008", "T-0009"} <= {t.task_id for t in load_tasks(store)}
    assert task(store, "T-0009").parent_task_id == "T-0008"
