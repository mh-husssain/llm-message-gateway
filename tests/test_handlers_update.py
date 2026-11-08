"""task_update: notes, blockers and owner/manager-only due-date changes."""

from __future__ import annotations

from fakes import ALICE, BOB, MANAGER, actions, ctx, seed_task, task
from message_gateway.handlers.updates import handle_task_update
from message_gateway.models import STATUS_ASSIGNED, STATUS_COMPLETED, STATUS_IN_PROGRESS


def test_manager_changes_due_date_without_copying_body_into_notes(store, channel):
    seed_task(store, task_id="T-0001", due_date="10/05/2026", notes="created")
    c = ctx(
        store,
        channel,
        MANAGER,
        "task_update",
        body="Please push T-0001 to the 31st",
        task_id="T-0001",
        new_due_date_iso="2026-05-31",
    )
    handle_task_update(c)
    t = task(store, "T-0001")
    assert t.due_date == "31/05/2026"
    assert "due_date 10/05/2026 → 31/05/2026 via fake" in t.notes
    assert "Please push" not in t.notes
    assert t.status == STATUS_ASSIGNED
    assert c.replies.plain == "Updated due date on T-0001 to 31/05/2026."
    html = c.replies.entries[0][1]
    assert "Deadline Updated" in html
    # The card shows the previous date as the old deadline.
    old_idx = html.index("Old deadline")
    assert "10/05/2026" in html[old_idx : html.index("New deadline")]


def test_assignee_cannot_change_due_date(store, channel):
    seed_task(store, task_id="T-0001", due_date="10/05/2026")
    c = ctx(store, channel, ALICE, "task_update", task_id="T-0001", new_due_date_iso="2026-06-30")
    handle_task_update(c)
    assert "Only the task owner or a manager can change the due date" in c.replies.plain
    assert task(store, "T-0001").due_date == "10/05/2026"
    assert "Update due-date denied" in actions(store)


def test_progress_note_moves_assigned_to_in_progress(store, channel):
    seed_task(store, task_id="T-0001", description="wire the panel")
    c = ctx(
        store,
        channel,
        ALICE,
        "task_update",
        update_text="half the circuits are done",
        task_description_hint="panel",
    )
    handle_task_update(c)
    t = task(store, "T-0001")
    assert t.status == STATUS_IN_PROGRESS
    assert t.notes.endswith(": half the circuits are done")
    assert c.replies.plain == "Logged update on T-0001."


def test_blocker_is_flagged(store, channel):
    seed_task(store, task_id="T-0001", status=STATUS_IN_PROGRESS)
    c = ctx(
        store,
        channel,
        ALICE,
        "task_update",
        task_id="T-0001",
        is_blocker=True,
        update_text="waiting for materials",
    )
    handle_task_update(c)
    assert "[BLOCKER] waiting for materials" in task(store, "T-0001").notes
    assert "Blocker" in c.replies.entries[0][1]


def test_body_is_used_as_note_when_no_update_text(store, channel):
    seed_task(store, task_id="T-0001")
    handle_task_update(
        ctx(store, channel, ALICE, "task_update", body="still on it", task_id="T-0001")
    )
    assert task(store, "T-0001").notes.endswith(": still on it")


def test_unrelated_employee_cannot_update(store, channel):
    seed_task(store, task_id="T-0001", assignee_id=BOB, assignee_name="Bob")
    c = ctx(store, channel, ALICE, "task_update", task_id="T-0001", update_text="x")
    handle_task_update(c)
    assert "You don't have permission to update T-0001." in c.replies.plain
    assert "Update permission denied" in actions(store)


def test_terminal_task_updates_are_ignored(store, channel):
    seed_task(store, task_id="T-0001", status=STATUS_COMPLETED)
    c = ctx(store, channel, ALICE, "task_update", task_id="T-0001", update_text="x")
    handle_task_update(c)
    assert "T-0001 is already completed; updates are ignored." in c.replies.plain


def test_unparseable_date_is_rejected(store, channel):
    seed_task(store, task_id="T-0001")
    c = ctx(store, channel, MANAGER, "task_update", task_id="T-0001", new_due_date_iso="end of May")
    handle_task_update(c)
    assert "I couldn't parse 'end of May' as a date." in c.replies.plain
    assert task(store, "T-0001").due_date == ""


def test_without_id_ambiguous_and_missing(store, channel):
    c = ctx(store, channel, ALICE, "task_update", update_text="x")
    handle_task_update(c)
    assert "No open tasks for you to update." in c.replies.plain
    seed_task(store, task_id="T-0001", description="a")
    seed_task(store, task_id="T-0002", description="b")
    c2 = ctx(store, channel, ALICE, "task_update", update_text="x")
    handle_task_update(c2)
    assert "More than one task matches" in c2.replies.plain
    c3 = ctx(store, channel, ALICE, "task_update", update_text="x", task_description_hint="zzz")
    handle_task_update(c3)
    assert "I couldn't find that task." in c3.replies.plain
