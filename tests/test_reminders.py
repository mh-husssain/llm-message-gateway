"""Project-task reminder engine (/cron): rules A-H and F, their once-only
guards, recipients and the business-hours window."""

from __future__ import annotations

from datetime import datetime

import pytest

from fakes import ADMIN, ALICE, BOB, MANAGER, actions, convs, ctx, seed_task, task
from message_gateway.clock import local_tz
from message_gateway.handlers.assignment import handle_assign_task
from message_gateway.handlers.completion import handle_task_completion
from message_gateway.handlers.confirmation import handle_confirmation_response
from message_gateway.handlers.extensions import (
    handle_delay_acknowledgment,
    handle_request_extension,
)
from message_gateway.handlers.external import (
    handle_external_completion_confirmation,
    handle_external_eta_response,
)
from message_gateway.models import (
    STATUS_ASSIGNED,
    STATUS_AWAITING_ASSIGNEE_COMPLETION,
    STATUS_AWAITING_ASSIGNEE_ETA,
    STATUS_AWAITING_EXTERNAL_COMPLETION,
    STATUS_AWAITING_OWNER_CONFIRMATION,
    STATUS_BLOCKED_BY_PREDECESSOR,
    STATUS_COMPLETED,
    STATUS_ESCALATED,
    STATUS_IN_PROGRESS,
)
from message_gateway.records import load_tasks
from message_gateway.reminders import run_periodic_jobs
from message_gateway.reminders.project_rules import ESCALATED_MARKER, MAX_ESCALATION
from message_gateway.schema import CONTACTS
from message_gateway.workflow import LATE_OWNER_NOTICE_MARKER, OWNER_NO_UPDATE_MARKER


def at(y: int, m: int, d: int, h: int = 11, mi: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=local_tz())


def tick(store, channel, now) -> list[tuple[str, str]]:
    result = run_periodic_jobs(channel, store, now=now)
    return [(d["rule"], d["task_id"]) for d in result.get("details", [])]


def rules(store, channel, now) -> list[str]:
    return [r for r, _ in tick(store, channel, now)]


# ─── A: day before ─────────────────────────────────────────────────────────


def test_rule_a_reminds_the_assignee_the_day_before(store, channel):
    seed_task(store, task_id="T-0001", due_date="20/05/2026", description="pour the slab")
    assert tick(store, channel, at(2026, 5, 19)) == [("A", "T-0001")]

    mail = channel.to(ALICE)
    assert len(mail) == 1
    assert mail[0].subject == "[Gateway] Reminder: T-0001 due tomorrow"
    assert mail[0].body == (
        "Reminder, Alice: 'pour the slab' is due tomorrow (20/05/2026). Reference: T-0001."
    )
    plain, html = mail[0].blocks[0]
    assert plain == mail[0].body and "Reminder · T-1 day" in html and "T-0001" in html
    assert task(store, "T-0001").last_reminder_sent_at == "19/05/2026 11:00:00"
    row = convs(store, "t-1_reminder")[0]
    assert (row.participant_id, row.awaiting_reply) == (ALICE, False)
    assert "Reminder sent" in actions(store)


def test_rule_a_does_not_fire_twice_on_the_same_day(store, channel):
    seed_task(store, task_id="T-0001", due_date="20/05/2026")
    assert rules(store, channel, at(2026, 5, 19, 9)) == ["A"]
    assert rules(store, channel, at(2026, 5, 19, 15)) == []
    assert len(channel.outbox) == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"due_date": "21/05/2026"},  # two days out
        {"due_date": ""},  # undated
        {"due_date": "20/05/2026", "status": STATUS_BLOCKED_BY_PREDECESSOR},
        {"due_date": "20/05/2026", "sub_step_index": 0},  # workflow root
        {"due_date": "20/05/2026", "assignee_id": "ghost@example.com"},  # unknown contact
    ],
)
def test_rule_a_guards(store, channel, fields):
    seed_task(store, task_id="T-0001", **fields)
    assert rules(store, channel, at(2026, 5, 19)) == []


def test_rule_a_also_fires_for_in_progress_tasks(store, channel):
    seed_task(store, task_id="T-0001", due_date="20/05/2026", status=STATUS_IN_PROGRESS)
    assert rules(store, channel, at(2026, 5, 19)) == ["A"]


# ─── B: due today ──────────────────────────────────────────────────────────


def test_rule_b_asks_on_the_due_date_and_a_yes_completes_the_task(store, channel):
    seed_task(store, task_id="T-0001", due_date="20/05/2026", description="pour the slab")
    assert rules(store, channel, at(2026, 5, 20)) == ["B"]
    t = task(store, "T-0001")
    assert t.status == STATUS_AWAITING_ASSIGNEE_COMPLETION
    assert channel.to(ALICE)[0].subject == "[Gateway] T-0001: due today — submitted?"
    check = convs(store, "due_check")
    assert len(check) == 1 and check[0].awaiting_reply

    assert rules(store, channel, at(2026, 5, 20, 16)) == []  # once per day

    c = ctx(store, channel, ALICE, "completion_confirmation_response", confirmed=True)
    handle_confirmation_response(c)
    assert task(store, "T-0001").status == STATUS_COMPLETED
    assert not convs(store, "due_check")[0].awaiting_reply


def test_rule_a_then_b_on_consecutive_days(store, channel):
    seed_task(store, task_id="T-0001", due_date="20/05/2026")
    assert rules(store, channel, at(2026, 5, 19)) == ["A"]
    assert rules(store, channel, at(2026, 5, 20)) == ["B"]


# ─── C and D: waiting on an outside party's timeline ──────────────────────


def eta_task(store, **kw):
    fields = dict(
        task_id="T-0001",
        status=STATUS_AWAITING_ASSIGNEE_ETA,
        external_party="Permit Office",
        awaiting_response_from=ALICE,
        awaiting_response_since="18/05/2026 10:00:00",
        description="submit forms",
    )
    fields.update(kw)
    return seed_task(store, **fields)


def test_rule_c_chases_the_assignee_daily_and_counts(store, channel):
    eta_task(store)
    assert rules(store, channel, at(2026, 5, 19)) == ["C"]
    t = task(store, "T-0001")
    assert t.escalation_count == 1
    mail = channel.to(ALICE)[0]
    assert mail.subject == "[Gateway] T-0001: ETA from Permit Office?"
    assert "any update from Permit Office on 'submit forms'?" in mail.body
    assert convs(store, "eta_request")[0].awaiting_reply
    assert rules(store, channel, at(2026, 5, 19, 17)) == []  # once per day


def test_rule_c_waits_at_least_20_hours_after_the_hand_off(store, channel):
    eta_task(store, awaiting_response_since="19/05/2026 15:00:00")
    assert rules(store, channel, at(2026, 5, 19, 19)) == []  # 4 h
    assert rules(store, channel, at(2026, 5, 20, 10, 59)) == []  # 19 h 59 min
    assert rules(store, channel, at(2026, 5, 20, 11)) == ["C"]  # 20 h


def test_rule_d_tells_the_owner_once_after_two_chasers(store, channel):
    eta_task(store, escalation_count=1)
    assert rules(store, channel, at(2026, 5, 19)) == ["C", "D"]  # C's bump unlocks D
    t = task(store, "T-0001")
    assert t.escalation_count == 2 and OWNER_NO_UPDATE_MARKER in t.notes
    owner_mail = channel.to(MANAGER)
    assert len(owner_mail) == 1
    assert owner_mail[0].subject == "[Gateway] T-0001: no ETA from Permit Office yet"
    assert "hasn't responded for 2 day(s)" in owner_mail[0].body

    assert rules(store, channel, at(2026, 5, 20)) == ["C"]  # D is one-shot
    assert len(channel.to(MANAGER)) == 1


# ─── E: outside party's due date approaching ──────────────────────────────


def test_rule_e_reminds_on_each_of_the_three_days_before(store, channel):
    seed_task(
        store,
        task_id="T-0001",
        status=STATUS_AWAITING_EXTERNAL_COMPLETION,
        due_date="20/05/2026",
        external_party="Permit Office",
    )
    assert rules(store, channel, at(2026, 5, 16)) == []  # four days out
    for day, left in ((17, 3), (18, 2), (19, 1)):
        assert rules(store, channel, at(2026, 5, day)) == ["E"]
        assert channel.outbox[-1].subject == f"[Gateway] Reminder: T-0001 due in {left} day(s)"
        assert rules(store, channel, at(2026, 5, day, 18)) == []  # once per day
    assert rules(store, channel, at(2026, 5, 20)) == []  # due day itself
    assert len(convs(store, "external_completion_check")) == 3


# ─── G, H, F: overdue lifecycle ───────────────────────────────────────────


def late_task(store, **kw):
    fields = dict(
        task_id="T-0100",
        status=STATUS_AWAITING_ASSIGNEE_COMPLETION,
        due_date="19/05/2026",
        description="finalize the deck",
    )
    fields.update(kw)
    return seed_task(store, **fields)


def test_overdue_lifecycle_g_then_h_then_f(store, channel):
    late_task(store)

    assert rules(store, channel, at(2026, 5, 20)) == ["G"]
    t = task(store, "T-0100")
    assert t.escalation_count == 1 and t.status == STATUS_AWAITING_ASSIGNEE_COMPLETION
    assert channel.to(ALICE)[-1].subject == "[Gateway] T-0100: 1 day past due"
    assert rules(store, channel, at(2026, 5, 20, 17)) == []  # same day

    assert rules(store, channel, at(2026, 5, 21)) == ["G", "H"]
    t = task(store, "T-0100")
    assert t.escalation_count == 2 and LATE_OWNER_NOTICE_MARKER in t.notes
    notice = channel.to(MANAGER)
    assert len(notice) == 1
    assert notice[0].subject == "[Gateway] T-0100: 2 days past due, no submission"

    assert rules(store, channel, at(2026, 5, 22)) == ["G"]  # H is one-shot
    assert rules(store, channel, at(2026, 5, 23)) == ["G"]
    assert rules(store, channel, at(2026, 5, 24)) == ["G"]
    assert task(store, "T-0100").escalation_count == MAX_ESCALATION

    assert rules(store, channel, at(2026, 5, 25)) == ["F"]  # cap reads last tick's count
    t = task(store, "T-0100")
    assert t.status == STATUS_ESCALATED and ESCALATED_MARKER in t.notes
    admin_mail = channel.to(ADMIN)
    assert len(admin_mail) == 1 and admin_mail[0].subject == "[Gateway] ESCALATED: T-0100"
    assert "hit escalation cap (5). Manual review required." in admin_mail[0].body

    assert rules(store, channel, at(2026, 6, 1)) == []  # terminal: everything stops


@pytest.mark.parametrize(
    "fields",
    [
        {"due_date": ""},
        {"status": STATUS_COMPLETED},
        {"due_date": "30/05/2026"},  # not late yet
        {"due_date": "20/05/2026"},  # due today is rule B's day, not G's
        {"sub_step_index": 0, "description": "[Workflow] 1-step"},
    ],
)
def test_rule_g_guards(store, channel, fields):
    late_task(store, **fields)
    assert rules(store, channel, at(2026, 5, 20)) == []


def test_rule_f_goes_to_the_first_admin_and_needs_one(store, channel):
    seed_task(store, task_id="T-0001", status=STATUS_IN_PROGRESS, escalation_count=MAX_ESCALATION)
    assert rules(store, channel, at(2026, 5, 20)) == ["F"]
    assert channel.outbox[0].to == ADMIN

    no_admin = [r for r in store.tabs[CONTACTS.name] if r[5] != "admin"]
    store.tabs[CONTACTS.name] = no_admin
    seed_task(store, task_id="T-0002", status=STATUS_IN_PROGRESS, escalation_count=MAX_ESCALATION)
    assert rules(store, channel, at(2026, 5, 21)) == []


def test_a_deliberate_extension_restarts_the_overdue_cycle(store, channel):
    late_task(store)
    rules(store, channel, at(2026, 5, 20))
    rules(store, channel, at(2026, 5, 21))  # G + H
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
    assert t.status == STATUS_IN_PROGRESS and t.escalation_count == 0
    assert LATE_OWNER_NOTICE_MARKER not in t.notes
    assert rules(store, channel, at(2026, 5, 29)) == ["A"]


# ─── Recipients, engine and business hours ────────────────────────────────


def test_joint_task_reminder_goes_to_the_first_known_assignee(store, channel):
    seed_task(
        store, task_id="T-0001", due_date="20/05/2026", assignee_id=f"ghost@example.com,{BOB}"
    )
    rules(store, channel, at(2026, 5, 19))
    assert [o.to for o in channel.outbox] == [BOB]


def test_engine_logs_each_firing_and_the_run(store, channel):
    seed_task(store, task_id="T-0001", due_date="20/05/2026")
    seed_task(store, task_id="T-0002", due_date="19/05/2026")
    seed_task(store, task_id="T-0003", due_date="20/05/2026", status=STATUS_COMPLETED)
    result = run_periodic_jobs(channel, store, now=at(2026, 5, 19))
    assert result == {
        "fired": 2,
        "details": [{"rule": "A", "task_id": "T-0001"}, {"rule": "B", "task_id": "T-0002"}],
    }
    log = store.rows("Activity_Log")
    assert [r[1] for r in log] == ["Reminder sent", "Reminder sent", "Run completed"]
    assert log[0][3] == "Rule A: t-1_reminder; status_after=assigned."
    assert log[-1][3] == "Cron via fake. Rules fired: 2."


@pytest.mark.parametrize(
    ("hour", "minute", "runs"), [(6, 59, False), (7, 0, True), (19, 59, True), (20, 0, False)]
)
def test_business_hours_window(store, channel, hour, minute, runs):
    seed_task(store, task_id="T-0001", due_date="20/05/2026")
    result = run_periodic_jobs(channel, store, now=at(2026, 5, 19, hour, minute))
    if runs:
        assert result["fired"] == 1
    else:
        assert result == {"skipped": "outside_business_hours"}
        assert channel.outbox == [] and store.rows("Activity_Log") == []


def test_business_hours_are_configurable(store, channel, monkeypatch):
    seed_task(store, task_id="T-0001", due_date="20/05/2026")
    monkeypatch.setenv("GATEWAY_BUSINESS_HOURS_START", "9")
    monkeypatch.setenv("GATEWAY_BUSINESS_HOURS_END", "17")
    assert run_periodic_jobs(channel, store, now=at(2026, 5, 19, 8))["skipped"]
    assert run_periodic_jobs(channel, store, now=at(2026, 5, 19, 17))["skipped"]
    assert run_periodic_jobs(channel, store, now=at(2026, 5, 19, 9))["fired"] == 1
    monkeypatch.setenv("GATEWAY_BUSINESS_HOURS_END", "8")
    with pytest.raises(ValueError):
        run_periodic_jobs(channel, store, now=at(2026, 5, 19, 9))


def test_engine_uses_the_clock_when_no_time_is_given(store, channel, frozen):
    seed_task(store, task_id="T-0001", due_date="20/05/2026")
    frozen.set(2026, 5, 19, 3)
    assert run_periodic_jobs(channel, store) == {"skipped": "outside_business_hours"}
    frozen.set(2026, 5, 19, 10)
    assert run_periodic_jobs(channel, store)["fired"] == 1
    assert task(store, "T-0001").last_reminder_sent_at == "19/05/2026 10:00:00"


# ─── Full lifecycle: handlers and engine together ─────────────────────────


def test_assignment_to_external_completion_with_reminders(store, channel, frozen):
    """Assign two steps; reminders before and on the due day; a delay approved
    by the owner; completion; a hand-off with daily chasers and an owner
    notice; an agreed timeline with pre-due reminders; delivery."""
    frozen.set(2026, 5, 4, 10)
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
            due_date_iso="2026-05-06",
            steps=steps,
        )
    )
    root, step1, step2 = sorted(load_tasks(store), key=lambda t: t.sub_step_index)

    def run_on(day):
        frozen.set(2026, 5, day, 10)
        return [(r, tid) for r, tid in tick(store, channel, frozen.now)]

    assert run_on(5) == [("A", step1.task_id)]
    assert run_on(6) == [("B", step1.task_id)]
    assert task(store, step1.task_id).status == STATUS_AWAITING_ASSIGNEE_COMPLETION

    handle_delay_acknowledgment(
        ctx(store, channel, ALICE, "delay_acknowledgment", task_id=step1.task_id, extra_days=1)
    )
    assert task(store, step1.task_id).status == STATUS_AWAITING_OWNER_CONFIRMATION
    handle_confirmation_response(
        ctx(store, channel, MANAGER, "completion_confirmation_response", confirmed=True)
    )
    assert task(store, step1.task_id).due_date == "07/05/2026"

    assert run_on(7) == [("B", step1.task_id)]  # new due day, fresh cycle
    handle_task_completion(ctx(store, channel, ALICE, "task_completion", task_id=step1.task_id))
    assert task(store, step2.task_id).status == STATUS_ASSIGNED
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

    assert run_on(8) == [("C", step2.task_id)]
    assert run_on(9) == [("C", step2.task_id), ("D", step2.task_id)]
    assert OWNER_NO_UPDATE_MARKER in task(store, step2.task_id).notes

    handle_external_eta_response(ctx(store, channel, ALICE, "external_eta_response", eta_days=10))
    handle_confirmation_response(
        ctx(store, channel, MANAGER, "completion_confirmation_response", confirmed=True)
    )
    s2 = task(store, step2.task_id)
    assert s2.status == STATUS_AWAITING_EXTERNAL_COMPLETION and s2.due_date == "19/05/2026"
    assert s2.escalation_count == 0

    assert [run_on(d) for d in (16, 17, 18)] == [[("E", step2.task_id)]] * 3

    handle_external_completion_confirmation(
        ctx(store, channel, ALICE, "external_completion_confirmation", external_artifact="approval")
    )
    assert task(store, step2.task_id).status == STATUS_COMPLETED
    assert task(store, root.task_id).status == STATUS_COMPLETED
    assert run_on(19) == []


def test_yes_after_an_overdue_chaser_is_only_acknowledged(store, channel, frozen):
    """Preserved original behaviour: the chaser opens a `late_completion_check`
    row, which becomes the sender's latest open question; no handler is wired
    for that purpose, so a YES is acknowledged and the task stays open."""
    late_task(store, status=STATUS_ASSIGNED)
    frozen.set(2026, 5, 19, 10)
    assert run_periodic_jobs(channel, store)["details"][0]["rule"] == "B"
    frozen.set(2026, 5, 20, 10)
    assert run_periodic_jobs(channel, store)["details"][0]["rule"] == "G"
    frozen.set(2026, 5, 20, 12)
    c = ctx(store, channel, ALICE, "completion_confirmation_response", confirmed=True)
    handle_confirmation_response(c)
    assert c.replies.plain == "Noted — thanks for confirming."
    assert task(store, "T-0100").status == STATUS_AWAITING_ASSIGNEE_COMPLETION
