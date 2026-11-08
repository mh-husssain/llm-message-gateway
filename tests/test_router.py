"""Dispatch: one combined reply per message, partial-failure notes, reloads
between intents, and the processing markers around classification."""

from __future__ import annotations

import pytest

from fakes import (
    ALICE,
    MANAGER,
    FakeLLM,
    StubClassifier,
    actions,
    classification,
    intent,
    msg,
    one,
    seed_task,
)
from message_gateway import handlers
from message_gateway.classifier import INTENT_NAMES, Classifier
from message_gateway.records import load_contacts, load_projects, load_tasks
from message_gateway.router import dispatch_classification, process_message


def dispatch(store, channel, sender, *intents, body="(message)"):
    contacts = load_contacts(store)
    return dispatch_classification(
        channel=channel,
        store=store,
        msg=msg(sender, body),
        sender=contacts.by_email(sender),
        classification=classification(*intents),
        contacts=contacts,
        projects=load_projects(store),
    )


def test_multi_intent_message_gets_one_combined_reply(store, channel):
    seed_task(store, task_id="T-0005", description="old task")
    ok = dispatch(
        store,
        channel,
        MANAGER,
        intent(
            "assign_task",
            assignee_names=["Alice"],
            project_id="P-0001",
            steps=[{"description": "check the specs"}],
        ),
        intent("request_info", scope="person_tasks", person_name="Alice"),
        intent("task_completion", task_id="T-0005"),
    )
    assert ok is True
    assert len(channel.replies) == 1
    reply = channel.replies[0]
    sections = [plain for plain, _ in reply.blocks]
    assert len(sections) == 3
    assert sections[0].startswith("Created T-0007 for Alice on Project Alpha")
    assert sections[1].startswith("Alice's open tasks")
    assert sections[2].startswith("Got it — T-0005 marked complete.")
    assert reply.body == "\n\n".join(sections)
    assert all(html for _, html in reply.blocks)  # every section has a rich card


def test_create_project_then_assign_in_one_message(store, channel):
    dispatch(
        store,
        channel,
        MANAGER,
        intent("create_project", proposed_name="North Depot"),
        intent(
            "assign_task",
            assignee_names=["Alice"],
            project_id=None,
            project_ref_unresolved="North Depot",
            steps=[{"description": "draft scope"}],
        ),
    )
    child = next(t for t in load_tasks(store) if t.sub_step_index == 1)
    assert child.project_id == "P-0004" and child.project_name == "North Depot"


def test_failure_mid_message_keeps_earlier_items_and_says_what_stopped(store, channel, monkeypatch):
    def explode(ctx):
        raise RuntimeError("store unavailable")

    monkeypatch.setitem(handlers.HANDLERS, "request_info", explode)
    ok = dispatch(
        store,
        channel,
        MANAGER,
        intent("create_project", proposed_name="North Depot"),
        intent("request_info", scope="projects"),
        intent("create_project", proposed_name="South Depot"),
    )
    assert ok is False
    assert set(load_projects(store)) == {"P-0001", "P-0002", "P-0003", "P-0004"}  # third skipped
    body = channel.replies[0].body
    assert "Registered new project P-0004 (North Depot)" in body
    assert "Stopped after an error on item 2 (request_info)." in body
    assert "South Depot" not in body
    assert "Intent dispatch error" in actions(store)


def test_arabic_failure_note(store, channel, monkeypatch):
    monkeypatch.setitem(handlers.HANDLERS, "request_info", lambda ctx: 1 / 0)
    dispatch(
        store, channel, MANAGER, intent("request_info", scope="projects"), body="اعرض المشاريع"
    )
    assert "توقفت بعد خطأ في البند 1" in channel.replies[0].body


def test_unknown_intent_name_falls_back_to_unclear_handler(store, channel):
    assert handlers.handler_for("unclear") is handlers.handle_unclear
    dispatch(store, channel, ALICE, intent("unclear"))
    assert channel.replies[0].body.startswith("I'm not sure how to handle that.")


def test_every_intent_has_a_handler():
    assert set(handlers.HANDLERS) == set(INTENT_NAMES) - {"unclear"}
    assert len(handlers.HANDLERS) == 12


def test_process_message_writes_dispatch_marker_before_classifying(store, channel):
    seen_marker_at_classify_time = []

    class CheckingClassifier:
        def classify(self, m, sender, projects):
            seen_marker_at_classify_time.append("Message dispatch started" in actions(store))
            return classification(intent("request_info", scope="my_tasks"))

    m = msg(ALICE, "show my tasks", msg_id="GMAIL-42", subject="status")
    process_message(m, channel=channel, store=store, classifier=CheckingClassifier())

    assert seen_marker_at_classify_time == [True]
    log = store.rows("Activity_Log")
    started = next(r for r in log if r[1] == "Message dispatch started")
    done = next(r for r in log if r[1] == "fake message processed")
    assert "msg_id:GMAIL-42" in started[2] and "msg_id:GMAIL-42" in done[2]
    assert "Intent: request_info (conf=0.95)" in done[3]
    assert log[-1][1] == "Run completed (agent)"
    assert channel.processed == ["GMAIL-42"]
    assert len(channel.replies) == 1


def test_skipped_message_is_logged_and_marked_without_classification(store, channel):
    channel.skip = (True, "auto-reply header: Auto-Submitted")
    stub = StubClassifier(classification())
    process_message(msg(ALICE, "out of office"), channel=channel, store=store, classifier=stub)
    assert stub.calls == []
    assert channel.replies == []
    assert channel.processed == ["MSG-1"]
    assert "fake message skipped" in actions(store)


def test_end_to_end_with_the_real_classifier_and_a_fake_model(store, channel):
    llm = FakeLLM(one("create_project", 0.9, proposed_name="North Depot"))
    classifier = Classifier(llm, prompt="SYSTEM", primary_model="m")
    process_message(
        msg(MANAGER, "New project: North Depot"),
        channel=channel,
        store=store,
        classifier=classifier,
    )
    assert "P-0004" in load_projects(store)
    assert llm.calls[0]["user"]["sender"]["permission"] == "manager"
    assert "Registered new project P-0004" in channel.replies[0].body


@pytest.mark.parametrize("lang_body", ["تم", "done"])
def test_reply_language_follows_the_message(store, channel, lang_body):
    dispatch(store, channel, MANAGER, intent("create_project", proposed_name="X"), body=lang_body)
    body = channel.replies[0].body
    assert body.startswith("تم تسجيل") if lang_body == "تم" else body.startswith("Registered")
