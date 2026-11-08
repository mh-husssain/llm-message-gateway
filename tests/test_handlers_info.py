"""request_info scopes, permission gates, filters and the manager summary."""

from __future__ import annotations

from datetime import timedelta

from fakes import ADMIN, ALICE, BOB, MANAGER, actions, ctx, seed_task
from message_gateway.clock import now_local
from message_gateway.handlers.info import format_task_summary, handle_request_info
from message_gateway.models import STATUS_COMPLETED, Project, Task
from message_gateway.records import log_activity


def day(offset: int) -> str:
    return (now_local().date() + timedelta(days=offset)).strftime("%d/%m/%Y")


def info(store, channel, sender, **extracted):
    c = ctx(store, channel, sender, "request_info", **extracted)
    handle_request_info(c)
    return c.replies.plain


def test_projects_scope_lists_active_by_default_and_all_on_request(store, channel):
    body = info(store, channel, MANAGER, scope="projects")
    assert body.startswith("Active projects (2 total):")
    assert "Project Alpha [P-0001] — active, manager: Manager One" in body
    assert "P-0003" not in body
    body_all = info(store, channel, MANAGER, scope="projects", status_filter="all")
    assert body_all.startswith("Projects (3 total):") and "P-0003" in body_all
    body_done = info(store, channel, MANAGER, scope="projects", status_filter="completed")
    assert body_done.startswith("Completed projects (1 total):")


def test_date_filters_narrow_the_window(store, channel):
    seed_task(store, task_id="T-0001", description="overdue work", due_date=day(-1))
    seed_task(store, task_id="T-0002", description="due today", due_date=day(0))
    seed_task(store, task_id="T-0003", description="this week", due_date=day(2))
    seed_task(store, task_id="T-0004", description="next week", due_date=day(10))
    seed_task(store, task_id="T-0005", description="undated", due_date="")

    overdue = info(store, channel, ADMIN, scope="all_tasks", date_filter="overdue")
    assert "T-0001" in overdue and "T-0002" not in overdue and "T-0005" not in overdue
    assert overdue.startswith("Tasks overdue (1 total):")
    week = info(store, channel, ADMIN, scope="all_tasks", date_filter="due_this_week")
    assert "T-0002" in week and "T-0003" in week and "T-0001" not in week and "T-0004" not in week
    nxt = info(store, channel, ADMIN, scope="all_tasks", date_filter="due_next_week")
    assert "T-0004" in nxt and "T-0003" not in nxt
    today = info(store, channel, ADMIN, scope="all_tasks", date_filter="due_today")
    assert "T-0002" in today and "T-0001" not in today


def test_all_tasks_is_admin_only(store, channel):
    body = info(store, channel, MANAGER, scope="all_tasks")
    assert body.startswith("Only admins can list all tasks.")
    assert "Permission denied" in actions(store)


def test_person_tasks_requires_manager_unless_asking_about_self(store, channel):
    seed_task(
        store, task_id="T-0001", assignee_id=BOB, assignee_name="Bob", description="bob's work"
    )
    seed_task(store, task_id="T-0002", description="alice's work")
    denied = info(store, channel, ALICE, scope="person_tasks", person_name="Bob")
    assert denied == "Only managers can ask about other people's tasks."
    own = info(store, channel, ALICE, scope="person_tasks", person_name="alice")
    assert "alice's work" in own and "bob's work" not in own
    mgr = info(store, channel, MANAGER, scope="person_tasks", person_name="Bob")
    assert mgr.startswith("Bob's open tasks (1 total):")


def test_required_fields_are_requested(store, channel):
    assert info(store, channel, MANAGER, scope="person_tasks").startswith("Which person?")
    assert info(store, channel, MANAGER, scope="project_tasks").startswith("Which project?")


def test_single_task_lookup_is_permission_gated(store, channel):
    seed_task(store, task_id="T-0009", assignee_id=BOB, assignee_name="Bob", description="secret")
    denied = info(store, channel, ALICE, scope="all_tasks", task_id="t-0009")
    assert denied == "You don't have visibility on T-0009."
    ok = info(store, channel, MANAGER, scope="all_tasks", task_id="T-0009")
    assert ok.startswith("Status of task T-0009 (1 total):")
    missing = info(store, channel, MANAGER, scope="all_tasks", task_id="T-0404")
    assert missing == "I don't see a task T-0404."


def test_my_tasks_for_managers_includes_delegated_work(store, channel):
    seed_task(store, task_id="T-0001", description="delegated by manager")  # owner MANAGER
    seed_task(
        store,
        task_id="T-0002",
        owner_id=ADMIN,
        assignee_id=MANAGER,
        assignee_name="Manager One",
        description="assigned to manager",
    )
    body = info(store, channel, MANAGER, scope="my_tasks")
    assert "delegated by manager" in body and "assigned to manager" in body
    assert body.startswith("Your open tasks (assigned to you + delegated by you)")
    alice = info(store, channel, ALICE, scope="my_tasks")
    assert "delegated by manager" in alice and "assigned to manager" not in alice


def test_workflow_rows_and_completed_tasks_are_filtered(store, channel):
    seed_task(store, task_id="T-0001", sub_step_index=0, description="[Workflow] 1-step")
    seed_task(store, task_id="T-0002", parent_task_id="T-0001", description="real step")
    seed_task(store, task_id="T-0003", description="finished", status=STATUS_COMPLETED)
    body = info(store, channel, ADMIN, scope="all_tasks")
    assert "real step" in body and "[Workflow]" not in body and "finished" not in body
    done = info(store, channel, ADMIN, scope="all_tasks", status_filter="completed")
    assert "finished" in done and "real step" not in done


def test_unknown_scope_and_unresolved_project(store, channel):
    body = info(store, channel, ALICE, scope="weather")
    assert body.startswith("I can show your tasks")
    assert "Info request — unclear scope" in actions(store)
    missing = info(
        store, channel, MANAGER, scope="project_tasks", project_ref_unresolved="warehouse"
    )
    assert missing.startswith("I don't see a project matching 'warehouse'.")


def test_daily_summary_for_managers(store, channel):
    log_activity(store, "Task assigned", "T-0010 / Alice", "x")
    log_activity(store, "Task completed", "T-0011 / Alice", "x")
    log_activity(store, "Task update", "T-0012 / Manager One", "x")
    log_activity(store, "Extension applied (direct)", "T-0010 / Manager One", "x")
    log_activity(store, "Project created", "P-0004 / New Site", "x")
    log_activity(store, "Permission denied", ALICE, "ignored")
    seed_task(store, task_id="T-0020", due_date=day(0), description="due now")

    body = info(store, channel, MANAGER, scope="daily_summary")
    assert body.startswith(f"Daily summary for {day(0)}:")
    assert "Tasks created: 2 (T-0010, P-0004)" in body
    assert "Tasks completed: 1 (T-0011)" in body
    assert "Tasks updated: 1 (T-0012)" in body
    assert "Extensions applied: 1 (T-0010)" in body
    assert "Open tasks due today: 1 (T-0020)" in body


def test_daily_summary_is_manager_only(store, channel):
    body = info(store, channel, ALICE, scope="daily_summary")
    assert body.startswith("Daily summary is for managers only.")


def test_list_reply_carries_a_task_table(store, channel):
    seed_task(store, task_id="T-0001", description="one")
    c = ctx(store, channel, ADMIN, "request_info", scope="all_tasks")
    handle_request_info(c)
    html = c.replies.entries[0][1]
    assert "Task List" in html and "T-0001" in html


# ─── Plain-text summary format ─────────────────────────────────────────────


def _t(tid, status, due, desc, project_id="P-0006"):
    return Task(
        task_id=tid,
        parent_task_id="",
        project_id=project_id,
        project_name="Site Six",
        owner_id=MANAGER,
        owner_name="Manager One",
        assignee_id=ALICE,
        assignee_name="Alice",
        description=desc,
        sub_step_index=1,
        status=status,
        due_date=due,
        external_party="",
        awaiting_response_from="",
        awaiting_response_since="",
        last_reminder_sent_at="",
        escalation_count=0,
        completed_at="",
        original_request_msg_id="",
        completion_msg_id="",
        notes="",
    )


PROJECTS = {"P-0006": Project(id="P-0006", name="Site Six", status="active")}
TASKS = [
    _t("T-0050", "awaiting_assignee_completion", "07/05/2026", "review the invoice"),
    _t("T-0051", "blocked", "07/05/2026", "send the invoice by 4pm"),
    _t("T-0048", "assigned", "", "work on the project"),
]


def test_summary_header_status_words_and_trailing_ids():
    out = format_task_summary(
        TASKS,
        scope="project_tasks",
        person_name="",
        project_id="P-0006",
        projects=PROJECTS,
        lang="en",
    )
    lines = out.split("\n")
    assert lines[0] == "Open tasks for Site Six [P-0006] (3 total):"
    assert lines[1] == "  • review the invoice — awaiting completion, due 07/05/2026 [T-0050]"
    assert "awaiting_assignee_completion" not in out
    assert "due no due date [T-0048]" in out
    assert "on Site Six" not in out  # project already in the header


def test_summary_shows_project_inline_for_broad_scopes_only():
    broad = format_task_summary(
        TASKS, scope="all_tasks", person_name="", project_id="", projects=PROJECTS, lang="en"
    )
    assert "on Site Six [P-0006] [T-0050]" in broad
    one = format_task_summary(
        TASKS[:1],
        scope="all_tasks",
        person_name="",
        project_id="",
        projects=PROJECTS,
        lang="en",
        task_id_filter="T-0050",
    )
    assert one.startswith("Status of task T-0050") and "on Site Six" not in one


def test_summary_overflow_and_empty_and_arabic():
    many = [_t(f"T-{i:04d}", "assigned", "", f"task {i}") for i in range(12)]
    out = format_task_summary(
        many, scope="all_tasks", person_name="", project_id="", projects=PROJECTS, lang="en"
    )
    assert out.endswith("… +2 more (showing first 10)")
    empty = format_task_summary(
        [], scope="my_tasks", person_name="", project_id="", projects=PROJECTS, lang="en"
    )
    assert empty == "Your open tasks: no matching tasks."
    ar = format_task_summary(
        TASKS[:1], scope="my_tasks", person_name="", project_id="", projects=PROJECTS, lang="ar"
    )
    assert "بانتظار الإكمال" in ar and "(1 مهمة):" in ar
