"""Independent-task reminder engine (/cron-fast): rules I, I2 and J."""

from __future__ import annotations

from datetime import datetime

import pytest

from fakes import ALICE, BOB, MANAGER, FakeChannel, actions, convs, ctx
from message_gateway.clock import local_tz
from message_gateway.handlers.assignment import handle_assign_task
from message_gateway.models import (
    STATUS_ASSIGNED,
    STATUS_AWAITING_ASSIGNEE_COMPLETION,
    STATUS_COMPLETED,
    STATUS_ESCALATED,
    IndependentTask,
)
from message_gateway.records import append_independent_task, load_independent_tasks
from message_gateway.reminders import run_independent_periodic_jobs
from message_gateway.reminders.independent_rules import (
    INDEPENDENT_ESCALATED_MARKER,
    INDEPENDENT_MAX_PINGS,
)


def at(h: int, mi: int = 0, d: int = 7) -> datetime:
    return datetime(2026, 5, d, h, mi, tzinfo=local_tz())


def seed(store, **kw) -> None:
    fields = dict(
        task_id="I-0100",
        creator_id=MANAGER,
        creator_name="Manager One",
        assignee_id=BOB,
        assignee_name="Bob",
        description="send the signed form to the courier",
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


def current(store) -> IndependentTask:
    return load_independent_tasks(store)[0]


def rules(store, channel, now) -> list[str]:
    result = run_independent_periodic_jobs(channel, store, now=now)
    return [d["rule"] for d in result.get("details", [])]


def test_rule_i_reminds_before_the_deadline_at_the_requested_interval(store, channel):
    seed(store, pre_deadline_reminder_minutes="30")
    assert rules(store, channel, at(15)) == ["I"]
    t = current(store)
    assert t.last_reminder_sent_at == "07/05/2026 15:00:00"
    assert t.escalation_count == 0 and t.status == STATUS_ASSIGNED
    mail = channel.to(BOB)[0]
    assert mail.subject == "[Gateway] Reminder: I-0100 due 07/05/2026 16:00:00"
    assert "(in ~60 minute(s))" in mail.body
    assert convs(store, "pre_deadline_reminder")[0].awaiting_reply is False

    assert rules(store, channel, at(15)) == []  # same tick
    assert rules(store, channel, at(15, 29)) == []  # interval not elapsed
    assert rules(store, channel, at(15, 30)) == ["I"]


@pytest.mark.parametrize("interval", ["", "0", "abc", "-5"])
def test_rule_i_needs_an_explicit_interval(store, channel, interval):
    seed(store, pre_deadline_reminder_minutes=interval)
    assert rules(store, channel, at(15)) == []


def test_after_the_deadline_two_chasers_then_one_creator_notice(store, channel):
    seed(store, deadline="07/05/2026 14:30:00", pre_deadline_reminder_minutes="30")

    assert rules(store, channel, at(15, 1)) == ["I2"]  # I no longer applies
    t = current(store)
    assert t.escalation_count == 1 and t.status == STATUS_AWAITING_ASSIGNEE_COMPLETION
    assert channel.to(BOB)[-1].subject == "[Gateway] I-0100: 31 min past due"
    assert convs(store, "late_completion_check")[0].awaiting_reply

    assert rules(store, channel, at(15, 20)) == []  # 30-minute spacing

    assert rules(store, channel, at(15, 32)) == ["I2", "J"]  # J sees I2's bump
    t = current(store)
    assert t.escalation_count == INDEPENDENT_MAX_PINGS
    assert t.status == STATUS_ESCALATED
    assert t.escalated_at == "07/05/2026 15:32:00"
    assert INDEPENDENT_ESCALATED_MARKER in t.notes

    notice = channel.to(MANAGER)
    assert len(notice) == 1
    assert (
        notice[0].subject == "[Gateway] Overdue, no response: send the signed form to the courier"
    )
    assert "• Task:" in notice[0].body and "• Assignee: Bob" in notice[0].body
    assert "• Reminders sent: 2" in notice[0].body
    assert len(channel.to(BOB)) == 2  # the assignee never gets the creator notice

    assert rules(store, channel, at(16, 5)) == []  # escalated: reminders stop


def test_rule_j_needs_a_known_creator(store, channel):
    seed(
        store,
        deadline="07/05/2026 14:00:00",
        creator_id="ghost@example.com",
        escalation_count=INDEPENDENT_MAX_PINGS,
        status=STATUS_AWAITING_ASSIGNEE_COMPLETION,
    )
    assert rules(store, channel, at(15)) == []  # I2 capped, J has no recipient
    assert current(store).status == STATUS_AWAITING_ASSIGNEE_COMPLETION


def test_completed_task_gets_no_reminders(store, channel):
    seed(store, deadline="07/05/2026 14:00:00", status=STATUS_COMPLETED)
    assert rules(store, channel, at(15)) == []


def test_unknown_assignee_gets_no_chaser(store, channel):
    seed(store, deadline="07/05/2026 14:00:00", assignee_id="ghost@example.com")
    assert rules(store, channel, at(15)) == []


@pytest.mark.parametrize("hour", [3, 6, 20, 23])
def test_outside_business_hours_nothing_runs(store, channel, hour):
    seed(store, deadline="07/05/2026 01:00:00")
    assert run_independent_periodic_jobs(channel, store, now=at(hour)) == {
        "skipped": "outside_business_hours"
    }
    assert channel.outbox == [] and store.rows("Activity_Log") == []


def test_engine_logs_and_sends_plain_text(store, channel):
    seed(store, deadline="07/05/2026 14:00:00")
    result = run_independent_periodic_jobs(channel, store, now=at(15))
    assert result == {"fired": 1, "details": [{"rule": "I2", "task_id": "I-0100"}]}
    assert actions(store) == ["Reminder sent (independent)", "Run completed (independent)"]
    assert channel.outbox[0].blocks is None  # rendered by the channel's line parser


def test_a_failed_send_does_not_stop_the_tick(store):
    class Flaky(FakeChannel):
        def send_new(self, contact, subject, body, *, blocks=None):
            raise RuntimeError("smtp down")

    seed(store, deadline="07/05/2026 14:00:00")
    assert rules(store, Flaky(), at(15)) == ["I2"]
    assert current(store).escalation_count == 1
    assert len(convs(store, "late_completion_check")) == 1


def test_handler_created_task_is_picked_up_by_the_engine(store, channel, frozen):
    frozen.set(2026, 5, 7, 12)
    handle_assign_task(
        ctx(
            store,
            channel,
            MANAGER,
            "assign_task",
            assignee_names=["Alice"],
            is_independent=True,
            deadline_iso="2026-05-07T13:00:00Z",
            pre_deadline_reminder_minutes=30,
            description="collect the keys",
        )
    )
    channel.outbox.clear()
    frozen.set(2026, 5, 7, 12, 30)
    assert run_independent_periodic_jobs(channel, store)["details"] == [
        {"rule": "I", "task_id": "I-0001"}
    ]
    assert channel.outbox[0].to == ALICE
